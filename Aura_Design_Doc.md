# Design Document — Aura: Google SDE Interview Coach

**Stack**: Google ADK · Gemini Live · Vertex AI · LiveKit · Cloud Run  
**Model**: `gemini-live-2.5-flash-native-audio`

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Browser                                                                 │
│  ┌─────────────────────────────────────────────────────┐                │
│  │  Vite + React frontend (demo.html)                  │                │
│  │  LiveKit JS SDK — mic capture + bot audio playback  │                │
│  │  Real-time transcript + metrics panel               │                │
│  └──────────┬───────────────────────────┬──────────────┘                │
│             │ WebRTC (audio)             │ HTTP POST /livekit/session    │
└─────────────┼─────────────────────────── ┼ ─────────────────────────────┘
              │                             │
┌─────────────▼─────────────────────────── ▼ ─────────────────────────────┐
│  Cloud Run — Aura Google SDE Interview Coach (FastAPI + Python 3.11)     │
│                                                                          │
│  POST /livekit/session ────► token mint ──► spawn AuraVoiceSession task  │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  AuraVoiceSession                                                 │   │
│  │                                                                   │   │
│  │  LiveKit rtc.Room ◄──── WebRTC PCM16 audio ───► browser mic      │   │
│  │       │                                                           │   │
│  │       │  rtc.AudioStream (PCM16 @ 16 kHz)                        │   │
│  │       ▼                                                           │   │
│  │  _send_audio_loop ──────────────────────────────────────────────► │   │
│  │       │                           google-genai aio.live           │   │
│  │       │                           ┌──────────────────────────┐   │   │
│  │       └──────────────────────────►│  Gemini Live session      │   │   │
│  │                                   │  gemini-live-2.5-flash    │   │   │
│  │  ◄────────────────────────────────│  -native-audio            │   │   │
│  │  _recv_loop                       │  (Vertex AI, bidi stream) │   │   │
│  │  ├─ audio data ──► rtc.AudioSource│                           │   │   │
│  │  ├─ text turns ──► events/transcript                          │   │   │
│  │  └─ tool calls ──► dispatch_tool_call ──► LlmAgent tools      │   │   │
│  │                                   └──────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  Google ADK                                                              │
│  ├─ LlmAgent (name="aura", model="gemini-2.5-flash", tools=[get_interview_question, record_answer_note, get_session_summary, get_current_time])         │
│  ├─ VertexAiSessionService ──► Vertex AI Agent Engine (per-user history) │
│  └─ dispatch_tool_call(name, args) → result dict                         │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
              │
              │  HTTPS (Secret Manager, Vertex AI Agent Engine)
              ▼
┌─────────────────────────────────┐
│  Google Cloud                   │
│  ├─ Vertex AI Agent Engine      │  ← VertexAiSessionService (sessions)
│  ├─ Vertex AI (Gemini Live)     │  ← bidi audio API
│  ├─ Secret Manager              │  ← LiveKit API key/secret
│  ├─ Artifact Registry           │  ← Docker images
│  └─ Cloud Build                 │  ← CI/CD on push to main
└─────────────────────────────────┘
```

---

## Key Design Decisions

### 1. Google ADK (mandatory requirement)

`google-adk` is used as the orchestration layer:
- `LlmAgent` defines the agent name, model, system instruction, and tool set
- `VertexAiSessionService` provides per-user persistent conversation history via Vertex AI Agent Engine — no Redis, no PostgreSQL
- History from previous sessions is injected into the Gemini Live system instruction before each call, giving Aura genuine long-term memory

### 2. Gemini Live native audio via `google-genai`

`genai.aio.live.connect()` opens a bidirectional gRPC stream directly to Gemini Live on Vertex AI. This gives:
- Server-side voice activity detection (no client-side Silero/WebRTC VAD needed)
- Native barge-in / interruption handling
- Sub-300 ms first-audio-output latency

Audio flows: **Browser mic → LiveKit WebRTC → PCM16 @ 16 kHz → Gemini Live → PCM16 @ 24 kHz → LiveKit → Browser speaker**

### 3. LiveKit for WebRTC transport

LiveKit handles the browser-to-server WebRTC plumbing:
- `rtc.AudioStream` consumes remote participant audio frames
- `rtc.AudioSource` / `rtc.LocalAudioTrack` publish bot audio back to the room
- Data messages (`publish_data`) carry real-time transcript events and metrics to the UI

### 4. Tool dispatch during live audio

Four interview tools are declared as `FunctionDeclaration` objects to the Gemini Live session:

| Tool | Purpose |
|---|---|
| `get_interview_question` | Pulls a question by round + category from the built-in question bank |
| `record_answer_note` | Saves a structured strength/weakness note to session memory |
| `get_session_summary` | Returns a spoken performance summary across completed rounds |
| `get_current_time` | Provides answer-timing signals to the candidate |

When Gemini emits a `tool_call` event mid-stream, `dispatch_tool_call()` runs the matching Python function and returns a `FunctionResponse` — all within the live audio session, with zero interruption to the audio flow.

### 5. Cloud Run + Terraform IaC

- **Cloud Run** — stateless, auto-scaling, pay-per-use. Each session runs as an `asyncio.Task` within the process.
- **Terraform** (`infra/`) provisions all GCP resources: Cloud Run service, Artifact Registry, IAM, Secret Manager, Cloud Build trigger.
- **`infra/deploy.sh`** — one-command bootstrap: stores secrets, runs `terraform apply`, builds & pushes the Docker image.
- **`cloudbuild.yaml`** — triggered on every push to `main`; builds image → deploys to Cloud Run.

---

## Data Flow — Single Call

```
1. User opens browser → POST /livekit/session
2. Backend mints user token + bot token, spawns AuraVoiceSession task
3. AuraVoiceSession:
   a. Loads ADK session from VertexAiSessionService (prior history)
   b. Connects LiveKit room (both user and bot)
   c. Opens Gemini Live bidi stream with system instruction + injected history + tools
   d. Publishes bot audio track to LiveKit room
   e. Sends opening greeting to Gemini Live
   f. Runs concurrently:
      - _send_audio_loop: Browser PCM16 → Gemini Live (100 ms chunks, idle timeout)
      - _recv_loop: Gemini Live audio → LiveKit; tool calls → dispatch; text → events
      - Timeout watchdog (max call duration)
4. On session end: new turns appended to VertexAiSessionService
```

---

## File Structure

```
├── bot/
│   ├── agent.py              # LlmAgent, tool registry, LIVE_TOOL_DECLARATIONS
│   ├── bot.py                # FastAPI app, /livekit/session, /health
│   ├── pipelines/
│   │   └── voice.py          # AuraVoiceSession, _send_audio_loop, _recv_loop
│   ├── processors/
│   │   └── session_timer.py  # Call duration + idle timeout utility
│   └── prompts/
│       └── system_prompt.md  # Aura persona + Google SDE interview guidelines
├── frontend/
│   ├── public/demo.html      # Voice UI (LiveKit JS SDK, transcript + metrics)
│   └── src/App.tsx           # Vite/React wrapper
├── infra/
│   ├── main.tf               # Cloud Run, Artifact Registry, IAM, Secrets, Build trigger
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   ├── terraform.tfvars.example
│   └── deploy.sh             # One-click bootstrap
├── cloudbuild.yaml           # CI/CD: build image → deploy to Cloud Run
├── Dockerfile                # Multi-stage: Node.js frontend + Python 3.11 slim
├── docker-compose.yml        # Local dev (no external dependencies)
├── pyproject.toml            # Dependencies (uv)
└── .env.example              # All environment variables documented
```

---

## Engineering Challenges

### Bridging ADK + Gemini Live

Google ADK's `Runner` is designed for synchronous text/function turn loops. Gemini Live's `aio.live` API is an async bidirectional audio stream. These two paradigms needed to be composed without coupling:

- ADK tools are declared to the Gemini Live session as `FunctionDeclaration` objects, mirroring the ADK `LlmAgent`'s tool registry
- Tool calls arrive as `message.tool_call` events mid-stream and are dispatched via `dispatch_tool_call()`, which delegates to the same Python functions the ADK agent uses
- ADK sessions (`VertexAiSessionService`) are used only for persistent history — they do not drive the live audio loop

### Memory injection without a text turn

`VertexAiSessionService` stores full conversation history — which round the candidate is on, questions asked, and `record_answer_note` entries. Gemini Live doesn't have a "prepend history" API, so the last 20 ADK session events are formatted as a plain-text context block and appended to the system instruction before opening the live session. This gives genuine cross-session continuity: Aura knows exactly where the candidate left off without an extra API call.

### PCM16 resampling on the hot path

LiveKit delivers audio at whatever sample rate the browser produces (typically 48 kHz stereo). Gemini Live requires 16 kHz mono. NumPy linear interpolation handles resampling in-process with negligible latency (<0.5 ms per 100 ms chunk on a standard Cloud Run instance).
