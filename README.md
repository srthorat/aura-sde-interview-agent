# Aura — SDE Interview Agent

Aura is a real-time AI-powered SDE interview coach built on the **Google ADK** + **Gemini Live** native audio stack with **LiveKit** WebRTC transport and **Vertex AI** persistent session memory.

| Layer | Technology |
|---|---|
| Agent framework | Google ADK (`google-adk`) — `LlmAgent`, `VertexAiSessionService` |
| Voice model | `gemini-live-2.5-flash-native-audio` via Vertex AI bidi stream |
| WebRTC transport | LiveKit (`livekit`, `livekit-api`) |
| Session memory | Vertex AI Agent Engine (`VertexAiSessionService`) |
| Infra | Google Cloud only (Cloud Run, Artifact Registry, Secret Manager, Vertex AI) |
| Backend | FastAPI + Python 3.11, containerised on Cloud Run |
| Frontend | Vite + React + LiveKit JS SDK |
| IaC | Terraform + Cloud Build CI/CD |

---

## Architecture

```
Browser mic → LiveKit WebRTC → PCM16 @ 16 kHz → Gemini Live (Vertex AI)
                                                        ↓
Browser speaker ← LiveKit WebRTC ← PCM16 @ 24 kHz ← Gemini audio response
```

Full diagram and design decisions: see [Architecture](#architecture) section above.

---

## Local Development

### Prerequisites

- Python 3.11+, [`uv`](https://github.com/astral-sh/uv)
- Node.js 20+
- A [LiveKit Cloud](https://livekit.io) project (free tier works)
- A Google Cloud project with Vertex AI enabled **or** a Google AI Studio API key

### 1. Clone and install

```bash
git clone https://github.com/srthorat/aura-sde-interview-agent.git
cd aura-sde-interview-agent
cp .env.example .env
# Fill in .env — see section below
uv sync
```

### 2. Configure `.env`

Minimum required values:

```bash
# LiveKit
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

# Google Cloud (Vertex AI path — recommended)
GOOGLE_CLOUD_PROJECT_ID=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1
# Authenticate with: gcloud auth application-default login

# --- OR --- Google AI Studio (preview path, no GCP project needed)
# GEMINI_MODEL=preview
# GOOGLE_API_KEY=your_ai_studio_api_key
```

### 3. Run the backend

```bash
uv run python -m bot.bot
# Listening on http://localhost:7862
```

### 4. Run the frontend (separate terminal)

```bash
cd frontend
npm install
npm run dev
# http://localhost:3000
```

Open `http://localhost:3000`, enter a User ID (1–10), click **Connect**, and speak.

### 5. Single-container run (Docker)

```bash
cp .env.example .env   # fill in values

docker compose up --build -d
docker compose ps
curl http://localhost:7862/health
```

Open `http://localhost:7862`.

---

## Cloud Deployment (Google Cloud Run)

### One-click deploy

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # fill in project_id, livekit_url, etc.
./deploy.sh
```

The script:
1. Stores LiveKit credentials in Secret Manager
2. Provisions Cloud Run, Artifact Registry, IAM, and Cloud Build trigger via Terraform
3. Builds and pushes the Docker image to Artifact Registry
4. Deploys to Cloud Run and prints the live URL

### Manual Terraform

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # edit values

terraform init
terraform plan
terraform apply
```

### CI/CD

`cloudbuild.yaml` at the repo root is triggered automatically on every push to `main`. It:
1. Builds the Docker image (with layer caching from the `:latest` tag)
2. Pushes `:<commit-sha>` and `:latest` to Artifact Registry
3. Rolls out the new image to Cloud Run

To enable the trigger, update the `github.owner` and `github.name` values in `infra/main.tf` before running `terraform apply`.

---

## Environment Variables

See [`.env.example`](.env.example) for the full list with descriptions. Key variables:

| Variable | Required | Description |
|---|---|---|
| `LIVEKIT_URL` | ✓ | LiveKit server WebSocket URL |
| `LIVEKIT_API_KEY` | ✓ | LiveKit API key |
| `LIVEKIT_API_SECRET` | ✓ | LiveKit API secret |
| `GOOGLE_CLOUD_PROJECT_ID` | ✓* | GCP project for Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | — | GCP region (default: `us-central1`) |
| `GOOGLE_API_KEY` | ✓* | Google AI Studio key (preview path only) |
| `GEMINI_LIVE_MODEL` | — | Model name (default: `gemini-live-2.5-flash-native-audio`) |
| `GEMINI_VOICE` | — | Voice name (default: `Aoede`) |
| `USER_IDLE_TIMEOUT_SECS` | — | Idle silence timeout (default: `120`) |
| `MAX_CALL_DURATION_SECS` | — | Hard call limit in seconds (default: `840`) |

\* One of `GOOGLE_CLOUD_PROJECT_ID` (Vertex AI) or `GOOGLE_API_KEY` (preview) is required.

---

## Project Structure

```
├── bot/
│   ├── agent.py              # ADK LlmAgent, tool registry, LIVE_TOOL_DECLARATIONS
│   ├── bot.py                # FastAPI app — /livekit/session, /health
│   ├── pipelines/
│   │   └── voice.py          # AuraVoiceSession — LiveKit ↔ Gemini Live bridge
│   ├── processors/
│   │   └── session_timer.py  # Call duration utility
│   └── prompts/
│       └── system_prompt.md  # Aura persona and interview guidelines
├── frontend/
│   ├── public/demo.html      # Voice UI (real-time transcript + metrics)
│   └── src/App.tsx           # Vite/React wrapper
├── infra/
│   ├── main.tf               # Cloud Run, Artifact Registry, IAM, Secret Manager
│   ├── variables.tf / outputs.tf / versions.tf
│   ├── terraform.tfvars.example
│   └── deploy.sh             # One-click bootstrap script
├── cloudbuild.yaml           # Cloud Build CI/CD pipeline
├── Dockerfile                # Multi-stage: Node.js build + Python 3.11 slim
├── docker-compose.yml        # Local single-container run
├── pyproject.toml            # Python dependencies (uv)
└── .env.example              # All environment variables documented
```

---

## Adding Tools

Tools are defined in `bot/agent.py`. To add a new tool:

1. Write a Python function and add it to `TOOL_REGISTRY`
2. Add a matching entry to `LIVE_TOOL_DECLARATIONS` (used by the Gemini Live session)
3. Add the function to the `tools=[]` list in `build_adk_agent()`

The same function is invoked whether the call comes from the ADK text runner or the live audio session.

---

## Custom Domain — aura.veloxpro.in

Aura uses a **Global HTTPS Load Balancer** in front of Cloud Run, with a Google-managed SSL certificate. HTTPS is fully automatic — no Caddy, no manual certs.

### Step 1 — Deploy the Cloud Run service first

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # fill in project_id + livekit_url
./deploy.sh
```

### Step 2 — Provision the Load Balancer

In `infra/terraform.tfvars` set:
```hcl
enable_custom_domain = true
custom_domain        = "aura.veloxpro.in"
```

Then apply:
```bash
terraform apply
```

Get the static IP that was reserved:
```bash
terraform output lb_ip
# e.g. 34.102.136.180
```

### Step 3 — Add DNS record in GoDaddy

1. Log in to [GoDaddy](https://sso.godaddy.com) → **My Products** → `veloxpro.in` → **DNS**
2. Click **Add New Record**
3. Fill in:

   | Field | Value |
   |---|---|
   | Type | `A` |
   | Name | `aura` |
   | Value | *(the IP from `terraform output lb_ip`)* |
   | TTL | 600 (or default) |

4. Click **Save**

### Step 4 — Wait for certificate provisioning

Google automatically provisions a managed TLS cert for `aura.veloxpro.in` once DNS resolves correctly. This takes **10–20 minutes** after the DNS record propagates.

Check the status:
```bash
gcloud compute ssl-certificates describe aura-ssl-cert \
  --global --project=YOUR_PROJECT_ID \
  --format='value(managed.status, managed.domainStatus)'
# ACTIVE means the cert is live
```

### Step 5 — Verify

```bash
curl https://aura.veloxpro.in/health
# {"status":"ok","bot":"Aura",...}
```

Open `https://aura.veloxpro.in` in a browser — the mic permission prompt should appear.

> **HTTP → HTTPS redirect** is included automatically. Any request to `http://aura.veloxpro.in` is redirected 301 to `https://`.

---

## HTTPS Deployment Options

Browser microphone access requires HTTPS on any public URL. Two GCP paths:

### Option A — Cloud Run (recommended, HTTPS built-in)

Cloud Run is already provisioned by Terraform (`infra/`). Google's load balancer terminates TLS automatically — **no Caddy or reverse proxy needed**.

```bash
# One-command deploy (provisions + builds + deploys)
cd infra && ./deploy.sh
```

Your service is immediately available at `https://aura-voice-agent-<hash>-uc.a.run.app`.

To use a **custom domain**: in Cloud Run console → *Domain mappings* → add your domain. Google provisions and renews the certificate automatically.

---

### Option B — Google Compute Engine VM + Caddy

Use this if you need a persistent VM (e.g. for testing, or if Cloud Run's request-based scaling doesn't fit your workload).

#### 1. Create a GCE VM

```bash
gcloud compute instances create aura-vm \
  --project=YOUR_PROJECT_ID \
  --zone=us-central1-a \
  --machine-type=e2-standard-2 \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --tags=http-server,https-server \
  --metadata=startup-script='#! /bin/bash
    apt-get update
    apt-get install -y docker.io
    systemctl enable --now docker'
```

#### 2. Open firewall ports

```bash
gcloud compute firewall-rules create allow-http-https \
  --allow=tcp:80,tcp:443 \
  --target-tags=http-server,https-server \
  --project=YOUR_PROJECT_ID
```

#### 3. SSH in and start the container

```bash
gcloud compute ssh aura-vm --zone=us-central1-a

# On the VM:
git clone https://github.com/your-org/velox-voice-ai-agent.git
cd velox-voice-ai-agent/solution4
cp .env.example .env   # fill in values
docker compose up --build -d
curl http://127.0.0.1:7862/health   # verify
```

#### 4. Install Caddy and configure HTTPS

```bash
# On the VM:
apt-get install -y caddy
cp deploy/caddy/Caddyfile.example /etc/caddy/Caddyfile
# Edit /etc/caddy/Caddyfile — replace aura.example.com with your real domain
systemctl enable --now caddy
```

5. **Point DNS** — create an `A` record for your domain pointing to the VM's external IP:
   ```bash
   gcloud compute instances describe aura-vm --zone=us-central1-a \
     --format='value(networkInterfaces[0].accessConfigs[0].natIP)'
   ```

Key points:
- Aura listens on port `7862`; Caddy terminates TLS on `443` and proxies to `127.0.0.1:7862`
- The Caddyfile includes a `keepalive` transport directive to keep LiveKit WebSocket signalling alive
- Caddy auto-renews Let's Encrypt certificates — no manual cert management needed

---

## Health Check

```bash
curl https://your-cloud-run-url/health
# {"status":"ok","bot":"Aura","transport":"livekit","model":"gemini-live-2.5-flash-native-audio","active_rooms":0}
```
