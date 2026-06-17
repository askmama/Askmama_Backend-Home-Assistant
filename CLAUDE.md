# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AskMama is a voice-driven smart inventory system. A physical scale detects weight changes via MQTT, a FastAPI backend logs events to Supabase, and an on-device voice agent ("Mama") lets users declare or confirm inventory actions by speaking.

## Commands

```bash
# Activate venv
.\venv\Scripts\Activate.ps1        # PowerShell
.\venv\Scripts\activate.bat         # CMD

# Run the API server
uvicorn main:app --reload

# Run the voice agent (standalone, not via uvicorn)
python mama_voice.py

# Install dependencies
pip install -r requirements.txt
```

There are no tests, linter config, or type-checking setup in this repo currently.

## Architecture

Two entry points, one shared core:

- **`main.py`** — FastAPI app. Starts the MQTT listener on startup (lifespan hook) and mounts the REST API. This is the headless backend; deploy it on a server.
- **`mama_voice.py`** — Standalone voice agent. Runs its own MQTT client, wake-word detection (openWakeWord), speech-to-text (Groq Whisper), LLM intent parsing (Groq Llama), and TTS (ElevenLabs). Designed to run on the edge device next to the scale.

### Data flow

```
Scale (ESP32) --MQTT--> HiveMQ Cloud --MQTT--> Backend / Voice Agent
                                                     |
                                                     v
                                                  Supabase
                                                 (events, items tables)
```

MQTT topic pattern: `askmama/+/weight_event` — the wildcard segment is the `device_id`.

### Core modules (`app/`)

| Module | Role |
|---|---|
| `core/config.py` | `Settings` via pydantic-settings, reads `.env` |
| `core/database.py` | Singleton Supabase client |
| `api/routes.py` | REST endpoints under `/api/v1` (get events, get weight, register item) |
| `iot/mqtt_client.py` | MQTT subscriber for the API server; routes payloads to `inventory/service.py` |
| `inventory/service.py` | `process_weight_event` — persists events, runs low-stock check |

### Voice agent (`mama_voice.py`)

Two interaction cases drive the design:

- **Case 1 (voice-first):** User says "I'm taking 2 eggs" via wake word → agent sets `pending_intent`, waits for a matching scale event on `case1_queue`.
- **Case 2 (scale-first):** Scale fires with no pending intent → event lands on `case2_queue` → agent asks "what was that?" and records the answer.

An event router thread reads from `_raw_event_queue` and dispatches to the correct case queue based on whether `pending_intent` is set.

Weight confirmation uses magnitude matching (`WEIGHT_TOLERANCE_G = 30g`) and trusts the scale's delta sign for direction (take vs return), auto-correcting mismatched spoken verbs.

## Environment Variables

All required in `.env` (loaded by pydantic-settings):

| Variable | Purpose |
|---|---|
| `MQTT_BROKER` | HiveMQ Cloud hostname |
| `MQTT_PORT` | Default 8883 (TLS) |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | HiveMQ credentials |
| `SUPABASE_URL` / `SUPABASE_KEY` | Supabase project connection |
| `GROQ_API_KEY` | Groq API (Whisper STT + Llama intent parsing) |
| `GOOGLE_API_KEY` | Google API key (declared in config, usage TBD) |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | ElevenLabs TTS |

## Supabase Tables

- **`events`** — weight change log (device_id, weight_g, delta_g, compartment, event_type, timestamp, raw_payload)
- **`items`** — registered inventory items (name, unit_weight_g, low_stock_threshold, current_weight_g, current_quantity, device_id)
