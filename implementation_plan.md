# Implementation Plan: Aura — Multimodal Live Agent

**Category**: Live Agents (primary) + UI Navigator (stretch goal if time permits)

Aura is a high-fidelity multimodal assistant for the **Gemini Live Agent Challenge**. It sees your screen in real-time, hears your voice with human-like turn detection, and acts on your behalf with sub-second latency.

---

## ✅ Challenge Compliance Checklist

| Requirement | Solution |
|---|---|
| Gemini model | `gemini-live-2.5-flash-native-audio` via Google ADK |
| Google GenAI SDK or ADK | **Google ADK** (Agent Development Kit) |
| Google Cloud hosting | **Cloud Run** (backend), **Cloud Build** (CI/CD), **Vertex AI** (sessions + memory) |
| Multimodal I/O | Audio in + audio out |
| Public repo + README | ✓ |
| <4m demo video | Phase 5 |
| Architecture diagram | Phase 4 |
| Cloud deployment proof | Phase 3 (Terraform + Cloud Build logs) |

### Bonus Points
1. **Automated deployment** — Terraform + `infra/deploy.sh` (IaC in public repo)
2. **Blog post / content** — Technical writeup with `#GeminiLiveAgentChallenge`
3. **GDG profile link** — Added to README

---

## 🏗️ Technical Architecture

### Transport — Low Latency Audio
- **LiveKit WebRTC** — optimised audio streaming from browser to backend
- **Gemini Live server-side VAD** — turn detection handled natively by the model (no local Silero/SmartTurn needed)

### Agent Core
- **Google ADK** — orchestration framework (mandatory); defines agent instruction + tools via `LlmAgent`
- **Gemini Live native audio** — bidi-streaming audio (`gemini-live-2.5-flash-native-audio` on Vertex AI)
- **ADK ↔ LiveKit bridge** — custom async adapter streams WebRTC PCM16 audio into the ADK live session

### Memory & Sessions — 100% Google
- **`VertexAiSessionService`** — ADK-native persistent session service backed by **Vertex AI Agent Engine**
- Stores full conversation history, user context, and facts per `user_id` automatically
- **No Redis, no PostgreSQL, no Azure, no OpenAI** — pure Google Cloud

### Frontend
- **Vite + React** — real-time dashboard (copied from solution3)
- **LiveKit JS SDK** — WebRTC client, audio visualiser, interrupt state sync

### Infrastructure
- **Cloud Run** — containerised backend (auto-scaling, pay-per-use)
- **Vertex AI Agent Engine** — persistent session + memory storage
- **Terraform** — full IaC for Cloud Run, IAM, Secret Manager, Vertex AI enablement
- **Cloud Build** — CI/CD triggered on push to `main`
- **Secret Manager** — API keys (Gemini/Vertex, LiveKit)

---

## 🏁 Phase 1: Backend Foundation

**Goal**: Working audio loop — user speaks, Gemini responds, interruptions handled, sessions persist.

- [ ] Initialise Python project with `google-adk`, `google-genai`, `livekit`, `livekit-api`
- [ ] Define `LlmAgent` with system instruction + ADK tools (`get_current_time`, extensible)
- [ ] Configure `VertexAiSessionService` for per-user persistent sessions on Vertex AI
- [ ] Configure Gemini Live session (`gemini-live-2.5-flash-native-audio`, audio modality, server VAD)
- [ ] Build ADK ↔ LiveKit bridge: LiveKit `AudioStream` → PCM16 → `genai.aio.live` session
- [ ] Route Gemini audio responses → LiveKit `AudioSource` → participant speaker
- [ ] Handle ADK tool calls dispatched during live audio session
- [x] Wire barge-in / interruption via Gemini server-side VAD (no local VAD needed)
- [ ] Verify end-to-end round-trip latency < 600 ms locally

## 🏁 Phase 2: Frontend Dashboard

**Goal**: Browser UI that users can actually demo.

- [ ] Scaffold Vite + React project in `solution4/frontend/`
- [ ] Integrate LiveKit JS SDK — connect to room, publish mic track
- [ ] Audio waveform visualiser (user speaking vs. bot speaking states)
- [ ] Interrupt button + visual state sync (shows when bot is talking, clears on barge-in)
- [ ] Token endpoint wired to backend LiveKit room service

## 🏁 Phase 3: Cloud Deployment (IaC)

**Goal**: Backend live on Google Cloud, reproducible one-command deploy.

- [ ] Write `Dockerfile` for backend service
- [ ] Write Terraform in `infra/` — Cloud Run service, IAM roles, Secret Manager secrets
- [ ] Write `infra/deploy.sh` — one-click bootstrap (`gcloud` auth → `terraform apply`)
- [ ] Set up Cloud Build trigger on `main` branch push
- [ ] Deploy and capture proof screen recording (Cloud Run console + live logs)
- [ ] Add Cloud Run service URL to README

## 🏁 Phase 4: Architecture Diagram & Docs

**Goal**: Submission artefacts ready.

- [ ] Generate Mermaid architecture diagram (browser → LiveKit → ADK → Gemini Live → back)
- [ ] Write `solution4_design_doc.md` — architecture, decisions, engineering challenges
- [ ] README with spin-up instructions (local dev + cloud deploy)
- [ ] Draft technical blog post (`#GeminiLiveAgentChallenge`)

## 🏁 Phase 5: Submission

**Goal**: All submission requirements met.

- [ ] Write final text description (features, tech stack, learnings)
- [ ] Record <4 min demo video (real-time multimodal features, no mockups)
- [ ] Record Cloud proof screen recording (Cloud Run console / live logs)
- [ ] Add GDG profile link to README
- [ ] Verify public repo access, all files committed
- [ ] Submit at GeminiLiveAgentChallenge.com

