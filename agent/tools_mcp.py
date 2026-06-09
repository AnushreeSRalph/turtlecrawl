"""
tools_mcp.py — Gemini tool declarations for the MCP-backed agent (v2).

13 tools total:
  11 Go MCP server tools  (routed through MCPClient.call_tool)
   2 Python-native tools  (run_load_test, wait — handled locally)
"""
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import vertexai
from vertexai.generative_models import FunctionDeclaration, Tool

# ─── 11 Go MCP tools ─────────────────────────────────────────────────────────

_MCP_DECLARATIONS = [

    FunctionDeclaration(
        name="get_scale_metrics",
        description=(
            "Get all scaling-relevant metrics in one shot: active_connections, "
            "in_flight_requests, p99_latency_ms, rps_total, error_rate_5xx. "
            "Always call this first before making a scaling decision."
        ),
        parameters={"type": "object", "properties": {}},
    ),

    FunctionDeclaration(
        name="get_metric",
        description="Execute a custom PromQL query and return its current value.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL query to execute"},
            },
            "required": ["query"],
        },
    ),

    FunctionDeclaration(
        name="get_replicas",
        description="Get current replica count and readiness status for a deployment.",
        parameters={
            "type": "object",
            "properties": {
                "deployment": {"type": "string", "description": "Deployment name"},
                "namespace":  {"type": "string", "description": "Kubernetes namespace"},
            },
            "required": ["deployment", "namespace"],
        },
    ),

    FunctionDeclaration(
        name="scale_deployment",
        description=(
            "Scale a deployment to a given number of replicas. "
            "IMPORTANT: Always call check_scale_gate before scaling down. "
            "Call set_keda_paused(paused=true) before manual scaling "
            "to prevent KEDA from immediately overriding your change. "
            "Resume KEDA with set_keda_paused(paused=false) when done."
        ),
        parameters={
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "namespace":  {"type": "string"},
                "replicas": {
                    "type": "integer",
                    "description": "Target replica count (1–50)",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why this scale action is needed",
                },
            },
            "required": ["deployment", "namespace", "replicas", "reason"],
        },
    ),

    FunctionDeclaration(
        name="get_pod_status",
        description="Get per-pod readiness status, restart count, and node assignment.",
        parameters={
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "namespace":  {"type": "string"},
            },
            "required": ["deployment", "namespace"],
        },
    ),

    FunctionDeclaration(
        name="check_scale_gate",
        description=(
            "Run all 6 scale-down safety conditions in parallel: "
            "max(active_connections)<2/pod, max(in_flight)<2/pod, p99<120ms, error_rate<1%, "
            "per_pod_rps_headroom<80, all_pods_ready. "
            "Returns all_passed:true only when all 6 conditions pass. "
            "Always call this before scaling down a deployment."
        ),
        parameters={
            "type": "object",
            "properties": {
                "deployment": {"type": "string", "description": "Deployment name to check"},
                "namespace":  {"type": "string", "description": "Kubernetes namespace"},
            },
            "required": ["deployment", "namespace"],
        },
    ),

    FunctionDeclaration(
        name="get_nats_stream_info",
        description=(
            "Get NATS JetStream stream status: message count, bytes stored, "
            "and per-consumer lag (num_pending). "
            "KEDA uses consumer lag on the keda-consumer to trigger scaling. "
            "High num_pending means KEDA is about to scale up sample-app. "
            "IMPORTANT: keda-consumer is a read-only lag gauge — messages on "
            "turtlecrawl.keda.load are intentionally never acked or consumed. "
            "num_pending growing is normal and expected; it does NOT indicate "
            "a processing failure."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "JetStream stream name (default: TURTLECRAWL)",
                },
                "consumer": {
                    "type": "string",
                    "description": "Consumer name to fetch lag for (omit for all consumers)",
                },
            },
        },
    ),

    FunctionDeclaration(
        name="publish_metric_snapshot",
        description=(
            "Publish a metric snapshot to the NATS audit subject "
            "(turtlecrawl.audit.snapshots). The snapshot is durably stored in "
            "JetStream and consumed by the GCS writer asynchronously. "
            "Use this to record key moments in the scaling experiment."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metrics": {
                    "type": "object",
                    "description": "Key-value map of metric names to values",
                },
                "label": {
                    "type": "string",
                    "description": "Short label for this snapshot (e.g. 'pre-scale-down')",
                },
            },
            "required": ["metrics"],
        },
    ),

    FunctionDeclaration(
        name="get_keda_scaledobject",
        description=(
            "Get the current state of a KEDA ScaledObject: "
            "current_replicas, desired_replicas, last_scale_time, paused status, "
            "and active triggers. "
            "Use this to understand what KEDA is doing before intervening manually."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "ScaledObject name (e.g. sample-app-scaler)",
                },
                "namespace": {"type": "string"},
            },
            "required": ["name", "namespace"],
        },
    ),

    FunctionDeclaration(
        name="set_keda_paused",
        description=(
            "Pause or resume KEDA autoscaling for a ScaledObject. "
            "Call with paused=true before calling scale_deployment "
            "to prevent KEDA from immediately overriding your manual scale. "
            "Always call with paused=false when done to restore autonomous KEDA scaling."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name":      {"type": "string", "description": "ScaledObject name"},
                "namespace": {"type": "string"},
                "paused": {
                    "type": "boolean",
                    "description": "true to pause autoscaling, false to resume",
                },
            },
            "required": ["name", "namespace", "paused"],
        },
    ),

    FunctionDeclaration(
        name="list_k8s_events",
        description=(
            "List recent Kubernetes events for a namespace, "
            "optionally filtered to a specific deployment. "
            "Warning events indicate OOMKilled pods, failed pulls, "
            "CrashLoopBackOff restarts, or scheduling failures. "
            "Call this when pod status looks unhealthy."
        ),
        parameters={
            "type": "object",
            "properties": {
                "namespace":    {"type": "string"},
                "deployment":   {"type": "string", "description": "Filter events to this deployment"},
                "warning_only": {"type": "boolean", "description": "Return only Warning events (default: true)"},
                "limit":        {"type": "integer", "description": "Max events to return (default: 20)"},
            },
            "required": ["namespace"],
        },
    ),
]

# ─── 2 Python-native tools ────────────────────────────────────────────────────

_NATIVE_DECLARATIONS = [
    FunctionDeclaration(
        name="run_load_test",
        description=(
            "Run a k6 load test against the sample app. "
            "Returns p99 latency, RPS, and error rate from the test run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "Base URL (e.g. http://sample-app.turtlecrawl.svc:8080)",
                },
                "vus": {
                    "type": "integer",
                    "description": "Number of virtual users (concurrent load)",
                },
                "duration_seconds": {
                    "type": "integer",
                    "description": "Test duration in seconds",
                },
            },
            "required": ["base_url", "vus", "duration_seconds"],
        },
    ),

    FunctionDeclaration(
        name="wait",
        description="Wait for a number of seconds (e.g. after scaling, before re-checking metrics).",
        parameters={
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "How many seconds to wait (max 300)",
                },
                "reason": {"type": "string"},
            },
            "required": ["seconds"],
        },
    ),
]

# Single Tool object passed to GenerativeModel
GEMINI_TOOLS_MCP = Tool(function_declarations=_MCP_DECLARATIONS + _NATIVE_DECLARATIONS)

# Tools that are handled locally in Python (not routed to the MCP server)
PYTHON_NATIVE_TOOLS = {"run_load_test", "wait"}


# ─── Python-native tool implementations ──────────────────────────────────────

def execute_native_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    dry_run: bool = False,
    load_in_cluster: bool = False,
    namespace: str = "turtlecrawl",
) -> dict[str, Any]:
    """Execute a Python-native tool (run_load_test, wait)."""

    if tool_name == "run_load_test":
        return _run_load_test(
            base_url=tool_input["base_url"],
            vus=int(tool_input["vus"]),
            duration=int(tool_input.get("duration_seconds", 60)),
            dry_run=dry_run,
            in_cluster=load_in_cluster,
            namespace=namespace,
        )

    elif tool_name == "wait":
        seconds = min(int(tool_input["seconds"]), 300)
        reason  = tool_input.get("reason", "")
        print(f"  [wait] Sleeping {seconds}s — {reason}")
        time.sleep(seconds)
        return {"waited_seconds": seconds, "reason": reason}

    return {"error": f"Unknown native tool: {tool_name}"}


def _run_load_test(
    base_url: str,
    vus: int,
    duration: int,
    dry_run: bool = False,
    in_cluster: bool = False,
    namespace: str = "turtlecrawl",
) -> dict:
    if dry_run:
        mode = "in-cluster k6 Job" if in_cluster else "local k6"
        return {
            "dry_run": True,
            "message": f"DRY RUN: would run {mode} with {vus} VUs for {duration}s against {base_url}",
        }

    if in_cluster:
        return _run_load_test_incluster(base_url, vus, duration, namespace)

    script = Path(__file__).parent.parent / "loadtest" / "script.js"
    cmd = [
        "k6", "run",
        "--env", f"BASE_URL={base_url}",
        "--env", f"VUS={vus}",
        "--env", f"DURATION={duration}s",
        str(script),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 60)
        return _parse_k6_output(result.stdout + result.stderr, vus, duration)
    except subprocess.TimeoutExpired:
        return {"error": "k6 load test timed out"}
    except FileNotFoundError:
        return {"error": "k6 not found — install with: brew install k6"}


def _run_load_test_incluster(base_url: str, vus: int, duration: int, namespace: str) -> dict:
    script = Path(__file__).parent.parent / "loadtest" / "script.js"
    script_content = script.read_text()
    job_name = f"k6-{int(time.time())}"
    # Do NOT use --rm: we handle cleanup in finally so it always runs even if
    # this process is interrupted. --rm only fires when kubectl exits cleanly,
    # which is not guaranteed if Python receives a signal.
    cmd = [
        "kubectl", "run", job_name,
        "--image=grafana/k6",
        f"--namespace={namespace}",
        "--restart=Never",
        "-i",   # attach stdin so the script piped via communicate() reaches k6
        "--",
        "run",
        "--vus", str(vus),
        "--duration", f"{duration}s",
        "--env", f"BASE_URL={base_url}",
        "--env", f"VUS={vus}",
        "--env", f"DURATION={duration}s",
        "-",
    ]
    output = ""
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            output, _ = proc.communicate(input=script_content, timeout=duration + 120)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
            return {"error": "in-cluster k6 job timed out"}
        return _parse_k6_output(output, vus, duration)
    except FileNotFoundError:
        return {"error": "kubectl not found"}
    finally:
        # Always clean up the pod, whether the test succeeded, failed, or timed out.
        # This runs even if Python receives SIGINT/SIGTERM.
        try:
            subprocess.run(
                ["kubectl", "delete", "pod", job_name,
                 f"--namespace={namespace}", "--ignore-not-found", "--wait=false"],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass  # best-effort cleanup — don't mask the real error


def _parse_k6_output(output: str, vus: int, duration: int) -> dict:
    metrics = {"vus": vus, "duration_seconds": duration}
    patterns = {
        "p99_latency_ms": r"http_req_duration.*?p\(99\)=(\d+\.?\d*)ms",
        "rps":            r"http_reqs\s+\d+\s+(\d+\.?\d+)/s",
        "error_rate":     r"http_req_failed.*?(\d+\.?\d+)%",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, output)
        if m:
            metrics[key] = float(m.group(1))
    return metrics
