"""
turtlecrawl sample app — the scaling target.

v2 additions:
  • NATS JetStream publisher (background thread)
  • Publishes to turtlecrawl.keda.load on each request (10% sampled)
    → KEDA ScaledObject reads consumer lag to auto-scale this deployment
  • Publishes metric snapshots to turtlecrawl.audit.snapshots every 30s
    → GCS audit writer consumes this for the durable audit trail

Exposes:
  GET /         → 200 OK with simulated work
  GET /slow     → 200 OK with configurable delay
  GET /stress   → CPU stress endpoint
  GET /health   → readiness probe
  GET /metrics  → Prometheus metrics

Prometheus metrics:
  active_connections          gauge
  http_requests_in_flight     gauge
  http_requests_total         counter  (method, endpoint, status)
  http_request_duration_seconds histogram
"""
import json
import os
import random
import socket
import threading
import time

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

# ─── NATS publisher ───────────────────────────────────────────────────────────

NATS_URL     = os.getenv("NATS_URL", "nats://nats.turtlecrawl.svc.cluster.local:4222")
NATS_ENABLED = os.getenv("NATS_ENABLED", "true").lower() == "true"

SUBJECT_KEDA_LOAD       = "turtlecrawl.keda.load"
SUBJECT_AUDIT_SNAPSHOTS = "turtlecrawl.audit.snapshots"

KEDA_SAMPLE_RATE       = float(os.getenv("KEDA_SAMPLE_RATE", "0.10"))
SNAPSHOT_INTERVAL_SECS = int(os.getenv("SNAPSHOT_INTERVAL_SECS", "30"))

# ─── Synchronous NATS publisher ───────────────────────────────────────────────
#
# The asyncio-based publisher fails in gunicorn's pre-fork model:
# nats-py tries to register asyncio signal handlers, which Python only
# allows in the main thread. In a forked worker's background thread,
# this silently falls back to no-op — no messages ever reach NATS.
#
# This implementation uses raw TCP sockets. The NATS PUB wire protocol
# is three lines of text. Raw sockets work in any thread in any process.
# JetStream captures the message automatically because the TURTLECRAWL
# stream covers subject prefix 'turtlecrawl.>'.

class _SyncNATSPublisher(threading.Thread):
    """Persistent synchronous NATS publisher — raw TCP, no asyncio."""

    def __init__(self, url: str, queue: list):
        super().__init__(daemon=True, name="nats-publisher")
        host_port = url.replace("nats://", "")
        self.host, port_str = host_port.rsplit(":", 1)
        self.port = int(port_str)
        self._queue = queue
        self._sock: socket.socket | None = None

    def _connect(self) -> bool:
        try:
            s = socket.create_connection((self.host, self.port), timeout=5)
            # Read the INFO {...} server greeting (ends with \r\n)
            buf = b""
            while b"\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    s.close()
                    return False
                buf += chunk
            # verbose:false → server skips +OK replies, reducing chatter
            s.sendall(b'CONNECT {"verbose":false,"pedantic":false}\r\n')
            self._sock = s
            print(f"[nats] Connected to {self.host}:{self.port}", flush=True)
            return True
        except Exception as e:
            print(f"[nats] Connect failed: {e}", flush=True)
            self._sock = None
            return False

    def _publish_one(self, subject: str, payload: bytes) -> bool:
        if self._sock is None:
            return False
        try:
            # Drain any server-sent data (PING keepalives) non-blocking
            self._sock.setblocking(False)
            try:
                data = self._sock.recv(4096)
                if b"PING" in data:
                    self._sock.setblocking(True)
                    self._sock.sendall(b"PONG\r\n")
                elif not data:
                    raise OSError("server closed connection")
            except BlockingIOError:
                pass  # nothing pending, fine
            self._sock.setblocking(True)
            # NATS PUB wire format: PUB <subject> <bytes>\r\n<payload>\r\n
            header = f"PUB {subject} {len(payload)}\r\n".encode()
            self._sock.sendall(header + payload + b"\r\n")
            return True
        except OSError:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            return False

    def run(self) -> None:
        # Brief delay so gunicorn finishes post-fork worker setup before
        # we open a socket (avoids inheriting a half-initialised fd table).
        time.sleep(2)

        snapshot_last = 0.0
        reconnect_delay = 1

        while True:
            # Ensure we have a live connection
            if self._sock is None:
                if self._connect():
                    reconnect_delay = 1
                else:
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 30)
                    continue

            if self._queue:
                item = self._queue.pop(0)
                if not self._publish_one(item["subject"], item["payload"]):
                    # Re-queue and let the reconnect loop retry
                    self._queue.insert(0, item)
            else:
                # Idle: send periodic metric snapshot
                now = time.time()
                if now - snapshot_last >= SNAPSHOT_INTERVAL_SECS:
                    snapshot_last = now
                    snapshot = json.dumps({
                        "source": "sample-app",
                        "metrics": {
                            "active_connections":  ACTIVE_CONNECTIONS._value.get(),
                            "in_flight_requests":  IN_FLIGHT_REQUESTS._value.get(),
                            "request_count_total": _request_count,
                        },
                        "ts": now,
                    }).encode()
                    self._publish_one(SUBJECT_AUDIT_SNAPSHOTS, snapshot)
                else:
                    time.sleep(0.05)   # 50 ms idle poll


_publish_queue: list[dict] = []
_request_count = 0
_request_count_lock = threading.Lock()

if NATS_ENABLED:
    _publisher = _SyncNATSPublisher(NATS_URL, _publish_queue)
    _publisher.start()


def _try_publish_keda_load() -> None:
    """Enqueue a lightweight keda.load message (non-blocking, sampled)."""
    if not NATS_ENABLED:
        return
    global _request_count
    with _request_count_lock:
        _request_count += 1
        count = _request_count
    if random.random() > KEDA_SAMPLE_RATE:
        return
    _publish_queue.append({
        "subject": SUBJECT_KEDA_LOAD,
        "payload": json.dumps({"count": count, "ts": time.time()}).encode(),
    })

# ─── Request tracking middleware ──────────────────────────────────────────────

def _is_infra_request() -> bool:
    """Return True for /health and /metrics.

    These are Prometheus scrapes and k8s probes — not real user traffic.
    Excluding them from ACTIVE_CONNECTIONS / IN_FLIGHT_REQUESTS prevents
    each pod from always showing ≥1 on those gauges because the Prometheus
    scrape captures the metric value while its own request is still in-flight.
    """
    return request.endpoint in ("health", "metrics")


@app.before_request
def before_request():
    request._start_time = time.perf_counter()
    if not _is_infra_request():
        ACTIVE_CONNECTIONS.inc()
        IN_FLIGHT_REQUESTS.inc()


@app.after_request
def after_request(response):
    elapsed = time.perf_counter() - getattr(request, "_start_time", time.perf_counter())
    if not _is_infra_request():
        ACTIVE_CONNECTIONS.dec()
        IN_FLIGHT_REQUESTS.dec()

    endpoint = request.endpoint or "unknown"
    REQUEST_COUNTER.labels(
        method=request.method,
        endpoint=endpoint,
        status=str(response.status_code),
    ).inc()
    REQUEST_DURATION.labels(endpoint=endpoint).observe(elapsed)

    if endpoint not in ("metrics", "health"):
        _try_publish_keda_load()

    return response

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Fast endpoint — simulates a cheap API call."""
    time.sleep(random.uniform(0.005, 0.015))
    return jsonify({"status": "ok", "message": "turtlecrawl sample app v2"})


@app.route("/slow")
def slow():
    """Slow endpoint — simulate CPU/DB-bound work."""
    delay = float(request.args.get("delay", 0.1))
    delay = min(delay, 5.0)
    time.sleep(delay)
    return jsonify({"status": "ok", "delay_ms": int(delay * 1000)})


@app.route("/stress")
def stress():
    """CPU stress endpoint — busy-loop to drive CPU metrics."""
    ms = int(request.args.get("ms", 50))
    end = time.perf_counter() + (ms / 1000)
    x = 0
    while time.perf_counter() < end:
        x += 1
    return jsonify({"status": "ok", "iterations": x})


@app.route("/health")
def health():
    """Kubernetes readiness + liveness probe."""
    return jsonify({"status": "healthy"})


@app.route("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
