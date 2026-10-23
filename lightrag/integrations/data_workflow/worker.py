"""Detached worker for scan, conversion, fixed-file delivery, and CLI completion."""

from __future__ import annotations

import argparse
import json
import os
import traceback
from pathlib import Path

from .config import BridgeSettings
from .batch_source import BatchSnapshot, read_batch_results
from .conversion import convert_result
from .execution import execute
from .inputs import read_results
from .progress import process_identity
from .storage import ACTIVE, JobStore, timestamp


def prepare(
    store: JobStore, job_id: str, settings: BridgeSettings, directory: Path
) -> None:
    seen = accepted = rejected = 0
    if store.get(job_id).get("source", {}).get("type") == "batch":
        snapshot = BatchSnapshot.model_validate_json(
            (directory / "batch-source.json").read_text()
        )
        results = read_batch_results(snapshot, settings.limits)
    else:
        results = read_results(settings)
    with (
        (directory / "input.jsonl.part").open("w", encoding="utf-8") as output,
        (directory / "rejected.jsonl").open("w", encoding="utf-8") as failures,
    ):
        for result, read_error in results:
            seen += 1
            try:
                if read_error:
                    raise ValueError(read_error)
                record = convert_result(result)
            except ValueError as exc:
                rejected += 1
                failures.write(
                    json.dumps(
                        {"record_index": seen, "reason": str(exc)}, ensure_ascii=False
                    )
                    + "\n"
                )
            else:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                accepted += 1
            if seen == 1 or seen % 100 == 0:
                store.update(
                    job_id,
                    phase="converting",
                    seen_sources=seen,
                    accepted_sources=accepted,
                    rejected_sources=rejected,
                )
        output.flush()
        os.fsync(output.fileno())
    store.update(
        job_id, seen_sources=seen, accepted_sources=accepted, rejected_sources=rejected
    )
    if not accepted:
        raise ValueError(
            "No valid original-text records to deliver; inspect rejected.jsonl"
        )
    (directory / "input.jsonl.part").replace(directory / "input.jsonl")


def run(store: JobStore, job_id: str, operation: str) -> None:
    record = store.get(job_id)
    directory = Path(record["directory"])
    try:
        store.update(
            job_id,
            expected=ACTIVE,
            worker_pid=os.getpid(),
            worker_identity=process_identity(os.getpid()),
        )
        settings = BridgeSettings.model_validate_json(
            (directory / "settings.json").read_text()
        )
        if operation != "run":
            prepare(store, job_id, settings, directory)
            if operation == "prepare":
                store.update(job_id, status="ready", phase="ready")
                return
        store.update(
            job_id, status="running", phase="processing", started_at=timestamp()
        )
        receipt = execute(settings, directory)
        store.update(
            job_id,
            status=receipt["status"],
            phase=receipt["status"],
            result=receipt,
            finished_at=timestamp(),
        )
    except Exception as exc:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "stderr.log").open("a", encoding="utf-8") as log:
            traceback.print_exc(file=log)
        store.update(
            job_id,
            status="failed",
            phase="failed",
            error=str(exc),
            finished_at=timestamp(),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--operation", choices=("prepare", "prepare-run", "run"), required=True
    )
    args = parser.parse_args()
    run(JobStore(args.store), args.job_id, args.operation)


if __name__ == "__main__":
    main()
