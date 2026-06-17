# AskMama Backend & Voice Agent

A voice-driven smart inventory system. A physical scale detects weight changes via MQTT, a FastAPI backend logs events to Supabase, and an on-device voice agent lets users declare or confirm inventory actions by speaking.

## Prerequisites

- Python 3.10+
- A [HiveMQ Cloud](https://www.hivemq.com/mqtt-cloud-broker/) MQTT broker
- A [Supabase](https://supabase.com/) project with `events` and `items` tables
- API keys for [Groq](https://groq.com/), [ElevenLabs](https://elevenlabs.io/), and Google
- (Voice agent only) A microphone and speakers on the host device

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/your-org/Askmama_Backend-Home-Assistant.git
   cd Askmama_Backend-Home-Assistant
   ```

2. **Create and activate a virtual environment**

   ```bash
   python -m venv venv

   # Windows PowerShell
   .\venv\Scripts\Activate.ps1

   # Windows CMD
   .\venv\Scripts\activate.bat

   # macOS / Linux
   source venv/bin/activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

   The voice agent (`mama_voice.py`) has additional dependencies not in `requirements.txt`:

   ```bash
   pip install groq elevenlabs sounddevice numpy openwakeword
   ```

4. **Configure environment variables**

   Create a `.env` file in the project root:

   ```env
   MQTT_BROKER=your-hivemq-host.s1.eu.hivemq.cloud
   MQTT_PORT=8883
   MQTT_USERNAME=your-mqtt-username
   MQTT_PASSWORD=your-mqtt-password

   SUPABASE_URL=https://your-project.supabase.co
   SUPABASE_KEY=your-supabase-anon-key

   GROQ_API_KEY=your-groq-api-key
   GOOGLE_API_KEY=your-google-api-key

   ELEVENLABS_API_KEY=your-elevenlabs-api-key
   ELEVENLABS_VOICE_ID=your-voice-id
   ```

## Running

### API Server

Starts the FastAPI backend with MQTT listener for logging scale events to Supabase:

```bash
uvicorn main:app --reload
```

The API is available at `http://localhost:8000`. Key endpoints:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| GET | `/api/v1/scales/{device_id}/events` | Get weight events for a device |
| GET | `/api/v1/scales/{device_id}/weight` | Get latest weight reading |
| POST | `/api/v1/items` | Register an inventory item |

### Voice Agent

Runs the standalone voice assistant on an edge device (e.g. Raspberry Pi) next to the scale:

```bash
python mama_voice.py
```

This starts:
- An MQTT client listening for scale events
- Wake-word detection (says "Hey Jarvis" to activate)
- Speech-to-text via Groq Whisper
- Intent parsing via Groq Llama
- Text-to-speech via ElevenLabs

The voice agent handles two interaction flows:
- **Voice-first:** Say the wake word, then tell Mama what you're taking/returning. The agent waits for the scale to confirm.
- **Scale-first:** Place or remove an item without speaking first. The agent detects the change and asks what it was.
