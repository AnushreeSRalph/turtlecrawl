PROJECT_ID ?= $(shell gcloud config get-value project)
REGION     ?= us-central1
CLUSTER    ?= turtlecrawl
NAMESPACE  ?= turtlecrawl
REGISTRY   ?= $(REGION)-docker.pkg.dev/$(PROJECT_ID)/turtlecrawl

.PHONY: help helm-lint helm-template helm-install helm-upgrade helm-uninstall helm-status \
        infra cluster-auth \
        install-prometheus prometheus-forward grafana-forward \
        build-sample-app deploy-sample-app app-forward \
        build-collector test-collector test-mcp-server \
        install-agent run-agent run-agent-dry run-agent-v2 run-agent-v2-dry \
        install-keda deploy-nats init-nats-stream deploy-keda keda-status nats-forward \
        run-loadtest pause resume clean

## ─── Help ───────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  turtlecrawl — local deploy workflow"
	@echo ""
	@echo "  Helm (alternative to kubectl apply)"
	@echo "    make helm-lint           lint the chart"
	@echo "    make helm-template       render templates without applying"
	@echo "    make helm-install        helm upgrade --install (idempotent)"
	@echo "    make helm-upgrade        upgrade an existing release"
	@echo "    make helm-uninstall      remove the release"
	@echo "    make helm-status         show release status"
	@echo ""
	@echo "  Infra"
	@echo "    make infra              provision GKE + GCS + Artifact Registry"
	@echo "    make cluster-auth       connect kubectl to your cluster"
	@echo ""
	@echo "  Observability"
	@echo "    make install-prometheus install kube-prometheus-stack via Helm"
	@echo "    make prometheus-forward port-forward Prometheus to localhost:9090"
	@echo "    make grafana-forward    port-forward Grafana to localhost:3000"
	@echo ""
	@echo "  Sample App"
	@echo "    make build-sample-app   build + push Docker image to Artifact Registry"
	@echo "    make deploy-sample-app  kubectl apply all sample-app manifests"
	@echo "    make app-forward        port-forward sample-app to localhost:8080"
	@echo ""
	@echo "  Collector & Agent (v1)"
	@echo "    make build-collector    compile Go collector binary (CLI + MCP server)"
	@echo "    make install-agent      pip install agent dependencies"
	@echo "    make run-agent-dry      run v1 agent in dry-run"
	@echo "    make run-agent          run v1 agent live"
	@echo "    make test-mcp-server    smoke-test MCP handshake"
	@echo ""
	@echo "  v2 — MCP + NATS + KEDA"
	@echo "    make install-keda            install KEDA operator into cluster"
	@echo "    make deploy-nats             deploy NATS JetStream 3-node cluster"
	@echo "    make migrate-nats-to-cluster tear down single-node + redeploy as cluster (destructive)"
	@echo "    make init-nats-stream        create TURTLECRAWL stream (R3) + keda-consumer"
	@echo "    make deploy-keda             apply KEDA ScaledObject"
	@echo "    make keda-status        show KEDA + HPA + replica status"
	@echo "    make nats-forward       port-forward NATS to localhost:4222"
	@echo "    make run-agent-v2-dry   run v2 MCP agent in dry-run"
	@echo "    make run-agent-v2       run v2 MCP agent live (LLM-supervised KEDA)"
	@echo ""
	@echo "  Load test"
	@echo "    make run-loadtest       run k6 against localhost:8080"
	@echo ""
	@echo "  Cluster management"
	@echo "    make pause              scale down pods + kill port-forwards"
	@echo "    make resume             scale up + restart port-forwards"
	@echo ""

## ─── Helm ────────────────────────────────────────────────────────────────────

HELM_RELEASE  ?= turtlecrawl
HELM_CHART    ?= helm/turtlecrawl
HELM_VALUES   ?= helm/turtlecrawl/values.yaml

helm-lint:
	helm lint $(HELM_CHART) --set global.projectId=$(PROJECT_ID)

helm-template:
	helm template $(HELM_RELEASE) $(HELM_CHART) \
	  --set global.projectId=$(PROJECT_ID) \
	  --set global.region=$(REGION)

helm-install:
	helm upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
	  --namespace $(NAMESPACE) --create-namespace \
	  --set global.projectId=$(PROJECT_ID) \
	  --set global.region=$(REGION) \
	  --wait
	@echo "✓ Helm release $(HELM_RELEASE) installed"

helm-upgrade:
	helm upgrade $(HELM_RELEASE) $(HELM_CHART) \
	  --namespace $(NAMESPACE) \
	  --set global.projectId=$(PROJECT_ID) \
	  --set global.region=$(REGION) \
	  --wait
	@echo "✓ Helm release $(HELM_RELEASE) upgraded"

helm-uninstall:
	helm uninstall $(HELM_RELEASE) --namespace $(NAMESPACE)
	@echo "✓ Helm release $(HELM_RELEASE) uninstalled"

helm-status:
	helm status $(HELM_RELEASE) --namespace $(NAMESPACE)

## ─── Infra (Terraform) ───────────────────────────────────────────────────────

infra:
	@echo "→ Running terraform apply..."
	cd terraform && terraform init -upgrade && terraform apply

## ─── Cluster ────────────────────────────────────────────────────────────────

cluster-auth:
	gcloud container clusters get-credentials $(CLUSTER) \
	  --region $(REGION) \
	  --project $(PROJECT_ID)
	@echo "✓ kubectl connected to $(CLUSTER)"

## ─── Prometheus ─────────────────────────────────────────────────────────────

install-prometheus:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
	  --namespace monitoring --create-namespace \
	  --timeout 10m \
	  --set nodeExporter.enabled=false \
	  --set grafana.enabled=false \
	  --set alertmanager.enabled=false \
	  --set kubeControllerManager.enabled=false \
	  --set kubeScheduler.enabled=false \
	  --set kubeEtcd.enabled=false \
	  --set kubeProxy.enabled=false \
	  --set prometheusOperator.admissionWebhooks.enabled=false \
	  --set prometheusOperator.admissionWebhooks.patch.enabled=false \
	  --set prometheusOperator.tls.enabled=false \
	  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
	  --wait
	@echo "✓ Prometheus installed"

prometheus-forward:
	@echo "→ Prometheus at http://localhost:9090"
	kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090

grafana-forward:
	@echo "→ Grafana at http://localhost:3000 (admin/prom-operator)"
	kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80

## Start both port-forwards in background (needed before running agent)
setup-forwards:
	@echo "→ Starting Prometheus + app port-forwards in background..."
	kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 &
	kubectl port-forward -n $(NAMESPACE) svc/sample-app 8080:8080 &
	@echo "✓ Forwards running. Ctrl+C won't stop them — use: kill \$$(lsof -ti:9090) \$$(lsof -ti:8080)"

## ─── Sample App ─────────────────────────────────────────────────────────────

build-sample-app:
	gcloud auth configure-docker $(REGION)-docker.pkg.dev --quiet
	docker build --platform linux/amd64 -t $(REGISTRY)/sample-app:latest ./sample-app
	docker push $(REGISTRY)/sample-app:latest
	@echo "✓ Pushed $(REGISTRY)/sample-app:latest"

deploy-sample-app:
	kubectl apply -f k8s/namespace.yaml
	sed "s|PROJECT_ID|$(PROJECT_ID)|g" k8s/rbac.yaml | kubectl apply -f -
	sed "s|REGISTRY|$(REGISTRY)|g" k8s/sample-app/deployment.yaml | kubectl apply -f -
	kubectl apply -f k8s/sample-app/service.yaml
	kubectl rollout status deployment/sample-app -n $(NAMESPACE) --timeout=6m
	@echo "✓ sample-app deployed"

app-forward:
	@echo "→ sample-app at http://localhost:8080"
	kubectl port-forward -n $(NAMESPACE) svc/sample-app 8080:8080

## ─── Go Collector ───────────────────────────────────────────────────────────

build-collector:
	cd collector && go mod tidy && go build -o bin/collector ./...
	@echo "✓ Built: collector/bin/collector"

test-collector: build-collector
	@echo "→ Testing collector (needs kubectl context + prometheus-forward running)"
	./collector/bin/collector get-replicas --deployment sample-app --namespace $(NAMESPACE)
	PROMETHEUS_URL=http://localhost:9090 ./collector/bin/collector get-scale-metrics

test-mcp-server: build-collector
	@echo "→ Testing MCP server handshake"
	@echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | \
	  PROMETHEUS_URL=http://localhost:9090 ./collector/bin/collector mcp-server | head -1 | python3 -m json.tool
	@echo "✓ MCP server responds correctly"

## ─── Python Agent ───────────────────────────────────────────────────────────

install-agent:
	python3 -m venv agent/.venv
	agent/.venv/bin/pip install -r agent/requirements.txt
	@echo "✓ Agent dependencies installed in agent/.venv"

run-agent-dry: build-collector
	@echo "→ Dry-run: agent will log decisions but NOT scale anything"
	@echo "   Requires: make prometheus-forward and make app-forward running in separate tabs"
	cd agent && GCP_PROJECT_ID=$(PROJECT_ID) \
	PROMETHEUS_URL=http://localhost:9090 \
	COLLECTOR_BIN=../collector/bin/collector \
	.venv/bin/python3 main.py \
	  --deployment sample-app \
	  --namespace $(NAMESPACE) \
	  --base-url http://localhost:8080 \
	  --target-vus 100 \
	  --max-replicas 10 \
	  --dry-run

run-agent: build-collector
	@echo "→ LIVE run: agent will scale sample-app"
	@echo "   Requires: make prometheus-forward and make app-forward running in separate tabs"
	cd agent && GCP_PROJECT_ID=$(PROJECT_ID) \
	PROMETHEUS_URL=http://localhost:9090 \
	COLLECTOR_BIN=../collector/bin/collector \
	.venv/bin/python3 main.py \
	  --deployment sample-app \
	  --namespace $(NAMESPACE) \
	  --base-url http://localhost:8080 \
	  --target-vus 100 \
	  --max-replicas 10

## ─── v2 Agent (MCP + NATS + KEDA) ───────────────────────────────────────────

run-agent-v2-dry: build-collector
	@echo "→ Dry-run: v2 agent (MCP) — no real scaling"
	@echo "   Requires: make prometheus-forward and make app-forward and make nats-forward"
	cd agent && GCP_PROJECT_ID=$(PROJECT_ID) \
	PROMETHEUS_URL=http://localhost:9090 \
	NATS_URL=nats://localhost:4222 \
	COLLECTOR_BIN=../collector/bin/collector \
	.venv/bin/python3 main_mcp.py \
	  --deployment sample-app \
	  --namespace $(NAMESPACE) \
	  --base-url http://localhost:8080 \
	  --target-vus 100 \
	  --max-replicas 10 \
	  --keda-scaledobject sample-app-scaler \
	  --dry-run

run-agent-v2: build-collector
	@echo "→ LIVE v2 run: LLM-supervised KEDA scaling (k6 in-cluster)"
	@echo "   Requires: make prometheus-forward and make nats-forward"
	cd agent && GCP_PROJECT_ID=$(PROJECT_ID) \
	PROMETHEUS_URL=http://localhost:9090 \
	NATS_URL=nats://localhost:4222 \
	COLLECTOR_BIN=../collector/bin/collector \
	.venv/bin/python3 main_mcp.py \
	  --deployment sample-app \
	  --namespace $(NAMESPACE) \
	  --base-url http://sample-app.$(NAMESPACE).svc.cluster.local:8080 \
	  --target-vus 500 \
	  --max-replicas 20 \
	  --load-in-cluster \
	  --keda-scaledobject sample-app-scaler

## ─── NATS JetStream ──────────────────────────────────────────────────────────

deploy-nats:
	@echo "→ Deploying NATS JetStream (cluster mode)..."
	kubectl apply -f k8s/nats/storageclass.yaml   # StorageClass is cluster-scoped — apply first
	kubectl apply -f k8s/nats/
	kubectl rollout status statefulset/nats -n $(NAMESPACE) --timeout=5m
	@echo "✓ NATS 3-node cluster deployed"

## migrate-nats-to-cluster: safely tears down single-node NATS (deletes PVCs
## which cannot be changed in-place) and redeploys as a 3-node cluster.
## Run this if you previously deployed the single-node version.
## WARNING: this deletes all existing JetStream messages in the stream.
migrate-nats-to-cluster:
	@echo "→ Tearing down single-node NATS (PVCs must be deleted to change StorageClass)..."
	kubectl delete statefulset nats -n $(NAMESPACE) --ignore-not-found
	kubectl delete pvc -l app=nats -n $(NAMESPACE) --ignore-not-found
	kubectl delete configmap nats-config -n $(NAMESPACE) --ignore-not-found
	@echo "→ Waiting for pods to terminate..."
	kubectl wait --for=delete pod -l app=nats -n $(NAMESPACE) --timeout=60s || true
	@echo "→ Redeploying as 3-node cluster..."
	$(MAKE) deploy-nats
	@echo "→ Initialising JetStream stream with R3 replication..."
	$(MAKE) init-nats-stream
	@echo "✓ Migration complete — NATS is now a 3-node cluster with pd-ssd storage"

nats-forward:
	@echo "→ NATS at nats://localhost:4222  monitoring at http://localhost:8222"
	kubectl port-forward -n $(NAMESPACE) svc/nats 4222:4222 8222:8222

init-nats-stream:
	@echo "→ Creating TURTLECRAWL stream + keda-consumer..."
	kubectl run nats-init-$(shell date +%s) \
	  --rm -i --restart=Never \
	  --image=natsio/nats-box \
	  --namespace=$(NAMESPACE) \
	  -- sh -c "\
	    nats stream add TURTLECRAWL \
	      --subjects 'turtlecrawl.>' \
	      --storage file \
	      --retention limits \
	      --max-age 168h \
	      --replicas 3 \
	      --defaults \
	      --server nats://nats.$(NAMESPACE).svc.cluster.local:4222 && \
	    nats consumer add TURTLECRAWL keda-consumer \
	      --filter 'turtlecrawl.keda.load' \
	      --ack explicit \
	      --pull \
	      --deliver all \
	      --defaults \
	      --server nats://nats.$(NAMESPACE).svc.cluster.local:4222 && \
	    echo 'Stream + consumer ready'"
	@echo "✓ NATS stream initialized"
	@echo "→ Seeding keda.load subject (200 requests → activates KEDA consumer)..."
	kubectl run nats-seed-$(shell date +%s) \
	  --rm -i --restart=Never \
	  --image=curlimages/curl \
	  --namespace=$(NAMESPACE) \
	  -- sh -c \
	    "for i in \$$(seq 1 500); do curl -sf http://sample-app.$(NAMESPACE).svc.cluster.local:8080/ > /dev/null; done; echo 'seed done'"
	@echo "✓ NATS keda.load seeded — KEDA consumer is active"

## reset-keda-lag: purge accumulated turtlecrawl.keda.load messages so KEDA
## can scale down from its current high-replica state.
## Run this before each experiment to start with a clean lag baseline.
## Audit snapshots on turtlecrawl.audit.snapshots are preserved.
reset-keda-lag:
	@echo "→ Purging turtlecrawl.keda.load messages (resets KEDA lag to 0)..."
	kubectl run nats-purge-$(shell date +%s) \
	  --rm -i --restart=Never \
	  --image=natsio/nats-box \
	  --namespace=$(NAMESPACE) \
	  -- sh -c "\
	    nats stream purge TURTLECRAWL \
	      --subject 'turtlecrawl.keda.load' \
	      --force \
	      --server nats://nats.$(NAMESPACE).svc.cluster.local:4222 && \
	    echo 'keda.load purged — lag reset to 0'"
	@echo "✓ KEDA lag reset. KEDA will scale down to minReplicaCount within cooldownPeriod (60s)."

## ─── KEDA ────────────────────────────────────────────────────────────────────

install-keda:
	@echo "→ Installing KEDA v2.14.0..."
	kubectl apply --server-side \
	  -f https://github.com/kedacore/keda/releases/download/v2.14.0/keda-2.14.0.yaml
	kubectl rollout status deployment/keda-operator -n keda --timeout=5m
	@echo "✓ KEDA installed"

deploy-keda:
	@echo "→ Applying KEDA ScaledObject..."
	kubectl apply -f k8s/keda/
	@echo "✓ KEDA ScaledObject applied"
	@echo "  Check status: make keda-status"

keda-status:
	@echo "── KEDA ScaledObject ────────────────────────────────"
	kubectl get scaledobjects -n $(NAMESPACE) -o wide
	@echo ""
	@echo "── KEDA HPA ─────────────────────────────────────────"
	kubectl get hpa -n $(NAMESPACE) -o wide
	@echo ""
	@echo "── sample-app replicas ──────────────────────────────"
	kubectl get deployment sample-app -n $(NAMESPACE) -o wide

run-agent-incluster: build-collector
	@echo "→ LIVE run: k6 load runs inside the cluster (no port-forward bottleneck)"
	@echo "   Requires: make prometheus-forward running in a separate tab"
	cd agent && GCP_PROJECT_ID=$(PROJECT_ID) \
	PROMETHEUS_URL=http://localhost:9090 \
	COLLECTOR_BIN=../collector/bin/collector \
	.venv/bin/python3 main.py \
	  --deployment sample-app \
	  --namespace $(NAMESPACE) \
	  --base-url http://sample-app.$(NAMESPACE).svc.cluster.local:8080 \
	  --target-vus 500 \
	  --max-replicas 10 \
	  --load-in-cluster

## ─── Load Test ──────────────────────────────────────────────────────────────

run-loadtest:
	@echo "→ Running k6 at 50 VUs against localhost:8080"
	k6 run \
	  --env BASE_URL=http://localhost:8080 \
	  --env VUS=50 \
	  --env DURATION=60s \
	  loadtest/script.js

## ─── Pause / Resume (end-of-day) ─────────────────────────────────────────────

pause:
	@echo "→ Scaling down all workloads..."
	kubectl scale deployment sample-app --replicas=0 -n $(NAMESPACE) 2>/dev/null || true
	@echo "→ Killing port-forwards..."
	-kill $$(lsof -ti:9090) 2>/dev/null || true
	-kill $$(lsof -ti:8080) 2>/dev/null || true
	-kill $$(lsof -ti:3000) 2>/dev/null || true
	@echo "✓ Paused. GKE cluster is idle (~\$$0.10/hr management fee only)."
	@echo "  Resume tomorrow with: make resume"

resume:
	@echo "→ Scaling up workloads..."
	kubectl scale deployment sample-app --replicas=1 -n $(NAMESPACE)
	kubectl rollout status deployment/sample-app -n $(NAMESPACE) --timeout=5m
	@echo "→ Starting port-forwards..."
	kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 &
	kubectl port-forward -n $(NAMESPACE) svc/sample-app 8080:8080 &
	@echo "✓ Ready. Prometheus :9090 · sample-app :8080"
	@echo "  Run the agent with: make run-agent-dry"

## ─── Cleanup ────────────────────────────────────────────────────────────────

clean:
	cd collector && rm -rf bin/
	find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "✓ Cleaned"
