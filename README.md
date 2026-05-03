# turtlecrawl 🐢

> LLM-driven Kubernetes scaling agent that autonomously runs load tests and scales pods based on custom metrics — using Gemini 2.5 Flash on Vertex AI.

---

## Architecture

```
┌─────────────────────────────┐     ┌──────────────────────────────────┐
│       YOUR MACHINE          │     │              GCP                  │
│                             │     │                                   │
│  ┌─────────────────────┐    │     │  ┌───────────────────────────┐   │
│  │   Python Agent      │────┼─LLM─┼─▶│  Vertex AI                │   │
│  │   Gemini tool-use   │    │     │  │  Gemini 2.5 Flash         │   │
│  │   Scale-down gate   │    │     │  └───────────────────────────┘   │
│  └────────┬────────────┘    │     │                                   │
│           │ subprocess      │     │  ┌───────────────────────────┐   │
│  ┌────────▼────────────┐    │     │  │  GKE Standard             │   │
│  │   Go Collector      │────┼─────┼─▶│  ├─ Prometheus            │   │
│  │   get-scale-metrics │    │port │  │  ├─ sample-app (Flask)    │   │
│  │   scale / replicas  │    │fwd  │  │  ├─ k6 pod (ephemeral)   │   │
│  └─────────────────────┘    │     │  └─ pods (scale up/down)    ┘   │
└─────────────────────────────┘     │                                   │
          gcloud ADC auth           │  ┌──────────┐  ┌─────────────┐  │
          no API keys needed        │  │ GCS      │  │ Artifact    │  │
                                    │  │ Audit log│  │ Registry    │  │
                                    │  └──────────┘  └─────────────┘  │
                                    └──────────────────────────────────┘
```

## Stack

| Component | Choice |
|-----------|--------|
| Cluster | GKE Standard (e2-standard-4, autoscaling 1–4 nodes) |
| Metrics | Prometheus (kube-prometheus-stack via Helm) |
| Load test | k6 (runs as in-cluster pod — no port-forward bottleneck) |
| Agent brain | Python + Gemini 2.5 Flash (Vertex AI) |
| K8s / metrics tools | Go binary (client-go + Prometheus HTTP API) |
| Auth | gcloud ADC — no long-lived keys |
| Audit log | JSON → GCS bucket |

## Repo Structure

```
turtlecrawl/
├── terraform/          # GKE Standard + Artifact Registry + GCS
├── collector/          # Go binary — metrics, scaling, pod status
├── agent/              # Python — Gemini tool-use loop
├── sample-app/         # Flask app with Prometheus metrics (scaling target)
├── k8s/                # Kubernetes manifests
├── loadtest/           # k6 load test script
├── Makefile            # all commands
└── SETUP.md            # step-by-step setup guide
```

## Quick Start

### Prerequisites

```bash
brew install terraform kubectl helm go k6
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### 1 — Provision GKE Cluster

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Set project_id in terraform.tfvars

terraform init && terraform apply
```

### 2 — Connect kubectl + install Prometheus

```bash
make cluster-auth
make install-prometheus
```

### 3 — Deploy Sample App

```bash
make build-sample-app      # builds linux/amd64 image, pushes to Artifact Registry
make deploy-sample-app
```

### 4 — Build Go Collector

```bash
make build-collector
make test-collector        # verify metrics + scaling work
```

### 5 — Start Port-Forward

```bash
make prometheus-forward    # Prometheus at localhost:9090 (keep running in a separate tab)
```

### 6 — Run the Agent

```bash
make install-agent

make run-agent-dry         # dry-run — logs every decision, no scaling executed
make run-agent-incluster   # live — k6 runs inside cluster, agent scales sample-app
```

## Scale-Down Gate

The agent checks **all six conditions** before removing a replica:

| Check | Threshold |
|-------|-----------|
| `active_connections` | == 0 |
| `http_requests_in_flight` | < 2 |
| `p99 latency` | < 120ms |
| `per-pod RPS after removal` | < max_rps_per_pod |
| `5xx error rate` | < 1% |
| `all surviving pods Ready` | true |

## Agent Loop

```
get_scale_metrics() → get_replicas() → run_load_test(vus=500)
         ↓
   SLOs breached?
   YES → scale_deployment(up)  → wait() → get_scale_metrics()
   NO  → gate passed?
         YES → scale_deployment(down) → wait() → verify
         NO  → hold, report
```

## Example Run

Real run — k6 load generated inside the cluster, Gemini reasoning over live Prometheus metrics.

```
$ make run-agent-incluster

============================================================
  turtlecrawl scaling agent (Gemini on Vertex AI)
  run_id   : run-1777788508
  project  : vertex-ai-proj  region: us-central1
  model    : gemini-2.5-flash
  target   : sample-app/turtlecrawl
  load     : 500 VUs
  dry_run  : False
============================================================

── Turn 1 ──────────────────────────────
[tool] get_scale_metrics({})
  → {"active_connections": 1, "p99_latency_ms": 4.95, "rps_total": 1.78, "error_rate_5xx": 0}

[tool] get_replicas({"deployment": "sample-app", "namespace": "turtlecrawl"})
  → {"replicas": 1, "ready": 1, "available": 1}

[agent] Baseline: 1 replica, p99=4.95ms, 0 errors. Running load test at 500 VUs...

── Turn 2 ──────────────────────────────
[tool] run_load_test({"vus": 500, "duration_seconds": 60,
                      "base_url": "http://sample-app.turtlecrawl.svc.cluster.local:8080"})
  [wait] Sleeping 60s — Waiting for load test to complete and metrics to stabilize.

── Turn 8 ──────────────────────────────
[tool] get_scale_metrics({})
  → {"active_connections": 1, "p99_latency_ms": 122.35, "rps_total": 49.44, "error_rate_5xx": 0}

[agent] P99 latency SLO breached (122.35ms > 120ms). Scaling up to 2 replicas.

── Turn 9 ──────────────────────────────
[tool] scale_deployment({"replicas": 2,
       "reason": "P99 latency SLO breached (122.35ms > 120ms) with 1 replica."})
  → {"old_replicas": 1, "new_replicas": 2, "status": "scaled"}

  [wait] Sleeping 30s — Waiting for new replica to become ready.

── Turn 11 ──────────────────────────────
[tool] get_replicas({"deployment": "sample-app", "namespace": "turtlecrawl"})
  → {"replicas": 2, "ready": 2, "available": 2}

[agent] 2/2 pods ready. Re-running load test at 500 VUs...

── Turn 14 ──────────────────────────────
[tool] get_scale_metrics({})
  → {"active_connections": 2, "p99_latency_ms": 98.87, "rps_total": 105.39, "error_rate_5xx": 0}

[agent] SLOs met at 2 replicas (p99=98.87ms ✓, error_rate=0% ✓).
        Scale-down gate check → in_flight=2 (needs <2), projected RPS/pod=105 (limit 100).
        Gate BLOCKED — cannot safely reduce to 1 replica.

✓ Agent completed.

── Run Summary ──────────────────────────────
{
  "run_id":          "run-1777788508",
  "total_events":    20,
  "scale_decisions": 1,
  "scale_ups":       1,
  "scale_downs":     0
}

Recommendation: 2 replicas minimum under 500 VUs — p99 98ms, error rate 0%.
```

## Make Commands

| Command | What it does |
|---------|-------------|
| `make infra` | Terraform apply |
| `make cluster-auth` | Connect kubectl to cluster |
| `make install-prometheus` | Helm install kube-prometheus-stack |
| `make prometheus-forward` | Port-forward Prometheus to :9090 |
| `make build-sample-app` | Build + push Docker image (linux/amd64) |
| `make deploy-sample-app` | kubectl apply all manifests |
| `make build-collector` | Compile Go collector binary |
| `make test-collector` | Smoke-test collector against live cluster |
| `make install-agent` | Create venv + pip install |
| `make run-agent-dry` | Agent dry-run (no scaling executed) |
| `make run-agent-incluster` | Agent live run with in-cluster k6 load |

## Troubleshooting

**Prometheus not reachable:** Run `make prometheus-forward` before the agent.

**Image pull error on GKE:** Run `make build-sample-app` — the image must be built for `linux/amd64`.

**Collector not found:** Run `make build-collector` then verify with `./collector/bin/collector --help`.

**Vertex AI permission error:** Run `gcloud auth application-default login` to refresh credentials. Confirm the API is enabled: `gcloud services enable aiplatform.googleapis.com --project YOUR_PROJECT_ID`.
