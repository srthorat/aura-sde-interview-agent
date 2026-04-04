# ── APIs ─────────────────────────────────────────────────────────────────────
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "compute.googleapis.com",
    "certificatemanager.googleapis.com",
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# ── Artifact Registry ─────────────────────────────────────────────────────────
resource "google_artifact_registry_repository" "aura" {
  project       = var.project_id
  location      = var.region
  repository_id = "aura"
  format        = "DOCKER"
  description   = "Aura SDE Interview Agent container images"
  depends_on    = [google_project_service.apis]
}

locals {
  image_url = "${var.region}-docker.pkg.dev/${var.project_id}/aura/aura-backend:${var.image_tag}"
}

# ── Service Account ───────────────────────────────────────────────────────────
resource "google_service_account" "aura_run" {
  project      = var.project_id
  account_id   = "aura-cloud-run"
  display_name = "Aura SDE Interview Agent Service Account"
}

# Allow Cloud Run SA to use Vertex AI
resource "google_project_iam_member" "aura_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.aura_run.email}"
}

# Allow Cloud Run SA to access Secret Manager
resource "google_project_iam_member" "aura_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.aura_run.email}"
}

# ── Secrets ───────────────────────────────────────────────────────────────────
resource "google_secret_manager_secret" "livekit_api_key" {
  project   = var.project_id
  secret_id = "aura-livekit-api-key"
  replication { auto {} }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "livekit_api_secret" {
  project   = var.project_id
  secret_id = "aura-livekit-api-secret"
  replication { auto {} }
  depends_on = [google_project_service.apis]
}

# ── Cloud Run Service ─────────────────────────────────────────────────────────
resource "google_cloud_run_v2_service" "aura" {
  project  = var.project_id
  name     = "aura-sde-interview-agent"
  location = var.region

  template {
    service_account = google_service_account.aura_run.email

    scaling {
      min_instance_count = var.cloud_run_min_instances
      max_instance_count = var.cloud_run_max_instances
    }

    containers {
      image = local.image_url

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
        cpu_idle          = false  # CPU always allocated — no throttle between requests
        startup_cpu_boost = true   # Extra CPU burst during container startup
      }

      # ── Non-secret env vars ────────────────────────────────────────────────
      env {
        name  = "LIVEKIT_URL"
        value = var.livekit_url
      }
      env {
        name  = "LIVEKIT_ROOM_PREFIX"
        value = var.livekit_room_prefix
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GEMINI_LIVE_MODEL"
        value = var.gemini_live_model
      }
      env {
        name  = "GEMINI_VOICE"
        value = var.gemini_voice
      }
      env {
        name  = "PORT"
        value = "7862"
      }

      # ── Secret env vars ────────────────────────────────────────────────────
      env {
        name = "LIVEKIT_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.livekit_api_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "LIVEKIT_API_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.livekit_api_secret.secret_id
            version = "latest"
          }
        }
      }

      ports {
        container_port = 7862
      }

      startup_probe {
        http_get { path = "/health" }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 10
      }

      liveness_probe {
        http_get { path = "/health" }
        period_seconds    = 30
        failure_threshold = 3
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# ── Public HTTP access ────────────────────────────────────────────────────────
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.aura.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ── Cloud Build trigger ───────────────────────────────────────────────────────
# The trigger watches the solution4/ folder on the main branch.
# It builds the Docker image and deploys it to Cloud Run automatically.
resource "google_cloudbuild_trigger" "aura_deploy" {
  project     = var.project_id
  name        = "aura-deploy-on-push"
  description = "Build and deploy Aura on push to main"
  location    = "global"

  github {
    owner = "your-github-org"   # Update before applying
    name  = "velox-voice-ai-agent"
    push {
      branch = "^main$"
    }
  }

  included_files = ["solution4/**"]

  filename   = "solution4/cloudbuild.yaml"
  depends_on = [google_project_service.apis]
}

# ── Custom domain: aura.veloxpro.in via Global HTTPS Load Balancer ────────────
# Set var.enable_custom_domain = true to provision these resources.
# After `terraform apply` run:
#   terraform output lb_ip
# then add an A record in GoDaddy: aura → <lb_ip>

# ── Static global IP ──────────────────────────────────────────────────────────
resource "google_compute_global_address" "aura_lb_ip" {
  count   = var.enable_custom_domain ? 1 : 0
  project = var.project_id
  name    = "aura-lb-ip"
}

# ── Google-managed SSL certificate ───────────────────────────────────────────
resource "google_compute_managed_ssl_certificate" "aura" {
  count   = var.enable_custom_domain ? 1 : 0
  project = var.project_id
  name    = "aura-ssl-cert"

  managed {
    domains = [var.custom_domain]
  }

  depends_on = [google_project_service.apis]
}

# ── Serverless NEG pointing at the Cloud Run service ─────────────────────────
resource "google_compute_region_network_endpoint_group" "aura" {
  count                 = var.enable_custom_domain ? 1 : 0
  project               = var.project_id
  name                  = "aura-serverless-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.aura.name
  }

  depends_on = [google_project_service.apis]
}

# ── Backend service ───────────────────────────────────────────────────────────
resource "google_compute_backend_service" "aura" {
  count                   = var.enable_custom_domain ? 1 : 0
  project                 = var.project_id
  name                    = "aura-backend-service"
  protocol                = "HTTPS"
  port_name               = "https"
  load_balancing_scheme   = "EXTERNAL_MANAGED"
  enable_cdn              = false

  backend {
    group = google_compute_region_network_endpoint_group.aura[0].id
  }

  depends_on = [google_project_service.apis]
}

# ── URL map ───────────────────────────────────────────────────────────────────
resource "google_compute_url_map" "aura" {
  count           = var.enable_custom_domain ? 1 : 0
  project         = var.project_id
  name            = "aura-url-map"
  default_service = google_compute_backend_service.aura[0].id
}

# ── HTTPS target proxy ────────────────────────────────────────────────────────
resource "google_compute_target_https_proxy" "aura" {
  count            = var.enable_custom_domain ? 1 : 0
  project          = var.project_id
  name             = "aura-https-proxy"
  url_map          = google_compute_url_map.aura[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.aura[0].id]
}

# ── Forwarding rule (HTTPS :443) ──────────────────────────────────────────────
resource "google_compute_global_forwarding_rule" "aura_https" {
  count                 = var.enable_custom_domain ? 1 : 0
  project               = var.project_id
  name                  = "aura-https-forwarding"
  target                = google_compute_target_https_proxy.aura[0].id
  ip_address            = google_compute_global_address.aura_lb_ip[0].address
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# ── HTTP → HTTPS redirect ─────────────────────────────────────────────────────
resource "google_compute_url_map" "aura_http_redirect" {
  count   = var.enable_custom_domain ? 1 : 0
  project = var.project_id
  name    = "aura-http-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "aura_redirect" {
  count   = var.enable_custom_domain ? 1 : 0
  project = var.project_id
  name    = "aura-http-redirect-proxy"
  url_map = google_compute_url_map.aura_http_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "aura_http_redirect" {
  count                 = var.enable_custom_domain ? 1 : 0
  project               = var.project_id
  name                  = "aura-http-redirect-forwarding"
  target                = google_compute_target_http_proxy.aura_redirect[0].id
  ip_address            = google_compute_global_address.aura_lb_ip[0].address
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}
