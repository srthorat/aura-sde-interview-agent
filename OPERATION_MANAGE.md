# Aura Operations Runbook

This document is the day-2 operations guide for Aura on Google Cloud.

It covers:
- Initial Terraform deployment
- Connecting GitHub to Cloud Build in the GCP UI
- Finishing CI/CD trigger setup with Terraform
- Custom domain setup with GoDaddy
- Upgrade, stop, restart, and delete operations

## 1. Current Deployment State

Current project:
- `project-dcfc3a62-8889-44a3-ad9`

Current region:
- `us-central1`

Current Artifact Registry repository:
- `us-central1-docker.pkg.dev/project-dcfc3a62-8889-44a3-ad9/aura`

Current Cloud Run service:
- `aura-sde-interview-agent`

Current public URL:
- `https://aura-sde-interview-agent-ivhauk7c7a-uc.a.run.app`

## 2. Required Local Setup

Run all commands from the repo root unless noted.

Required local tools:
- `gcloud`
- `terraform`
- `docker`

Recommended shell setup:

```bash
set -euo pipefail

cd /home/ubuntu/velox/aura-sde-interview-agent

export GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/.config/gcp/aura-deploy.json
export PROJECT_ID=project-dcfc3a62-8889-44a3-ad9
export REGION=us-central1
export IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/aura/aura-backend:latest"
```

Verify auth:

```bash
gcloud auth list
gcloud config set project "$PROJECT_ID"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
```

## 3. Terraform Deploy Commands

Terraform values are stored in:
- [infra/terraform.tfvars](infra/terraform.tfvars)

Initialize Terraform:

```bash
cd infra
terraform init -upgrade
```

Review plan:

```bash
terraform plan -no-color
```

Apply all infrastructure:

```bash
terraform apply -auto-approve
```

Show outputs:

```bash
terraform output
```

Important note:
- Cloud Run is already live.
- The remaining optional Terraform item is the Cloud Build GitHub trigger.
- That trigger will fail until the GitHub repository is connected in the GCP UI.

## 4. Manual Image Build And Push

Build and push the current Docker image:

```bash
cd /home/ubuntu/velox/aura-sde-interview-agent

docker build -t "$IMAGE_URL" .
docker push "$IMAGE_URL"
```

If you already built with `docker compose`, retag and push:

```bash
docker tag aura-sde-interview-agent-aura:latest "$IMAGE_URL"
docker push "$IMAGE_URL"
```

## 5. Update Cloud Run To A New Image

If you want to push a new image immediately without waiting for CI/CD:

```bash
gcloud run services update aura-sde-interview-agent \
  --image="$IMAGE_URL" \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

Verify service status:

```bash
gcloud run services describe aura-sde-interview-agent \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --format='yaml(status.url,status.latestReadyRevisionName,status.traffic)'
```

Verify health endpoint:

```bash
curl -fsS https://aura-sde-interview-agent-ivhauk7c7a-uc.a.run.app/health
```

## 6. Connect GitHub To Cloud Build In GCP UI

This is required before `terraform apply` can create the GitHub-based Cloud Build trigger.

### What you are connecting

Repository:
- `srthorat/aura-sde-interview-agent`

Branch to watch:
- `main`

Build config file:
- `cloudbuild.yaml`

### UI steps

1. Open Google Cloud Console.
2. Make sure the selected project is `project-dcfc3a62-8889-44a3-ad9`.
3. In the left menu, open `Cloud Build`.
4. Open `Repositories`.
5. Click `Connect repository`.
6. Choose `GitHub` as the source provider.
7. If prompted, click `Authorize` and sign in to the GitHub account that owns `srthorat/aura-sde-interview-agent`.
8. Approve the Google Cloud Build GitHub App installation.
9. During installation, either:
   - grant access to all repositories, or
   - grant access specifically to `aura-sde-interview-agent`
10. Return to GCP.
11. Select the GitHub account/organization that contains `srthorat/aura-sde-interview-agent`.
12. Select the repository `aura-sde-interview-agent`.
13. Choose the region if the UI asks for one. Use `us-central1` if it requires a connection region.
14. Complete the repository connection flow.

### What to verify after connecting

You should see the repository listed under `Cloud Build` -> `Repositories`.

If the UI shows a connection name, keep it. Terraform uses the older GitHub trigger block here, but the important part is that the project must already be authorized to access the repo.

### After the UI connection is done

Run:

```bash
cd /home/ubuntu/velox/aura-sde-interview-agent/infra
export GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/.config/gcp/aura-deploy.json
terraform apply -auto-approve
```

Then verify triggers:

```bash
gcloud builds triggers list --project="$PROJECT_ID"
```

## 7. GoDaddy Custom Domain Setup

This repo already has Terraform support for a custom domain fronted by a Global HTTPS Load Balancer.

Current Terraform variables:
- `enable_custom_domain`
- `custom_domain`

Current configured domain value:
- `aura.veloxpro.in`

### Step 1. Enable the custom domain in Terraform

Edit [infra/terraform.tfvars](infra/terraform.tfvars):

```hcl
enable_custom_domain = true
custom_domain        = "aura.veloxpro.in"
```

### Step 2. Apply Terraform

```bash
cd /home/ubuntu/velox/aura-sde-interview-agent/infra
export GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/.config/gcp/aura-deploy.json
terraform apply -auto-approve
```

### Step 3. Get the load balancer IP

```bash
terraform output lb_ip
```

### Step 4. Add DNS in GoDaddy

In GoDaddy:

1. Open `My Products`.
2. Open the DNS page for `veloxpro.in`.
3. Add a new DNS record.
4. Set:
   - Type: `A`
   - Name: `aura`
   - Value: the IP from `terraform output lb_ip`
   - TTL: default or `600`
5. Save the record.

### Step 5. Wait for certificate provisioning

Google-managed certificate issuance usually takes several minutes after DNS propagates.

Check status:

```bash
gcloud compute ssl-certificates describe aura-ssl-cert \
  --global \
  --project="$PROJECT_ID" \
  --format='value(managed.status,managed.domainStatus)'
```

When active, open:
- `https://aura.veloxpro.in`

## 8. Day-2 Operations

### View Cloud Run service status

```bash
gcloud run services describe aura-sde-interview-agent \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

### View recent Cloud Run logs

```bash
gcloud run services logs read aura-sde-interview-agent \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --limit=200
```

### View revisions

```bash
gcloud run revisions list \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --service=aura-sde-interview-agent
```

### Roll traffic to latest revision

```bash
gcloud run services update-traffic aura-sde-interview-agent \
  --to-latest \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

## 9. Upgrade Procedure

Use this when you want to deploy a new application version.

### Option A. Manual upgrade now

```bash
set -euo pipefail

cd /home/ubuntu/velox/aura-sde-interview-agent

export GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/.config/gcp/aura-deploy.json
export PROJECT_ID=project-dcfc3a62-8889-44a3-ad9
export REGION=us-central1
export IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/aura/aura-backend:latest"

docker build -t "$IMAGE_URL" .
docker push "$IMAGE_URL"

gcloud run services update aura-sde-interview-agent \
  --image="$IMAGE_URL" \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

### Option B. Terraform-driven reconcile

Use this when infrastructure also changed:

```bash
cd /home/ubuntu/velox/aura-sde-interview-agent/infra
export GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/.config/gcp/aura-deploy.json
terraform plan -no-color
terraform apply -auto-approve
```

## 10. Stop And Restart Guidance

Cloud Run does not have a normal VM-style stop/start lifecycle.

Use one of these patterns instead.

### Lowest cost, keep service available on demand

Set minimum instances to zero in [infra/terraform.tfvars](infra/terraform.tfvars):

```hcl
# add if not present
cloud_run_min_instances = 0
```

Then apply:

```bash
cd /home/ubuntu/velox/aura-sde-interview-agent/infra
export GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/.config/gcp/aura-deploy.json
terraform apply -auto-approve
```

This does not stop the service permanently. It allows scale-to-zero.

### Temporarily block public traffic

```bash
gcloud run services remove-iam-policy-binding aura-sde-interview-agent \
  --member=allUsers \
  --role=roles/run.invoker \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

Restore public access:

```bash
gcloud run services add-iam-policy-binding aura-sde-interview-agent \
  --member=allUsers \
  --role=roles/run.invoker \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

### Force a fresh rollout restart

```bash
gcloud run services update aura-sde-interview-agent \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --set-env-vars=ROLLING_RESTART_TS="$(date +%s)"
```

## 11. Delete Procedure

Delete the Cloud Run service only:

```bash
gcloud run services delete aura-sde-interview-agent \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --quiet
```

Delete all Terraform-managed infrastructure:

```bash
cd /home/ubuntu/velox/aura-sde-interview-agent/infra
export GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/.config/gcp/aura-deploy.json
terraform destroy -auto-approve
```

Warning:
- `terraform destroy` removes Cloud Run, Artifact Registry repo, IAM bindings, Secret Manager secret containers, the Cloud Build trigger, and optional load balancer resources.
- Secret versions and retained artifacts may still need manual review depending on GCP behavior and provider settings.

## 12. Known Follow-Up Item

Current remaining setup item:
- Connect GitHub to Cloud Build in the GCP UI, then rerun `terraform apply`

After that, pushes to `main` can create a normal CI/CD flow through `cloudbuild.yaml`.