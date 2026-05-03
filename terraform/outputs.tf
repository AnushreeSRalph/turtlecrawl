output "registry_url" {
  description = "Docker registry URL — use this in Makefile REGISTRY"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/turtlecrawl"
}

output "audit_bucket" {
  description = "GCS bucket for agent audit logs"
  value       = google_storage_bucket.audit_logs.name
}

output "agent_sa_email" {
  description = "GCP service account email for the agent (in-cluster use)"
  value       = google_service_account.agent.email
}

output "kubeconfig_command" {
  description = "Run this to connect kubectl to your cluster"
  value       = "gcloud container clusters get-credentials ${var.cluster_name} --region ${var.region} --project ${var.project_id}"
}

output "docker_auth_command" {
  description = "Run this to authenticate Docker to Artifact Registry"
  value       = "gcloud auth configure-docker ${var.region}-docker.pkg.dev"
}
