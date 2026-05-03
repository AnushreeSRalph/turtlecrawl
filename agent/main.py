#!/usr/bin/env python3
"""
turtlecrawl — LLM-driven Kubernetes scaling agent.

Uses gemini-2.5-flash on Vertex AI — auth via existing gcloud ADC.
No API key needed. Free while GCP credits last.

Usage:
    python main.py --deployment sample-app --namespace turtlecrawl \
        --target-vus 100 --max-replicas 10 --dry-run

    python main.py --deployment sample-app --namespace turtlecrawl \
        --target-vus 100 --max-replicas 10
"""
import argparse
import json
import os
import sys
from typing import Any

import vertexai
from vertexai.generative_models import (
    GenerativeModel,
    GenerationConfig,
    Part,
)

from audit import AuditLogger
from prompts import SYSTEM_PROMPT
from tools import GEMINI_TOOLS, execute_tool

GCP_PROJECT = (
    os.getenv("GCP_PROJECT_ID")
    or os.popen("gcloud config get-value project 2>/dev/null").read().strip()
)
GCP_REGION  = os.getenv("GCP_REGION", "us-central1")
MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

MAX_TOKENS      = 4096
MAX_AGENT_TURNS = 30


def build_initial_message(
    deployment: str,
    namespace: str,
    target_vus: int,
    max_replicas: int,
    base_url: str,
    dry_run: bool,
) -> str:
    mode = "DRY RUN (describe what you would do, don't execute scale_deployment)" if dry_run else "LIVE"
    return f"""
Start the turtlecrawl scaling experiment.

Mode: {mode}
Deployment: {deployment}
Namespace: {namespace}
Target load: {target_vus} VUs
Max replicas: {max_replicas}
App URL: {base_url}
SLO: p99 latency < 120ms, error rate < 1%

1. Gather baseline metrics and current replica count.
2. Run the load test at {target_vus} VUs.
3. Scale up or down as needed to meet the SLO.
4. Find the minimum safe replica count.
5. Report your final recommendation.
""".strip()


def run_agent(
    deployment: str,
    namespace: str,
    target_vus: int,
    max_replicas: int,
    base_url: str,
    dry_run: bool = False,
    audit_bucket: str | None = None,
    load_in_cluster: bool = False,
) -> None:

    if not GCP_PROJECT:
        print("ERROR: Could not determine GCP project.")
        print("       Run: gcloud config set project YOUR_PROJECT_ID")
        sys.exit(1)

    # Initialise Vertex AI — uses ADC automatically
    vertexai.init(project=GCP_PROJECT, location=GCP_REGION)

    model = GenerativeModel(
        MODEL,
        system_instruction=SYSTEM_PROMPT,
        tools=[GEMINI_TOOLS],
        generation_config=GenerationConfig(
            temperature=0.1,
            max_output_tokens=MAX_TOKENS,
        ),
    )

    audit = AuditLogger(deployment, namespace)

    print(f"\n{'='*60}")
    print(f"  turtlecrawl scaling agent (Gemini on Vertex AI)")
    print(f"  run_id   : {audit.run_id}")
    print(f"  project  : {GCP_PROJECT}  region: {GCP_REGION}")
    print(f"  model    : {MODEL}")
    print(f"  target   : {deployment}/{namespace}")
    print(f"  load     : {target_vus} VUs")
    print(f"  dry_run  : {dry_run}")
    print(f"{'='*60}\n")

    audit.log("agent_start", {
        "deployment":  deployment,
        "namespace":   namespace,
        "target_vus":  target_vus,
        "max_replicas": max_replicas,
        "base_url":    base_url,
        "dry_run":     dry_run,
        "model":       MODEL,
        "gcp_project": GCP_PROJECT,
        "gcp_region":  GCP_REGION,
    })

    # Start chat session
    chat  = model.start_chat()
    turns = 0

    # Kick off with the initial task message
    response = chat.send_message(
        build_initial_message(deployment, namespace, target_vus, max_replicas, base_url, dry_run)
    )

    while turns < MAX_AGENT_TURNS:
        turns += 1
        print(f"\n── Turn {turns} ──────────────────────────────")

        # Collect text and function calls from all parts
        text_parts     = []
        function_calls = []

        for candidate in response.candidates:
            for part in candidate.content.parts:
                # .text raises AttributeError on function-call parts in Vertex SDK
                try:
                    if part.text:
                        text_parts.append(part.text)
                except AttributeError:
                    pass
                if part.function_call and part.function_call.name:
                    function_calls.append(part.function_call)

        # Print any text the model produced
        if text_parts:
            print(f"\n[agent]\n{''.join(text_parts)}")

        # No function calls = model is done
        if not function_calls:
            print("\n✓ Agent completed.")
            break

        # Execute each tool call and collect responses
        tool_response_parts = []
        reasoning = "".join(text_parts)[-500:] if text_parts else ""

        for fc in function_calls:
            tool_name  = fc.name
            tool_input = dict(fc.args)

            print(f"\n[tool] {tool_name}({json.dumps(tool_input, default=str)})")

            result = execute_tool(
                tool_name,
                tool_input,
                deployment=deployment,
                namespace=namespace,
                dry_run=dry_run,
                max_replicas=max_replicas,
                load_in_cluster=load_in_cluster,
            )
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

            # Gemini expects function responses as Part objects
            tool_response_parts.append(
                Part.from_function_response(
                    name=tool_name,
                    response={"output": result},
                )
            )

        # Send all tool results back in one message
        response = chat.send_message(tool_response_parts)

    else:
        print(f"\n[warn] Reached max turns ({MAX_AGENT_TURNS}) — stopping.")
        audit.log("max_turns_reached", {"turns": turns})

    if audit_bucket:
        audit.upload_to_gcs(audit_bucket)

    summary = audit.summary()
    print(f"\n── Run Summary ──────────────────────────────")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="turtlecrawl — Gemini-driven k8s scaling agent"
    )
    parser.add_argument("--deployment",   required=True)
    parser.add_argument("--namespace",    default="turtlecrawl")
    parser.add_argument("--target-vus",   type=int, default=100)
    parser.add_argument("--max-replicas", type=int, default=20)
    parser.add_argument(
        "--base-url",
        default="http://sample-app.turtlecrawl.svc.cluster.local:8080",
    )
    parser.add_argument("--dry-run",         action="store_true")
    parser.add_argument("--load-in-cluster", action="store_true",
                        help="Run k6 as a kubectl pod inside the cluster (bypasses port-forward)")
    parser.add_argument("--audit-bucket")
    parser.add_argument("--project",      help="GCP project ID (overrides gcloud config)")
    parser.add_argument("--region",       default="us-central1", help="Vertex AI region")
    parser.add_argument("--model",        default="gemini-2.5-flash")

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
    )


if __name__ == "__main__":
    main()
