"""
Audit logging for turtlecrawl agent decisions.

Writes JSON audit records locally and optionally uploads to GCS.
Every scale decision — up or down, dry-run or live — is recorded.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, deployment: str, namespace: str, run_id: str | None = None):
        self.deployment = deployment
        self.namespace = namespace
        self.run_id = run_id or f"run-{int(time.time())}"
        self.events: list[dict] = []
        self._local_path = Path(f"/tmp/turtlecrawl-audit-{self.run_id}.jsonl")

    def log(self, event_type: str, data: dict[str, Any], reasoning: str = "") -> None:
        """Append an audit event."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "deployment": self.deployment,
            "namespace": self.namespace,
            "event_type": event_type,
            "reasoning": reasoning,
            **data,
        }
        self.events.append(record)
        # Write to local JSONL immediately (crash-safe)
        with self._local_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

        print(f"  [audit] {event_type}: {json.dumps(data, default=str)}")

    def log_tool_call(self, tool_name: str, inputs: dict, result: dict, reasoning: str = "") -> None:
        self.log(
            "tool_call",
            {"tool": tool_name, "inputs": inputs, "result": result},
            reasoning=reasoning,
        )

    def log_scale(
        self,
        old_replicas: int,
        new_replicas: int,
        direction: str,
        reasoning: str,
        dry_run: bool = False,
    ) -> None:
        self.log(
            "scale_decision",
            {
                "old_replicas": old_replicas,
                "new_replicas": new_replicas,
                "direction": direction,
                "dry_run": dry_run,
            },
            reasoning=reasoning,
        )

    def upload_to_gcs(self, bucket: str | None = None) -> bool:
        """Upload the local JSONL audit file to GCS."""
        bucket = bucket or os.getenv("AUDIT_BUCKET")
        if not bucket:
            print("  [audit] No GCS bucket configured — skipping upload")
            return False

        try:
            from google.cloud import storage  # type: ignore
            client = storage.Client()
            b = client.bucket(bucket)
            blob_name = f"audit/{self.run_id}.jsonl"
            blob = b.blob(blob_name)
            blob.upload_from_filename(str(self._local_path))
            print(f"  [audit] Uploaded to gs://{bucket}/{blob_name}")
            return True
        except ImportError:
            print("  [audit] google-cloud-storage not installed — skipping GCS upload")
        except Exception as e:
            print(f"  [audit] GCS upload failed: {e}")
        return False

    def summary(self) -> dict:
        """Return a summary of this run."""
        scale_events = [e for e in self.events if e["event_type"] == "scale_decision"]
        return {
            "run_id": self.run_id,
            "total_events": len(self.events),
            "scale_decisions": len(scale_events),
            "scale_ups": sum(1 for e in scale_events if e.get("direction") == "up"),
            "scale_downs": sum(1 for e in scale_events if e.get("direction") == "down"),
            "local_log": str(self._local_path),
        }
