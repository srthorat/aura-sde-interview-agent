terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # Uncomment to use GCS backend for shared state (recommended for team use)
  # backend "gcs" {
  #   bucket = "your-terraform-state-bucket"
  #   prefix = "aura-solution4"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
