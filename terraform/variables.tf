variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "cluster_name" {
  description = "GKE cluster name"
  type        = string
  default     = "turtlecrawl"
}

variable "gcs_audit_bucket" {
  description = "GCS bucket suffix for agent audit logs"
  type        = string
  default     = "turtlecrawl-audit"
}
