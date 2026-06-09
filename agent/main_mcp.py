#!/usr/bin/env python3
"""
turtlecrawl v2 — LLM-supervised KEDA autoscaling agent.

KEDA handles autonomous scaling via NATS JetStream consumer lag.
The agent supervises: it pauses KEDA, checks the 6-condition scale-down
gate, optionally overrides replicas, then resumes KEDA.

Auth: Vertex AI via gcloud ADC — no API key required.

Usage:
    python main_mcp.py --deployment sample-app --namespace turtlecrawl \
        --target-vus 500 --dry-run

    python main_mcp.py --deployment sample-app --namespace turtlecrawl \
        --target-vus 500
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig, Part

from audit import AuditLogger
from mcp_client import MCPClient, MCPError
from tools_mcp import GEMINI_TOOLS_MCP, PYTHON_NATIVE_TOOLS, execute_native_tool

# ─── Config ───────────────────────────────────────────────────────────────────

GCP_PROJECT = (
    os.getenv("GCP_PROJECT_ID")
    or os.popen("gcloud config get-value project 2>/dev/null").read().strip()
)
GCP_REGION  = os.getenv("GCP_REGION", "us-central1")
MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

COLLECTOR_BIN = os.getenv(
    "COLLECTOR_BIN",
    str(Path(__file__).parent.parent / "collector" / "bin" / "collector"),
)

MAX_TOKENS      = 4096
MAX_AGENT_TURNS = 60

# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are turtlecrawl v2 — an LLM-supervised Kubernetes autoscaling agent.

ARCHITECTURE — read carefully, these facts prevent misdiagnosis:

1. NATS CONSUMER LAG IS A ONE-WAY METRIC.
   sample-app PUBLISHES to turtlecrawl.keda.load on each HTTP request (10% sampled).
   Nobody ever consumes or acks those messages. num_pending ONLY grows — it never
   decreases on its own. A high or growing num_pending is NORMAL and CORRECT.
   Do NOT treat growing num_pending as an anomaly or processing failure.

2. KEDA SCALES UP BASED ON num_pending, BUT CANNOT SCALE DOWN AUTONOMOUSLY.
   With accumulated lag > lagThreshold × maxReplicaCount, KEDA will always want
   maxReplicaCount replicas. You are responsible for the scale-down phase:
   pause KEDA, run check_scale_gate, scale down manually, then resume KEDA.

3. get_keda_scaledobject READS REPLICA COUNTS FROM THE HPA (not ScaledObject status).
   current_replicas and desired_replicas are accurate. Trust this tool.

4. get_replicas IS ALWAYS THE GROUND TRUTH for running pod count.

5. PROMETHEUS METRICS ARE RATE-WINDOWED (9 minutes).
   rps_total after a short load test will look lower than peak. A value of
   0.2–5 RPS after a 60s load test is normal dilution — not zero traffic.
   active_connections and in_flight_requests read near-zero at rest — normal.

6. p99_latency_ms: 4.95 is the Prometheus default when no histogram data exists.
   Only trust latency readings during or immediately after a live load test.

WORKFLOW:
1. Check baseline: get_keda_scaledobject, get_scale_metrics, get_nats_stream_info.
2. Run the load test (run_load_test). Watch num_pending grow — this confirms the
   publisher is working. Watch get_replicas to confirm KEDA is scaling up.
3. After load test completes, wait 30s for metrics to settle.
4. Find the minimum safe replica count:
   a. Pause KEDA: set_keda_paused(paused=true)
   b. Check scale gate: check_scale_gate (all 6 must pass)
   c. Scale down one step: scale_deployment
   d. Wait 20s, re-check gate and metrics
   e. Repeat until gate fails or you reach replica=1
   f. The last replica count where the gate passed = minimum safe count
   g. Resume KEDA: set_keda_paused(paused=false)
5. Publish pre/post snapshots to the audit trail (publish_metric_snapshot).
6. Report final recommendation.

SCALE-DOWN GATE (check_scale_gate) — ALL 6 must pass:
1. max(active_connections) < 2 per pod
2. max(in_flight_requests) < 2 per pod
3. p99_latency_ms < 120ms
4. error_rate_5xx < 1%
5. per_pod_rps < 80 RPS/pod
6. all_pods_ready

NEVER scale down without a passing gate check.
Use list_k8s_events when pods show restarts or FailedScheduling warnings.
DO NOT abort the experiment due to high num_pending, low rps_total at rest,
or discrepancies between get_keda_scaledobject and get_replicas — these are
known normal states, not failures.
"""

# ─── Initial task message ─────────────────────────────────────────────────────

def build_initial_message(
    deployment: str,
    namespace: str,
    target_vus: int,
    max_replicas: int,
    base_url: str,
    dry_run: bool,
    keda_scaledobject: str,
) -> str:
    mode = "DRY RUN" if dry_run else "LIVE"
    return f"""
Start the turtlecrawl v2 scaling experiment.

Mode: {mode}
Deployment: {deployment}
Namespace: {namespace}
Target load: {target_vus} VUs
Max replicas: {max_replicas}
App URL: {base_url}
KEDA ScaledObject: {keda_scaledobject}
SLO: p99 latency < 120ms, error rate < 1%

1. Check KEDA ScaledObject status and current Prometheus metrics.
2. Check NATS stream info to see current load signal.
3. Run the load test at {target_vus} VUs.
4. Observe KEDA's autoscaling response.
5. After load stabilizes, find the minimum safe replica count (scale-down with gate).
6. Publish pre-scale-down and post-scale-down metric snapshots to the audit trail.
7. Resume KEDA autoscaling and report your final recommendation.
""".strip()


# ─── Agent loop ───────────────────────────────────────────────────────────────

def run_agent(
    deployment: str,
    namespace: str,
    target_vus: int,
    max_replicas: int,
    base_url: str,
    dry_run: bool = False,
    audit_bucket: str | None = None,
    load_in_cluster: bool = False,
    keda_scaledobject: str = "sample-app-scaler",
) -> None:

    if not GCP_PROJECT:
        print("ERROR: Could not determine GCP project.")
        print("       Run: gcloud config set project YOUR_PROJECT_ID")
        sys.exit(1)

    vertexai.init(project=GCP_PROJECT, location=GCP_REGION)

    model = GenerativeModel(
        MODEL,
        system_instruction=SYSTEM_PROMPT,
        tools=[GEMINI_TOOLS_MCP],
        generation_config=GenerationConfig(
            temperature=0.1,
            max_output_tokens=MAX_TOKENS,
        ),
    )

    audit = AuditLogger(deployment, namespace)

    print(f"\n{'='*60}")
    print(f"  turtlecrawl v2 — LLM-supervised KEDA agent")
    print(f"  run_id    : {audit.run_id}")
    print(f"  project   : {GCP_PROJECT}  region: {GCP_REGION}")
    print(f"  model     : {MODEL}")
    print(f"  protocol  : MCP JSON-RPC 2.0 (stdio)")
    print(f"  target    : {deployment}/{namespace}")
    print(f"  load      : {target_vus} VUs")
    print(f"  dry_run   : {dry_run}")
    print(f"{'='*60}\n")

    audit.log("agent_start", {
        "deployment":         deployment,
        "namespace":          namespace,
        "target_vus":         target_vus,
        "max_replicas":       max_replicas,
        "base_url":           base_url,
        "dry_run":            dry_run,
        "model":              MODEL,
        "gcp_project":        GCP_PROJECT,
        "gcp_region":         GCP_REGION,
        "protocol":           "mcp-stdio",
        "keda_scaledobject":  keda_scaledobject,
    })

    # Start the Go MCP server
    with MCPClient(COLLECTOR_BIN) as mcp:
        print(f"  MCP server: {mcp.server_info.get('name')} v{mcp.server_info.get('version')}")

        # Confirm tools available
        tools = mcp.list_tools()
        print(f"  MCP tools : {len(tools)} Go tools + 2 Python-native = {len(tools)+2} total\n")

        chat  = model.start_chat()
        turns = 0

        response = chat.send_message(
            build_initial_message(
                deployment, namespace, target_vus, max_replicas,
                base_url, dry_run, keda_scaledobject,
            )
        )

        while turns < MAX_AGENT_TURNS:
            turns += 1
            print(f"\n── Turn {turns} ──────────────────────────────")

            text_parts     = []
            function_calls = []

            for candidate in response.candidates:
                for part in candidate.content.parts:
                    try:
                        if part.text:
                            text_parts.append(part.text)
                    except AttributeError:
                        pass
                    if part.function_call and part.function_call.name:
                        function_calls.append(part.function_call)

            if text_parts:
                print(f"\n[agent]\n{''.join(text_parts)}")

            if not function_calls:
                print("\n✓ Agent completed.")
                break

            tool_response_parts = []
            reasoning = "".join(text_parts)[-500:] if text_parts else ""

            for fc in function_calls:
                tool_name  = fc.name
                tool_input = dict(fc.args)

                print(f"\n[tool] {tool_name}({json.dumps(tool_input, default=str)})")

                # Route: Python-native vs Go MCP server
                if tool_name in PYTHON_NATIVE_TOOLS:
                    result = execute_native_tool(
                        tool_name,
                        tool_input,
                        dry_run=dry_run,
                        load_in_cluster=load_in_cluster,
                        namespace=namespace,
                    )
                else:
                    # Enforce max_replicas guard on the client side
                    if tool_name == "scale_deployment":
                        target_r = int(tool_input.get("replicas", 0))
                        if target_r > max_replicas:
                            result = {
                                "error": f"Requested {target_r} replicas exceeds max_replicas={max_replicas}",
                                "action": "blocked",
                            }
                        elif dry_run:
                            result = {
                                "dry_run": True,
                                "would_scale_to": target_r,
                                "reason": tool_input.get("reason", ""),
                                "message": f"DRY RUN: would scale {tool_input.get('deployment', deployment)} to {target_r} replicas",
                            }
                        else:
                            try:
                                result = mcp.call_tool(tool_name, tool_input)
                            except MCPError as e:
                                result = {"error": str(e)}
                    else:
                        try:
                            result = mcp.call_tool(tool_name, tool_input)
                        except MCPError as e:
                            result = {"error": str(e)}

                print(f"  → {json.dumps(result, default=str)}")

                audit.log_tool_call(tool_name, tool_input, result, reasoning=reasoning)

                if tool_name == "scale_deployment" and "error" not in result:
                    old_r = result.get("old_replicas", 0)
                    new_r = result.get("new_replicas", tool_input.get("replicas", 0))
                    audit.log_scale(
                        old_replicas=old_r,
                        new_replicas=new_r,
                        direction="up" if new_r > old_r else "down",
                        reasoning=tool_input.get("reason", ""),
                        dry_run=dry_run,
                    )

                tool_response_parts.append(
                    Part.from_function_response(
                        name=tool_name,
                        response={"output": result},
                    )
                )

            response = chat.send_message(tool_response_parts)

        else:
            print(f"\n[warn] Reached max turns ({MAX_AGENT_TURNS}) — stopping.")
            audit.log("max_turns_reached", {"turns": turns})

    if audit_bucket:
        audit.upload_to_gcs(audit_bucket)

    summary = audit.summary()
    print(f"\n── Run Summary ──────────────────────────────")
    print(json.dumps(summary, indent=2))


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="turtlecrawl v2 — LLM-supervised KEDA scaling agent"
    )
    parser.add_argument("--deployment",          required=True)
    parser.add_argument("--namespace",           default="turtlecrawl")
    parser.add_argument("--target-vus",          type=int, default=100)
    parser.add_argument("--max-replicas",        type=int, default=20)
    parser.add_argument(
        "--base-url",
        default="http://sample-app.turtlecrawl.svc.cluster.local:8080",
    )
    parser.add_argument("--dry-run",             action="store_true")
    parser.add_argument("--load-in-cluster",     action="store_true",
                        help="Run k6 as a kubectl pod inside the cluster")
    parser.add_argument("--audit-bucket",        help="GCS bucket for audit log upload")
    parser.add_argument("--project",             help="GCP project ID (overrides gcloud config)")
    parser.add_argument("--region",              default="us-central1")
    parser.add_argument("--model",               default="gemini-2.5-flash")
    parser.add_argument("--keda-scaledobject",   default="sample-app-scaler",
                        help="Name of the KEDA ScaledObject to supervise")

    args = parser.parse_args()

    if args.project:
        os.environ["GCP_PROJECT_ID"] = args.project
    if args.region:
        os.environ["GCP_REGION"] = args.region
    if args.model:
        os.environ["GEMINI_MODEL"] = args.model

    run_agent(
        deployment=args.deployment,
        namespace=args.namespace,
        target_vus=args.target_vus,
        max_replicas=args.max_replicas,
        base_url=args.base_url,
        dry_run=args.dry_run,
        audit_bucket=args.audit_bucket,
        load_in_cluster=args.load_in_cluster,
        keda_scaledobject=args.keda_scaledobject,
    )


if __name__ == "__main__":
    main()
