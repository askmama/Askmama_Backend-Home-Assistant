from dotenv import load_dotenv
load_dotenv()

import os
import io
import ssl
import wave
import json
import queue
import re
import random
import time
import threading
import numpy as np
import sounddevice as sd
import paho.mqtt.client as mqtt
from datetime import datetime, timezone
from groq import Groq
from elevenlabs.client import ElevenLabs
from app.core.config import settings
from app.core.database import supabase

# --- Config ---
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_DURATION = 1.5
BUFFER_CHUNKS = 3
WEIGHT_TOLERANCE_G = 30

# --- Clients ---
groq_client = Groq(api_key=settings.GROQ_API_KEY)
elevenlabs_client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)

# --- Shared state ---
audio_buffer = []
buffer_lock = threading.Lock()

# MQTT event routing (fed by background MQTT thread, drained by main loop)
_raw_event_queue = queue.Queue()
case1_queue = queue.Queue()
case2_queue = queue.Queue()

pending_intent: dict | None = None
pending_intent_lock = threading.Lock()


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def start_voice_mqtt():
    tls_context = ssl.create_default_context()
    client = mqtt.Client(
        client_id="askmama-voice",
        protocol=mqtt.MQTTv5,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.tls_set_context(tls_context)
    client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe("askmama/+/weight_event", qos=1)
            print("[mqtt] Voice layer connected + subscribed")
        else:
            print(f"[mqtt] Connection failed: rc={rc}")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            _raw_event_queue.put(payload)
        except Exception as e:
            print(f"[mqtt] Parse error: {e}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings.MQTT_BROKER, settings.MQTT_PORT, keepalive=60)
    threading.Thread(target=client.loop_forever, daemon=True).start()


def run_event_router():
    """Route raw scale events to case1_queue or case2_queue."""
    while True:
        try:
            payload = _raw_event_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        with pending_intent_lock:
            if pending_intent is not None:
                case1_queue.put(payload)
            else:
                case2_queue.put(payload)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def record_chunk(duration=CHUNK_DURATION) -> np.ndarray:
    audio = sd.rec(int(duration * SAMPLE_RATE),
                   samplerate=SAMPLE_RATE,
                   channels=CHANNELS,
                   dtype='int16')
    sd.wait()
    return audio.flatten()


def audio_to_wav_bytes(audio: np.ndarray) -> io.BytesIO:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    buf.name = "audio.wav"
    return buf


SILENCE_RMS_THRESHOLD = 100  # below this = silence, skip Whisper

def is_silent(audio: np.ndarray) -> bool:
    return np.sqrt(np.mean(audio.astype(np.float32) ** 2)) < SILENCE_RMS_THRESHOLD

def transcribe(audio: np.ndarray) -> str:
    if is_silent(audio):
        return ""
    try:
        buf = audio_to_wav_bytes(audio)
        result = groq_client.audio.transcriptions.create(
            file=buf,
            model="whisper-large-v3",
            language="en",
        )
        return result.text.strip().lower()
    except Exception:
        return ""


# Whisper tends to hallucinate these short tokens from silence / background noise.
_NO_SPEECH_ARTIFACTS = {"nothing", "you", "uh", "um", "hmm", "huh", "mm", "ah"}

def is_no_speech(text: str) -> bool:
    """True if the transcript is empty or a known silence artifact (not a real command)."""
    t = text.strip().strip(".!?,\"' ").lower()
    return t == "" or len(t) <= 1 or t in _NO_SPEECH_ARTIFACTS


def speak(text: str):
    print(f"[mama] {text}")
    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            voice_id=settings.ELEVENLABS_VOICE_ID,
            text=text,
            model_id="eleven_turbo_v2_5",
            output_format="pcm_16000",
        )
        audio_bytes = b"".join(audio_stream)
        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
        sd.play(audio_array, samplerate=16000)
        sd.wait()
    except Exception as e:
        print(f"[tts] Error: {e}")


# ---------------------------------------------------------------------------
# DB / Supabase
# ---------------------------------------------------------------------------

def get_items_from_db() -> list:
    result = supabase.table("items").select("*").execute()
    return result.data


def check_inventory(item_name: str, items: list) -> str:
    item = next((i for i in items if i["name"] == item_name), None)
    if not item:
        return f"I don't have {item_name} in my records lah."

    # Prefer the stored quantity; fall back to weight / unit if not populated yet.
    qty = item.get("current_quantity")
    if qty is not None:
        return f"You have about {qty} {item_name} left."

    current_weight = item.get("current_weight_g") or 0
    unit_weight = item.get("unit_weight_g") or 0

    if not unit_weight:
        return f"I don't know the unit weight for {item_name}."
    if not current_weight:
        return f"I don't have a current reading for {item_name} yet. Put it on the scale first."

    estimated_qty = int(current_weight / unit_weight)
    return f"You have about {estimated_qty} {item_name} left."


def log_voice_event(intent: dict, payload: dict, status: str):
    supabase.table("events").insert({
        "device_id": "scale-v1",
        "weight_g": payload.get("weight_g", 0),
        "delta_g": payload.get("delta_g", 0),
        "compartment": payload.get("compartment", 1),
        "event_type": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_payload": {**payload, "voice_intent": intent, "voice_status": status},
    }).execute()

    # Keep items.current_weight_g and current_quantity in sync on confirmed events
    if status in ("voice_confirmed", "voice_confirmed_after_mismatch"):
        item_name = intent.get("item_name")
        weight_g = payload.get("weight_g")
        if item_name and weight_g is not None:
            update = {"current_weight_g": weight_g}
            # Derive and store the live quantity directly in the inventory row.
            row = supabase.table("items").select("unit_weight_g").eq("name", item_name).limit(1).execute().data
            unit_weight = row[0]["unit_weight_g"] if row else 0
            if unit_weight:
                update["current_quantity"] = int(weight_g / unit_weight)
            supabase.table("items").update(update).eq("name", item_name).execute()


# ---------------------------------------------------------------------------
# Intent parsing
# ---------------------------------------------------------------------------

WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
}

INTENT_SYSTEM = (
    "You are the intent parser for Mama, a voice inventory assistant. "
    "You turn what the user said into a single JSON object and reply with JSON only."
)


def parse_intent(transcript: str, items: list) -> dict:
    names = [i["name"] for i in items]
    items_str = "\n".join([f"- {i['name']}: {i['unit_weight_g']}g each" for i in items])
    user = f"""Inventory items (match against these, use the name EXACTLY as written):
{items_str}

User said: "{transcript}"

Return a JSON object with these fields:
- "action": one of
    "take"    = removing stock (take, grab, use, remove, get).
    "return"  = adding stock back (return, put back, restock, add, refill, top up).
    "query"   = asking how many / how much is left.
    "unknown" = greetings, thanks, or anything unclear.
- "item_name": the EXACT matching name from the list (fuzzy: "egg" -> "eggs", "jbl" -> "JBL Go 3"). Use null if no item is clearly meant.
- "quantity": integer for take/return. Turn words into numbers (one -> 1, a couple -> 2). If a take/return clearly happened but no count was said, use 1. Use null for query and unknown.

Reply in JSON only. Examples:
{{"action": "take", "item_name": "eggs", "quantity": 2}}
{{"action": "return", "item_name": "eggs", "quantity": 1}}
{{"action": "query", "item_name": "JBL Go 3", "quantity": null}}
{{"action": "unknown", "item_name": null, "quantity": null}}"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": INTENT_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        intent = json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[intent] parse error: {e}")
        return {"action": "unknown", "item_name": None, "quantity": None}

    return _normalize_intent(intent, names)


def _resolve_name(raw: str, names: list) -> str | None:
    """Map the LLM's item_name to a canonical inventory name, or None."""
    raw_l = str(raw).strip().lower()
    if not raw_l:
        return None
    for n in names:                       # exact, case-insensitive
        if n.lower() == raw_l:
            return n
    for n in names:                       # substring either direction
        nl = n.lower()
        if raw_l in nl or nl in raw_l:
            return n
    return None


def _normalize_intent(intent: dict, names: list) -> dict:
    action = intent.get("action")
    if action not in ("take", "return", "query", "unknown"):
        action = "unknown"

    item_name = _resolve_name(intent.get("item_name"), names) if intent.get("item_name") else None

    # An action that needs an item but has none falls back to unknown.
    if not item_name:
        if action in ("take", "return", "query"):
            action = "unknown"
        return {"action": action, "item_name": None, "quantity": None}

    quantity = intent.get("quantity")
    if action in ("take", "return"):
        if isinstance(quantity, str):
            q = quantity.strip().lower()
            quantity = WORD_NUMBERS.get(q, int(q) if q.isdigit() else None)
        if not isinstance(quantity, int) or quantity <= 0:
            quantity = 1                  # default when a count wasn't captured
    else:
        quantity = None

    return {"action": action, "item_name": item_name, "quantity": quantity}


def expected_weight_for(intent: dict, items: list) -> float:
    unit = next((i["unit_weight_g"] for i in items if i["name"] == intent["item_name"]), 0)
    return (intent.get("quantity") or 0) * unit


def weights_match(actual_delta_g: float, expected_g: float) -> bool:
    """Magnitude-only match. Direction is read separately from the delta sign."""
    return abs(abs(actual_delta_g) - expected_g) <= WEIGHT_TOLERANCE_G


def observed_action(delta_g: float) -> str:
    """Source of truth for direction: the scale, not the spoken verb."""
    return "return" if delta_g > 0 else "take"


def action_phrases(action: str) -> dict:
    """Direction-aware wording so Mama speaks correctly for takes and returns."""
    if action == "return":
        return {"cue": "put it back", "past": "added back", "verb": "added to"}
    return {"cue": "take it", "past": "taken", "verb": "removed from"}


# ---------------------------------------------------------------------------
# Case 1 — user declares first, then acts on scale
# ---------------------------------------------------------------------------

def await_scale_confirmation(intent: dict, expected_weight_g: float, timeout: float = 30.0):
    global pending_intent
    with pending_intent_lock:
        pending_intent = intent

    declared = intent.get("action", "take")
    declared_phrases = action_phrases(declared)
    speak(
        f"Okay, I'm watching for {intent['quantity']} {intent['item_name']}. "
        f"Go ahead and {declared_phrases['cue']} now."
    )

    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                payload = case1_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            delta = payload.get("delta_g", 0)
            if weights_match(delta, expected_weight_g):
                # Trust the scale for direction (auto-correct a wrong-direction declaration).
                actual = observed_action(delta)
                confirmed = {**intent, "action": actual}
                phrases = action_phrases(actual)
                log_voice_event(confirmed, payload, "voice_confirmed")
                speak(f"Got it! I've logged {intent['quantity']} {intent['item_name']} {phrases['past']}.")
                return True

            print(f"[voice] Unexpected delta {delta}g (expected ~{expected_weight_g:.0f}g) — ignoring")

        speak("Aiyo, I didn't see any change on the scale. Never mind ah, call me again when you're ready.")
        return False
    finally:
        with pending_intent_lock:
            pending_intent = None


# ---------------------------------------------------------------------------
# Case 2 — scale detects first, handled inline in main loop
# ---------------------------------------------------------------------------

def handle_case2(payload: dict):
    """Called from the main loop — no threading, no mic conflict."""
    delta = payload.get("delta_g", 0)
    # Direction is known from the scale — infer it rather than trusting the spoken verb.
    actual = observed_action(delta)
    phrases = action_phrases(actual)

    speak(f"Hey, something was just {phrases['verb']} the scale. What was it?")

    items = get_items_from_db()
    if not items:
        log_voice_event({}, payload, "unknown_variance")
        return

    # First attempt — 15s window
    audio = record_chunk(duration=15.0)
    command = transcribe(audio)
    print(f"[case2] Heard: {command!r}")

    if not command:
        speak("No answer — I'll check back in a few minutes.")
        _schedule_variance_check(payload)
        return

    intent = parse_intent(command, items)
    if intent["action"] == "unknown" or not intent["item_name"]:
        speak("Hmm, I couldn't catch that. I'll flag this for review.")
        log_voice_event({"action": "unknown"}, payload, "unknown_variance")
        return

    intent["action"] = actual  # scale wins on direction
    expected_g = expected_weight_for(intent, items)
    if weights_match(delta, expected_g):
        log_voice_event(intent, payload, "voice_confirmed")
        speak(f"Got it! Logged {intent['quantity']} {intent['item_name']} {phrases['past']}.")
        return

    # Mismatch — ask once more
    speak(
        f"Hmm, I expected about {expected_g:.0f}g but the scale shows {abs(delta):.0f}g. "
        f"Can you confirm again what was {phrases['past']}?"
    )
    audio2 = record_chunk(duration=10.0)
    command2 = transcribe(audio2)
    print(f"[case2] Confirmation heard: {command2!r}")

    if command2:
        intent2 = parse_intent(command2, items)
        if intent2["action"] != "unknown" and intent2["item_name"]:
            intent2["action"] = actual
            log_voice_event(intent2, payload, "voice_confirmed_after_mismatch")
            speak(f"Okay! Logged {intent2['quantity']} {intent2['item_name']} {phrases['past']}.")
            return

    log_voice_event(intent, payload, "unknown_variance")
    speak("Alright, I'll flag this as an unknown change.")


def _schedule_variance_check(payload: dict, delay_s: float = 300.0):
    def check():
        time.sleep(delay_s)
        log_voice_event({"action": "no_reply"}, payload, "transient_variance")
        print(f"[voice] Transient variance flagged: {payload}")
    threading.Thread(target=check, daemon=True).start()


# ---------------------------------------------------------------------------
# Wake word (local, on-device via openWakeWord — no audio leaves the machine
# until the wake word fires, then Whisper handles the command)
# ---------------------------------------------------------------------------

WAKE_MODEL = "hey_jarvis"   # built-in for now; swap to custom "Hey Mama" later
WAKE_THRESHOLD = 0.5        # detection score 0..1; raise to reduce false triggers
WAKE_FRAME = 1280           # 80 ms @ 16 kHz — openWakeWord's expected frame size

WAKE_ACKS = ("Yes?", "Mmm?", "Yes, I'm here.", "What is it ah?", "Yes dear?")


def handle_command():
    """Run a full voice interaction after the wake word fires."""
    speak(random.choice(WAKE_ACKS))
    audio = record_chunk(duration=3)
    command = transcribe(audio)
    print(f"[command] {command}")

    if is_no_speech(command):
        speak("Never mind ah, call me again when you're ready.")
        return

    items = get_items_from_db()
    if not items:
        speak("I couldn't find any items in the inventory.")
        return

    intent = parse_intent(command, items)
    print(f"[intent] {intent}")

    # Thank-you shortcut (no item involved)
    if intent["action"] == "unknown" and any(w in command for w in ["thank you", "thanks", "thank u"]):
        speak("You're welcome! Take care ah.")
        return

    # Retry once if we couldn't understand the command
    if intent["action"] == "unknown" or not intent["item_name"]:
        speak("Sorry, I didn't catch that. Try again?")
        retry_audio = record_chunk(duration=5)
        retry_command = transcribe(retry_audio)
        print(f"[retry] {retry_command}")
        if retry_command:
            intent = parse_intent(retry_command, items)
            print(f"[intent] {intent}")
        if intent["action"] == "unknown" or not intent["item_name"]:
            speak("Never mind ah, call me again when you're ready.")
            return

    # Dispatch the resolved intent (handles both first pass and retry)
    if intent["action"] == "query":
        speak(check_inventory(intent["item_name"], items))
        return

    expected_g = expected_weight_for(intent, items)
    if expected_g == 0:
        speak(f"I found {intent['item_name']} but I don't know its unit weight yet.")
        return

    await_scale_confirmation(intent, expected_g)


# ---------------------------------------------------------------------------
# Main voice loop
# ---------------------------------------------------------------------------

def run_voice_loop():
    from openwakeword.model import Model

    print("[voice] Starting MQTT and event router...")
    start_voice_mqtt()
    threading.Thread(target=run_event_router, daemon=True).start()

    oww = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    # Score-dict key is the model filename without extension (e.g. "hey_jarvis",
    # or "hey_mama" if WAKE_MODEL points at a custom hey_mama.onnx).
    wake_key = os.path.splitext(os.path.basename(WAKE_MODEL))[0]

    def open_stream():
        s = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                           dtype="int16", blocksize=WAKE_FRAME)
        s.start()
        return s

    print(f"[voice] Listening locally for wake word '{wake_key}'... (scale events also handled)")
    stream = open_stream()
    try:
        while True:
            # Case 2: scale fired first (handle before listening for wake word)
            try:
                payload = case2_queue.get_nowait()
                if abs(payload.get("delta_g", 0)) >= 20:
                    stream.stop(); stream.close()
                    handle_case2(payload)
                    oww.reset()
                    stream = open_stream()
                    continue
            except queue.Empty:
                pass

            # Read one frame locally and score it — nothing leaves the machine here
            frame, _ = stream.read(WAKE_FRAME)
            scores = oww.predict(frame.flatten())

            if scores.get(wake_key, 0.0) >= WAKE_THRESHOLD:
                print(f"[wake] '{wake_key}' detected ({scores[wake_key]:.2f})")
                stream.stop(); stream.close()   # free the mic for command capture / TTS
                handle_command()
                oww.reset()                     # clear buffer to avoid re-triggering
                stream = open_stream()
    finally:
        stream.stop(); stream.close()


if __name__ == "__main__":
    run_voice_loop()
