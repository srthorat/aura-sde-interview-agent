#!/usr/bin/env bash
# ── Aura SDE Interview Agent — One-click deploy ──────────────────────────────
# Usage:
#   cd infra
#   cp terraform.tfvars.example terraform.tfvars   # fill in your values
#   ./deploy.sh
#
# Prerequisites:
#   - gcloud CLI authenticated: gcloud auth application-default login
#   - Terraform >= 1.7 installed
#   - docker CLI installed (for initial image push if trigger isn't set up yet)
#
# What this script does:
#   1. Enables required GCP APIs
#   2. Creates Artifact Registry, Cloud Run service, IAM, Secret Manager secrets
#   3. Builds and pushes the Docker image to Artifact Registry
#   4. Deploys to Cloud Run
#   5. Prints the live service URL
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

echo "==> Project: ${PROJECT_ID}  Region: ${REGION}"

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
echo "  Aura SDE Interview Agent is live at: ${SERVICE_URL}"
echo "=========================================="
