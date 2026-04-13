variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Google Cloud region for all resources"
  type        = string
  default     = "us-central1"
}

variable "image_tag" {
  description = "Container image tag to deploy (e.g. 'latest' or a Git SHA)"
  type        = string
  default     = "latest"
}

variable "livekit_url" {
  description = "LiveKit server WebSocket URL (wss://...)"
  type        = string
}

variable "livekit_room_prefix" {
  description = "Prefix used when generating LiveKit room names"
  type        = string
  default     = "aura-interview"
}

variable "gemini_live_model" {
  description = "Gemini Live model name"
  type        = string
  default     = "gemini-live-2.5-flash-native-audio"
}

variable "gemini_text_model" {
  description = "Gemini text model used for grading and narrative summaries"
  type        = string
  default     = "gemini-2.5-flash"
}

variable "gemini_voice" {
  description = "Gemini voice name for audio responses"
  type        = string
  default     = "Aoede"
}

variable "cloud_run_max_instances" {
  description = "Maximum number of Cloud Run instances"
  type        = number
  default     = 10
}

variable "cloud_run_min_instances" {
  description = "Minimum number of Cloud Run instances (0 = scale to zero, 1 = always warm)"
  type        = number
  default     = 1
}

variable "cloud_run_cpu" {
  description = "Cloud Run vCPU allocation"
  type        = string
  default     = "2"
}

variable "cloud_run_memory" {
  description = "Cloud Run memory limit"
  type        = string
  default     = "2Gi"
}

variable "enable_custom_domain" {
  description = "Set to true to provision the Global HTTPS Load Balancer for aura.veloxpro.in"
  type        = bool
  default     = false
}

variable "custom_domain" {
  description = "Custom domain to map to the Cloud Run service (e.g. aura.veloxpro.in)"
  type        = string
  default     = "aura.veloxpro.in"
}

variable "reasoning_engine_id" {
  description = "Numeric ID of the Vertex AI Reasoning Engine used for cross-deployment session persistence. Created automatically by deploy.sh if not set. Set to empty string to disable (falls back to FileSessionService or InMemory)."
  type        = string
  default     = ""
}

variable "gcs_staging_bucket" {
  description = "GCS bucket name (no gs:// prefix) used as staging area when creating the Vertex AI Reasoning Engine. Only needed if reasoning_engine_id is empty."
  type        = string
  default     = ""
}
