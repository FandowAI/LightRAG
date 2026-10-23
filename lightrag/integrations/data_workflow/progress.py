"""Read committed data_studio progress without treating QA counts as source counts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .storage import ACTIVE, TERMINAL, JobConflict, JobStore


def process_identity(pid: int) -> str | None:
    try:
        # Linux start ticks protect against a recycled PID, including zombie workers.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except FileNotFoundError:
        return None


def reconcile(store: JobStore, record: dict) -> dict:
    if record["status"] not in ACTIVE:
        return record
    pid = record.get("worker_pid")
    if pid is None:
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(record["updated_at"])
        ).total_seconds()
        alive = age < 60
    else:
        try:
            os.kill(pid, 0)
            identity = process_identity(pid)
            alive = identity is not None and identity == record.get("worker_identity")
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
    if not alive:
        try:
            return store.update(
                record["job_id"],
                expected=ACTIVE,
                status="interrupted",
                phase="interrupted",
                error="Worker stopped unexpectedly; inspect retained artifacts before a new submission",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except JobConflict:
            return store.get(record["job_id"])
    return record


def public_job(record: dict) -> dict:
    directory = Path(record["directory"])
    receipt = record.get("result")
    manifest = None
    manifests = list((directory / "outputs").glob("*/run.json"))
    if len(manifests) == 1:
        try:
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Atomic publisher may be between open and replace; keep progress unknown.
            manifest = None
    summary = (
        receipt.get("summary", {}) if receipt else (manifest or {}).get("summary", {})
    )
    processed = summary.get("sources")
    accepted = record.get("accepted_sources", 0)
    percent = (
        round(processed * 100 / accepted, 2)
        if (type(processed) is int and accepted > 0 and 0 <= processed <= accepted)
        else None
    )
    status = record["status"]
    return {
        "schema_version": 1,
        **{
            key: record.get(key)
            for key in (
                "job_id",
                "request_id",
                "status",
                "phase",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "error",
            )
        },
        "source": record.get("source", {"type": "directory"}),
        "terminal": status in TERMINAL,
        "can_start": status == "ready",
        "poll_after_ms": 2000 if status in ACTIVE else 0,
        "progress": {
            "unit": "sources",
            "percent": percent,
            "seen_sources": record.get("seen_sources", 0),
            "accepted_sources": accepted,
            "rejected_sources": record.get("rejected_sources", 0),
            "processed_sources": processed,
            "committed_batch": (manifest or {}).get("checkpoint", {}).get("batch_id"),
        },
        "result": receipt,
        "artifacts": {
            "directory": str(directory),
            "input": str(directory / "input.jsonl"),
            "config": str(directory / "job.yaml"),
            "rejected": str(directory / "rejected.jsonl"),
            "stdout": str(directory / "stdout.log"),
            "stderr": str(directory / "stderr.log"),
        },
    }
