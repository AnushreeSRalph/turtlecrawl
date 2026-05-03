terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ─── APIs ────────────────────────────────────────────────────────────────────

resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "storage.googleapis.com",
    "artifactregistry.googleapis.com",
    "monitoring.googleapis.com",
    "aiplatform.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ─── GKE Standard Cluster ────────────────────────────────────────────────────

resource "google_container_cluster" "turtlecrawl" {
  name     = var.cluster_name
  location = var.region

  # Remove default node pool — we manage our own below
  remove_default_node_pool = true
  initial_node_count       = 1

  # Allow terraform destroy to delete the cluster
  deletion_protection = false

  release_channel {
    channel = "REGULAR"
  }

  # Workload Identity — lets pods use GCP service accounts without key files
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  depends_on = [google_project_service.apis]
}

resource "google_container_node_pool" "turtlecrawl" {
  name       = "turtlecrawl-pool"
  cluster    = google_container_cluster.turtlecrawl.name
  location   = var.region

  # 2 nodes — enough for Prometheus + sample-app + agent with headroom
  node_count = 2

  node_config {
    machine_type = "e2-standard-4"   # 4 vCPU · 16GB RAM · ~$0.13/hr per node
    disk_size_gb = 50
    disk_type    = "pd-standard"     # cheaper than SSD, fine for this workload

    # Workload Identity on nodes
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }

  autoscaling {
    min_node_count = 1
    max_node_count = 4
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  depends_on = [google_container_cluster.turtlecrawl]
}

# ─── Artifact Registry ───────────────────────────────────────────────────────

resource "google_artifact_registry_repository" "turtlecrawl" {
  location      = var.region
  repository_id = "turtlecrawl"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# ─── GCS Audit Log Bucket ────────────────────────────────────────────────────

resource "google_storage_bucket" "audit_logs" {
  name          = "${var.project_id}-turtlecrawl-audit"
  location      = var.region
  force_destroy = false

  lifecycle_rule {
    condition { age = 90 }
    action { type = "Delete" }
  }

  uniform_bucket_level_access = true
  depends_on = [google_project_service.apis]
}

# ─── Artifact Registry access for GKE nodes ─────────────────────────────────
# Grants the default compute SA permission to pull images — prevents 403 on image pull.

data "google_project" "project" {
  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "registry_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
  depends_on = [google_project_service.apis]
}

# ─── Agent GCP Service Account ───────────────────────────────────────────────
# Used when the agent runs as a k8s Job in-cluster.
# For local dev, your personal ADC (gcloud auth application-default login) is enough.

resource "google_service_account" "agent" {
  account_id   = "turtlecrawl-agent"
  display_name = "turtlecrawl scaling agent"
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset([
    "roles/storage.objectAdmin",  # write audit logs to GCS
    "roles/monitoring.viewer",    # read Cloud Monitoring metrics
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# Bind the k8s ServiceAccount (turtlecrawl/turtlecrawl-agent) to the GCP SA
# so the agent Pod can authenticate to GCP without a key file.
# Must depend on the cluster — the Workload Identity pool only exists after GKE is created.
resource "google_service_account_iam_member" "agent_workload_identity" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[turtlecrawl/turtlecrawl-agent]"
  depends_on         = [google_container_cluster.turtlecrawl]
}
