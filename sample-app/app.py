"""
turtlecrawl sample app — the scaling target.

Exposes:
  GET /         → 200 OK with simulated work
  GET /slow     → 200 OK with configurable delay (simulate high load)
  GET /health   → readiness probe
  GET /metrics  → Prometheus metrics

Custom metrics exposed:
  active_connections          gauge   — connections currently in-flight
  http_requests_in_flight     gauge   — HTTP requests being served right now
  http_requests_total         counter — total requests (labelled by status)
  http_request_duration_seconds histogram — request latency
"""
import os
import random
import time
import threading

from flask import Flask, Response, jsonify, request
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

app = Flask(__name__)

# ─── Prometheus metrics ───────────────────────────────────────────────────────

ACTIVE_CONNECTIONS = Gauge(
    "active_connections",
    "Number of active TCP/HTTP connections",
)
IN_FLIGHT_REQUESTS = Gauge(
    "http_requests_in_flight",
    "Number of HTTP requests currently being processed",
)
REQUEST_COUNTER = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ─── Request tracking middleware ──────────────────────────────────────────────

@app.before_request
def before_request():
    request._start_time = time.perf_counter()
    ACTIVE_CONNECTIONS.inc()
    IN_FLIGHT_REQUESTS.inc()


@app.after_request
def after_request(response):
    elapsed = time.perf_counter() - getattr(request, "_start_time", time.perf_counter())
    ACTIVE_CONNECTIONS.dec()
    IN_FLIGHT_REQUESTS.dec()

    endpoint = request.endpoint or "unknown"
    REQUEST_COUNTER.labels(
        method=request.method,
        endpoint=endpoint,
        status=str(response.status_code),
    ).inc()
    REQUEST_DURATION.labels(endpoint=endpoint).observe(elapsed)
    return response


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Fast endpoint — simulates a cheap API call."""
    # Small jitter to make latency realistic
    time.sleep(random.uniform(0.005, 0.015))
    return jsonify({"status": "ok", "message": "turtlecrawl sample app"})


@app.route("/slow")
def slow():
    """Slow endpoint — simulate CPU/DB-bound work. Delay scales with load."""
    delay = float(request.args.get("delay", 0.1))
    delay = min(delay, 5.0)  # cap at 5s
    time.sleep(delay)
    return jsonify({"status": "ok", "delay_ms": int(delay * 1000)})


@app.route("/stress")
def stress():
    """CPU stress endpoint — spins for a bit to drive CPU metrics."""
    ms = int(request.args.get("ms", 50))
    end = time.perf_counter() + (ms / 1000)
    x = 0
    while time.perf_counter() < end:
        x += 1  # busy loop
    return jsonify({"status": "ok", "iterations": x})


@app.route("/health")
def health():
    """Kubernetes readiness + liveness probe."""
    return jsonify({"status": "healthy"})


@app.route("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    workers = int(os.getenv("WORKERS", "4"))
    app.run(host="0.0.0.0", port=port, threaded=True)
