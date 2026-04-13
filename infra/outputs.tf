output "cloud_run_url" {
  description = "HTTPS URL of the deployed Aura — Google SDE Interview Coach Cloud Run service"
  value       = google_cloud_run_v2_service.aura.uri
}

output "artifact_registry_repo" {
  description = "Full Artifact Registry repository path"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/aura"
}

output "service_account_email" {
  description = "Email of the Cloud Run service account"
  value       = google_service_account.aura_run.email
}

output "reasoning_engine_id" {
  description = "Numeric ID of the Vertex AI Reasoning Engine used for session persistence"
  value       = var.reasoning_engine_id
}

output "lb_ip" {
  description = "Global static IP for the HTTPS Load Balancer — add this as an A record: aura-sde-interview-agent → <lb_ip>"
  value       = var.enable_custom_domain ? google_compute_global_address.aura_lb_ip[0].address : "custom domain not enabled (set enable_custom_domain = true)"
}
