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

  
  # initial_node_count=1 is required by GKE even when removing the pool.
  remove_default_node_pool = true
  initial_node_count       = 1


  # remove_default_node_pool) to use pd-standard so it never touches SSD quota.
  node_config {
    machine_type = "e2-standard-4"
    disk_type    = "pd-standard"
    disk_size_gb = 50
  }

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

  # Pin to a single zone so node_count is literal, not per-zone.
  # Without this, a regional cluster (us-central1) creates node_count nodes
  # in EACH of the 3 zones — turning node_count=1 into 3 actual VMs (12 vCPU).
  node_locations = ["us-central1-a"]

  # 1 node = 4 vCPU in us-central1-a.
  # Autoscaler can add a second node (8 vCPU total) under peak load.
  node_count = 1

  node_config {
    # e2-custom-4-32768 = 4 vCPU · 32GB RAM
    machine_type = "e2-custom-4-32768"
    disk_size_gb = 100
    disk_type    = "pd-standard"   # HDD — avoids SSD_TOTAL_GB quota entirely

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }

  autoscaling {
    min_node_count = 1
    max_node_count = 3
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  # node_count is adjusted manually via `gcloud container clusters resize`
  # during experiments. Ignore drift so Terraform doesn't fight those changes.
  lifecycle {
    ignore_changes = [node_count]
  }

  depends_on = [google_container_cluster.turtlecrawl]
}

# ─── NATS Node Pool ──────────────────────────────────────────────────────────
# Dedicated pool for the 3-node NATS JetStream cluster.
# Tainted nats=true:NoSchedule so only NATS pods land here.
# pd-ssd disks give JetStream the fsync latency it needs for high throughput
# (~0.1–0.5ms vs 5–20ms on pd-standard HDD).

resource "google_container_node_pool" "nats" {
  name       = "nats-pool"
  cluster    = google_container_cluster.turtlecrawl.name
  location   = var.region

  # Pin to the same zone as the app pool so inter-node traffic stays free
  # (cross-zone egress is charged). Without node_locations, node_count=3 in a
  # regional cluster creates 3 nodes PER ZONE = 9 VMs = 18 vCPU — over quota.
  node_locations = ["us-central1-a"]

  # Fixed at 3 — exactly one node per NATS replica, all in us-central1-a.
  # No autoscaling: GKE must not evict a NATS node mid-operation as that
  # would break JetStream quorum (R3 needs all 3 nodes for writes to succeed).
  node_count = 3

  node_config {
    machine_type = "e2-standard-2"   # 2 vCPU · 8 GB — fits NATS + JetStream buffer
    disk_size_gb = 50
    disk_type    = "pd-ssd"          # Low-latency SSD for JetStream WAL writes

    # Taint prevents non-NATS workloads from landing on these nodes.
    taint {
      key    = "nats"
      value  = "true"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
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
  force_destroy = true   # allows terraform destroy even when audit objects exist

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
