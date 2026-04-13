#!/usr/bin/env bash
# ── Aura — Google SDE Interview Coach — One-click deploy ────────────────────
# Usage:
#   cd infra
#   cp terraform.tfvars.example terraform.tfvars   # fill in your values
#   ./deploy.sh
#
# Prerequisites:
#   - gcloud CLI authenticated: gcloud auth application-default login
#   - Terraform >= 1.7 installed
#   - docker CLI installed (for initial image push if trigger isn't set up yet)
#   - Python 3 with vertexai SDK (uv run used automatically if uv is available)
#
# What this script does:
#   1. Enables required GCP APIs
#   2. Creates a GCS staging bucket if needed
#   3. Creates the Vertex AI Reasoning Engine for session persistence (idempotent)
#   4. Creates Artifact Registry, Cloud Run service, IAM, Secret Manager secrets
#   5. Builds and pushes the Docker image to Artifact Registry
#   6. Deploys to Cloud Run
#   7. Prints the live service URL
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Load project_id and region from tfvars ────────────────────────────────────
if [[ ! -f "${SCRIPT_DIR}/terraform.tfvars" ]]; then
  echo "ERROR: terraform.tfvars not found."
  echo "  cp terraform.tfvars.example terraform.tfvars  # then fill in your values"
  exit 1
fi

PROJECT_ID=$(grep '^project_id' "${SCRIPT_DIR}/terraform.tfvars" | awk -F'"' '{print $2}')
REGION=$(grep '^region' "${SCRIPT_DIR}/terraform.tfvars" | awk -F'"' '{print $2}')
REGION=${REGION:-us-central1}
GCS_STAGING=$(grep '^gcs_staging_bucket' "${SCRIPT_DIR}/terraform.tfvars" | awk -F'"' '{print $2}')
GCS_STAGING=${GCS_STAGING:-aura-staging-${PROJECT_ID:0:8}}

echo "==> Project: ${PROJECT_ID}  Region: ${REGION}"

# ── Enable required GCP APIs first (idempotent) ───────────────────────────────
echo ""
echo "==> Enabling required GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  compute.googleapis.com \
  storage.googleapis.com \
  certificatemanager.googleapis.com \
  --project="${PROJECT_ID}" --quiet
echo "  APIs enabled."

# ── Ensure GCS staging bucket exists (needed for Reasoning Engine creation) ──
echo ""
echo "==> Ensuring GCS staging bucket: gs://${GCS_STAGING}"
if gsutil ls -b "gs://${GCS_STAGING}" > /dev/null 2>&1; then
  echo "  Bucket already exists."
else
  gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${GCS_STAGING}"
  echo "  Bucket created."
fi

# ── Create / retrieve Vertex AI Reasoning Engine (idempotent) ────────────────
echo ""
echo "==> Vertex AI Reasoning Engine (session persistence)"

# Check if already set in tfvars (non-empty value)
EXISTING_ENGINE_ID=$(grep '^reasoning_engine_id' "${SCRIPT_DIR}/terraform.tfvars" 2>/dev/null | awk -F'"' '{print $2}' | tr -d '[:space:]')

if [[ -n "${EXISTING_ENGINE_ID}" ]]; then
  echo "  Using existing engine ID from terraform.tfvars: ${EXISTING_ENGINE_ID}"
  REASONING_ENGINE_ID="${EXISTING_ENGINE_ID}"
else
  echo "  reasoning_engine_id not set in terraform.tfvars — creating/finding engine..."
  PYTHON_CMD="python3"
  if command -v uv &> /dev/null; then
    PYTHON_CMD="uv run python"
  fi

  REASONING_ENGINE_ID=$(cd "${APP_DIR}" && ${PYTHON_CMD} "${SCRIPT_DIR}/create_reasoning_engine.py" \
    --project "${PROJECT_ID}" \
    --location "${REGION}" \
    --staging-bucket "gs://${GCS_STAGING}" \
    --display-name "aura-sessions")

  if [[ -z "${REASONING_ENGINE_ID}" ]]; then
    echo "  ERROR: Failed to create Reasoning Engine. Aborting."
    exit 1
  fi

  echo "  Engine ID: ${REASONING_ENGINE_ID}"
  # Persist into terraform.tfvars so subsequent runs skip creation
  if grep -q '^reasoning_engine_id' "${SCRIPT_DIR}/terraform.tfvars"; then
    sed -i "s|^reasoning_engine_id.*|reasoning_engine_id = \"${REASONING_ENGINE_ID}\"|" \
      "${SCRIPT_DIR}/terraform.tfvars"
  else
    echo "" >> "${SCRIPT_DIR}/terraform.tfvars"
    echo "reasoning_engine_id = \"${REASONING_ENGINE_ID}\"" >> "${SCRIPT_DIR}/terraform.tfvars"
  fi
  echo "  Saved to terraform.tfvars."
fi

# ── Store secrets in Secret Manager ─────────────────────────────────────────
echo ""
echo "==> Secret setup"
echo "  Enter LiveKit credentials to store in Secret Manager."
echo "  (Skip with Ctrl+C if already stored, then re-run.)"

read -rsp "  LIVEKIT_API_KEY: " LK_KEY && echo
read -rsp "  LIVEKIT_API_SECRET: " LK_SECRET && echo

echo -n "${LK_KEY}" | gcloud secrets versions add "aura-livekit-api-key" \
  --data-file=- --project="${PROJECT_ID}" 2>/dev/null || \
  (gcloud secrets create "aura-livekit-api-key" --replication-policy=automatic \
    --project="${PROJECT_ID}" && \
   echo -n "${LK_KEY}" | gcloud secrets versions add "aura-livekit-api-key" \
     --data-file=- --project="${PROJECT_ID}")

echo -n "${LK_SECRET}" | gcloud secrets versions add "aura-livekit-api-secret" \
  --data-file=- --project="${PROJECT_ID}" 2>/dev/null || \
  (gcloud secrets create "aura-livekit-api-secret" --replication-policy=automatic \
    --project="${PROJECT_ID}" && \
   echo -n "${LK_SECRET}" | gcloud secrets versions add "aura-livekit-api-secret" \
     --data-file=- --project="${PROJECT_ID}")

echo "  Secrets stored."

# ── Terraform init + apply ────────────────────────────────────────────────────
echo ""
echo "==> Terraform init"
cd "${SCRIPT_DIR}"
terraform init -upgrade

echo ""
echo "==> Terraform apply"
terraform apply -auto-approve

# ── Build + push Docker image ─────────────────────────────────────────────────
echo ""
echo "==> Building Docker image"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/aura"
IMAGE_URL="${REGISTRY}/aura-backend:latest"

gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build -t "${IMAGE_URL}" "${APP_DIR}"
docker push "${IMAGE_URL}"

# ── Deploy updated image to Cloud Run ────────────────────────────────────────
echo ""
echo "==> Deploying to Cloud Run"
gcloud run services update aura-sde-interview-agent \
  --image="${IMAGE_URL}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
SERVICE_URL=$(gcloud run services describe aura-sde-interview-agent \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format='value(status.url)')
echo "=========================================="
echo "  Aura — Google SDE Interview Coach is live at: ${SERVICE_URL}"
echo "=========================================="
