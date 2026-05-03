"""
Tool implementations for the turtlecrawl agent.

Each function shells out to the Go collector binary, which handles all
Kubernetes and Prometheus interactions. Output is always JSON.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import vertexai
from vertexai.generative_models import FunctionDeclaration, Tool

# Path to the Go collector binary — override with COLLECTOR_BIN env var
COLLECTOR_BIN = os.getenv(
    "COLLECTOR_BIN",
    str(Path(__file__).parent.parent / "collector" / "bin" / "collector"),
)

# ─── Gemini function declarations ─────────────────────────────────────────────

_DECLARATIONS = [
    FunctionDeclaration(
        name="get_scale_metrics",
        description=(
            "Get all scaling-relevant metrics in one shot: active_connections, "
            "in_flight_requests, p99_latency_ms, rps_total, error_rate_5xx. "
            "Always call this first before making a scaling decision."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    FunctionDeclaration(
        name="get_metric",
        description="Execute a custom PromQL query and return its current value.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL query to execute",
                },
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
            "IMPORTANT: Always apply the scale-down gate before scaling down. "
            "Always verify pods are Ready after scaling up."
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
                    "description": "Base URL of the app (e.g. http://sample-app.turtlecrawl.svc:8080)",
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
GEMINI_TOOLS = Tool(function_declarations=_DECLARATIONS)


# ─── Tool execution ───────────────────────────────────────────────────────────

def run_collector(*args: str) -> dict[str, Any]:
    """Run the collector binary with the given arguments, return parsed JSON."""
    cmd = [COLLECTOR_BIN] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and result.stderr:
            return {"error": result.stderr.strip()}
        return json.loads(result.stdout.strip())
    except subprocess.TimeoutExpired:
        return {"error": f"collector timed out: {' '.join(cmd)}"}
    except json.JSONDecodeError as e:
        return {"error": f"invalid JSON from collector: {e}", "raw": result.stdout}
    except FileNotFoundError:
        return {
            "error": f"collector binary not found at {COLLECTOR_BIN}. Run: make build-collector"
        }


def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    deployment: str,
    namespace: str,
    dry_run: bool = False,
    max_replicas: int = 20,
    load_in_cluster: bool = False,
) -> dict[str, Any]:
    """Dispatch a tool call and return its result."""

    if tool_name == "get_scale_metrics":
        return run_collector("get-scale-metrics")

    elif tool_name == "get_metric":
        return run_collector("get-metric", "--query", tool_input["query"])

    elif tool_name == "get_replicas":
        return run_collector(
            "get-replicas",
            "--deployment", tool_input.get("deployment", deployment),
            "--namespace",  tool_input.get("namespace", namespace),
        )

    elif tool_name == "scale_deployment":
        target = int(tool_input["replicas"])
        if target > max_replicas:
            return {
                "error": f"Requested {target} replicas exceeds max_replicas={max_replicas}",
                "action": "blocked",
            }
        if dry_run:
            return {
                "dry_run": True,
                "would_scale_to": target,
                "reason": tool_input.get("reason", ""),
                "message": f"DRY RUN: would scale {tool_input.get('deployment', deployment)} to {target} replicas",
            }
        result = run_collector(
            "scale",
            "--deployment", tool_input.get("deployment", deployment),
            "--namespace",  tool_input.get("namespace", namespace),
            "--replicas",   str(target),
        )
        result["reason"] = tool_input.get("reason", "")
        return result

    elif tool_name == "get_pod_status":
        return run_collector(
            "pod-status",
            "--deployment", tool_input.get("deployment", deployment),
            "--namespace",  tool_input.get("namespace", namespace),
        )

    elif tool_name == "run_load_test":
        return _run_load_test(
            tool_input["base_url"],
            int(tool_input["vus"]),
            int(tool_input.get("duration_seconds", 60)),
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

    else:
        return {"error": f"Unknown tool: {tool_name}"}


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
    """Run k6 as a kubectl pod inside the cluster — bypasses port-forward bottleneck."""
    script = Path(__file__).parent.parent / "loadtest" / "script.js"
    script_content = script.read_text()
    job_name = f"k6-{int(time.time())}"
    cmd = [
        "kubectl", "run", job_name,
        "--image=grafana/k6",
        f"--namespace={namespace}",
        "--restart=Never",
        "--rm", "-i",
        "--",
        "run",
        "--vus", str(vus),
        "--duration", f"{duration}s",
        "--env", f"BASE_URL={base_url}",
        "--env", f"VUS={vus}",
        "--env", f"DURATION={duration}s",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=script_content,
            capture_output=True,
            text=True,
            timeout=duration + 120,
        )
        return _parse_k6_output(result.stdout + result.stderr, vus, duration)
    except subprocess.TimeoutExpired:
        return {"error": "in-cluster k6 job timed out"}
    except FileNotFoundError:
        return {"error": "kubectl not found"}


def _parse_k6_output(output: str, vus: int, duration: int) -> dict:
    import re
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
