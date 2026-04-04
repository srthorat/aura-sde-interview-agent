# Implementation Plan: Aura — Google SDE Interview Coach

Aura is a real-time AI-powered Google SDE interview coach. It hears your voice with human-like turn detection, conducts structured Google-style interviews across four rounds, and provides live spoken feedback — all backed by persistent per-candidate session memory.

---

## 🏗️ Technical Architecture

### Transport — Low Latency Audio
- **LiveKit WebRTC** — optimised audio streaming from browser to backend
- **Gemini Live server-side VAD** — turn detection handled natively by the model

### Agent Core
- **Google ADK** — orchestration framework; defines agent instruction + tools via `LlmAgent`
- **Gemini Live native audio** — bidi-streaming audio (`gemini-live-2.5-flash-native-audio` on Vertex AI)
- **ADK ↔ LiveKit bridge** — custom async adapter streams WebRTC PCM16 audio into the ADK live session

### Interview Tools
- `get_interview_question` — pulls a question by round + category from the built-in question bank
- `record_answer_note` — saves structured strength/weakness notes to session memory
- `get_session_summary` — returns a spoken performance summary across all completed rounds
- `get_current_time` — answer-timing signals to the candidate

### Memory & Sessions
- **`VertexAiSessionService`** — ADK-native persistent session service backed by **Vertex AI Agent Engine**
- Stores full conversation history, user context, and notes per `user_id` automatically
- Falls back to `InMemorySessionService` for local dev without GCP credentials

### Frontend
- **Vite + React** — real-time dashboard
- **LiveKit JS SDK** — WebRTC client, audio visualiser, interrupt state sync

### Infrastructure
- **Cloud Run** — containerised backend (auto-scaling, pay-per-use)
- **Vertex AI Agent Engine** — persistent session + memory storage
- **Terraform** — full IaC for Cloud Run, IAM, Secret Manager, Vertex AI enablement
- **Cloud Build** — CI/CD triggered on push to `main`
- **Secret Manager** — LiveKit API credentials

---

## ✅ Phase 1: Backend Foundation

- [x] Initialise Python project with `google-adk`, `google-genai`, `livekit`, `livekit-api`
- [x] Define `LlmAgent` with system instruction + interview tools
- [x] Configure `VertexAiSessionService` for per-user persistent sessions on Vertex AI
- [x] Configure Gemini Live session (`gemini-live-2.5-flash-native-audio`, audio modality, server VAD)
- [x] Build ADK ↔ LiveKit bridge: LiveKit `AudioStream` → PCM16 → `genai.aio.live` session
- [x] Route Gemini audio responses → LiveKit `AudioSource` → participant speaker
- [x] Handle ADK tool calls dispatched during live audio session
- [x] Wire barge-in / interruption via Gemini server-side VAD
- [x] Session timer processor for idle timeout + max call duration

## ✅ Phase 2: Frontend Dashboard

- [x] Scaffold Vite + React project in `frontend/`
- [x] Integrate LiveKit JS SDK — connect to room, publish mic track
- [x] Audio waveform visualiser (user speaking vs. bot speaking states)
- [x] Interrupt button + visual state sync
- [x] Token endpoint wired to backend LiveKit room service

## ✅ Phase 3: Cloud Deployment (IaC)

- [x] `Dockerfile` — multi-stage: Node.js frontend build + Python 3.11 slim runtime
- [x] Terraform in `infra/` — Cloud Run service, IAM roles, Secret Manager secrets
- [x] `infra/deploy.sh` — one-click bootstrap (`gcloud` auth → `terraform apply`)
- [x] `cloudbuild.yaml` — CI/CD pipeline triggered on push to `main`

## 🔲 Phase 4: Enhancements

- [ ] Add Round 4 debrief logic — auto-detect weak spots from `record_answer_note` history
- [ ] Scoring rubric per round (1–4 scale, spoken at end of each round)
- [ ] Candidate progress dashboard in frontend (rounds completed, scores over time)
- [ ] Support for custom question sets via environment config
- [ ] Add architecture diagram to README

