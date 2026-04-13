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
│  │       │                       ADK Runner.run_live +               │   │
│  │       ├─ Silero VAD (UI hints only)  LiveRequestQueue            │   │
│  │       │                           ┌──────────────────────────┐   │   │
│  │       └──────────────────────────►│  Gemini Live session      │   │   │
│  │                                   │  gemini-live-2.5-flash    │   │   │
│  │  ◄────────────────────────────────│  -native-audio            │   │   │
│  │  _process_events                  │  (Vertex AI, bidi stream) │   │   │
│  │  ├─ audio data ──► rtc.AudioSource│                           │   │   │
│  │  ├─ text turns ──► transcript + summary state                  │   │   │
│  │  └─ tool calls ──► ADK tool callbacks + LlmAgent tools         │   │   │
│  │                                   └──────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  Google ADK                                                              │
│  ├─ LlmAgent (name="aura") with interview tools and guardrails          │
│  ├─ Runner.run_live(...) for audio + tool orchestration                  │
│  ├─ VertexAiSessionService ──► Vertex AI Agent Engine (per-user history) │
│  └─ Persisted state snapshots for notes, grades, and round continuity    │
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

`google-adk` is the orchestration layer for the final system:
- `LlmAgent` defines Aura's instruction set, tool surface, and interview behavior
- `Runner.run_live()` handles the live Gemini session and tool execution loop
- `VertexAiSessionService` provides per-user persistent session history via Vertex AI Agent Engine
- Aura also persists structured state snapshots for asked questions, rubric grades, answer notes, and round continuity so feedback survives restarts without introducing another database

### 2. Gemini Live native audio via `google-genai`

Gemini Live is used through the ADK live runner on Vertex AI. This gives:
- Server-side voice activity detection for actual turn control
- Native barge-in / interruption handling
- Sub-300 ms first-audio-output latency

Silero remains in the backend only for faster UI speaking indicators and STS timing. It does not decide turn boundaries for Gemini.

Audio flows: **Browser mic → LiveKit WebRTC → PCM16 @ 16 kHz → Gemini Live → PCM16 @ 24 kHz → LiveKit → Browser speaker**

### 3. LiveKit for WebRTC transport

LiveKit handles the browser-to-server WebRTC plumbing:
- `rtc.AudioStream` consumes remote participant audio frames
- `rtc.AudioSource` / `rtc.LocalAudioTrack` publish bot audio back to the room
- Data messages (`publish_data`) carry real-time transcript events and metrics to the UI

### 4. Tool dispatch during live audio

Aura now uses a larger tool surface during live audio:

| Tool | Purpose |
|---|---|
| `get_current_time` | Provides answer-timing signals to the candidate |
| `get_interview_question` | Pulls a question by round + category from the built-in question bank |
| `record_answer_note` | Saves a structured strength/weakness note to session memory |
| `get_session_summary` | Returns a spoken performance summary across completed rounds |
| `submit_rubric_grade` | Captures evidence-based rubric grades continuously during the interview |
| `get_rubric_report` | Returns the rubric report used for end-of-round and end-of-call feedback |
| `get_round_scorecard` | Produces a spoken 1-4 round score with top strength and focus area |
| `end_conversation` | Gracefully ends the session only after explicit user intent |

ADK tool callbacks set per-session context before each tool call, so the live model reads and writes the correct candidate state during the interview. This makes spoken round scoring and post-call summaries consistent with the same in-memory and persisted state.

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
   a. Creates isolated live session state and restores prior named-user state if available
   b. Connects LiveKit room (both user and bot)
   c. Starts `Runner.run_live()` with Gemini Live, tool callbacks, and Vertex-backed session storage
   d. Publishes bot audio track to LiveKit room
   e. Sends opening greeting to Gemini Live
   f. Runs concurrently:
      - _send_audio_loop: Browser PCM16 → Gemini Live (100 ms chunks, idle timeout)
      - _process_events: Gemini Live audio → LiveKit; transcripts/events → frontend; tool results → live state
      - Timeout watchdog (max call duration)
4. On session end:
   - auto-grading fills any missing rubric evidence
   - a narrative summary is generated
   - a final state snapshot is persisted for named users
   - call summary is sent to the frontend and optional webhook
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

The main challenge was using ADK's live runner as the source of truth while still keeping the transport and UX responsive:

- LiveKit remains responsible for browser audio transport and immediate playback control
- ADK `Runner.run_live()` remains responsible for tool invocation, agent instruction enforcement, and session persistence
- Silero VAD is intentionally limited to UI hints and instant barge-in responsiveness, while Gemini server-side VAD remains the authority for turn boundaries

### Durable structured memory without another database

Conversation history already lives in `VertexAiSessionService`, but interview-grade structured state originally lived only in process memory. The final design persists compact state snapshots into the same ADK session history and restores them for named users on the next session. This keeps the architecture hackathon-simple while still preserving notes, grades, and round continuity across restarts.

### Controlled history reuse instead of raw reinjection

Earlier iterations relied on reusing raw prior turns. The final implementation compresses prior context and avoids reinjecting synthetic persistence events into the model prompt. This preserves continuity without wasting prompt budget or polluting the conversation with implementation details.

### Audio on the hot path

LiveKit is configured to deliver 16 kHz mono audio directly into the Gemini path, avoiding unnecessary extra conversion logic in the hot loop and keeping the runtime simple enough for Cloud Run.
