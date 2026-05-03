PROJECT_ID ?= $(shell gcloud config get-value project)
REGION     ?= us-central1
CLUSTER    ?= turtlecrawl
NAMESPACE  ?= turtlecrawl
REGISTRY   ?= $(REGION)-docker.pkg.dev/$(PROJECT_ID)/turtlecrawl

.PHONY: help infra cluster-auth \
        install-prometheus prometheus-forward grafana-forward \
        build-sample-app deploy-sample-app app-forward \
        build-collector test-collector \
        install-agent run-agent run-agent-dry \
        run-loadtest pause resume clean

## ─── Help ───────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  turtlecrawl — local deploy workflow"
	@echo ""
	@echo "  Day 1 — Infra"
	@echo "    make infra              provision GKE + GCS + Artifact Registry"
	@echo "    make cluster-auth       connect kubectl to your cluster"
	@echo ""
	@echo "  Day 2 — Prometheus"
	@echo "    make install-prometheus install kube-prometheus-stack via Helm"
	@echo "    make prometheus-forward port-forward Prometheus to localhost:9090"
	@echo "    make grafana-forward    port-forward Grafana to localhost:3000"
	@echo ""
	@echo "  Day 3 — Sample App"
	@echo "    make build-sample-app   build + push Docker image to Artifact Registry"
	@echo "    make deploy-sample-app  kubectl apply all sample-app manifests"
	@echo "    make app-forward        port-forward sample-app to localhost:8080"
	@echo ""
	@echo "  Day 4 — Collector + Agent"
	@echo "    make build-collector    compile Go collector binary"
	@echo "    make install-agent      pip install agent dependencies"
	@echo "    make run-agent-dry      run agent in dry-run (no real scaling)"
	@echo "    make run-agent          run agent live"
	@echo ""
	@echo "  Load test"
	@echo "    make run-loadtest       run k6 against localhost:8080"
	@echo ""
	@echo "  End of day"
	@echo "    make pause              scale down pods + kill port-forwards"
	@echo "    make resume             scale up + restart port-forwards"
	@echo ""

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
	kubectl apply -f k8s/rbac.yaml
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
