"""Start-button orchestration; rendering and workflow execution stay independent."""

from __future__ import annotations

import hashlib
import codecs
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .config import PROJECT_ROOT, configuration_path, load_settings, write_job_config
from .progress import process_identity, public_job, reconcile
from .storage import ACTIVE, TERMINAL, JobConflict, JobStore, timestamp


class DataWorkflowService:
    """Keep API requests short while a detached worker owns each complete run."""

    def __init__(self, workspace: str, config_path: Path | None = None):
        self.workspace = workspace
        self.config_path = config_path

    def _resources(self):
        config = self.config_path or configuration_path()
        settings = load_settings(config)
        namespace = hashlib.sha256(self.workspace.encode("utf-8")).hexdigest()[:16]
        root = settings.jobs_directory / namespace
        return settings, JobStore(root / "jobs.sqlite3"), root

    def configuration(self) -> dict:
        settings, _, _ = self._resources()
        return {
            "config_file": str(self.config_path or configuration_path()),
            "workflow_config": str(settings.workflow_config),
            "input": settings.input.model_dump(),
            "jobs_directory": str(settings.jobs_directory),
            "project_directory": str(settings.project_directory),
            "python": str(settings.python),
            "credentials_file_configured": settings.credentials_file is not None,
            "limits": settings.limits.model_dump(),
        }

    def submit(self, request_id: str, start: bool = True) -> dict:
        settings, store, root = self._resources()
        # Dead workers must not permanently consume the per-workspace run capacity.
        for record in store.active():
            reconcile(store, record)
        job_id = "distill-" + uuid.uuid4().hex
        now = timestamp()
        record, created = store.reserve(
            {
                "job_id": job_id,
                "request_id": request_id,
                "status": "preparing",
                "phase": "scanning",
                "created_at": now,
                "updated_at": now,
                "directory": str(root / job_id),
                "worker_pid": None,
                "worker_identity": None,
                "seen_sources": 0,
                "accepted_sources": 0,
                "rejected_sources": 0,
            },
            settings.limits.max_active_jobs,
        )
        if not created:
            record = reconcile(store, record)
            if start and record["status"] == "ready":
                return self.start(record["job_id"])
            return public_job(record)
        try:
            directory = Path(record["directory"])
            directory.mkdir(parents=True, exist_ok=False)
            # No credential values are copied. Workers use an explicit credential file.
            (directory / "settings.json").write_text(
                settings.model_dump_json(indent=2), encoding="utf-8"
            )
            write_job_config(settings.workflow_config, directory)
            self._spawn(store, record, "prepare-run" if start else "prepare")
        except Exception as exc:
            store.update(
                job_id,
                status="failed",
                phase="failed",
                error=str(exc),
                finished_at=timestamp(),
            )
        return public_job(store.get(job_id))

    def start(self, job_id: str) -> dict:
        settings, store, _ = self._resources()
        for active in store.active():
            reconcile(store, active)
        record, claimed = store.claim_start(job_id, settings.limits.max_active_jobs)
        if claimed:
            try:
                self._spawn(store, record, "run")
            except Exception as exc:
                store.update(
                    job_id,
                    status="failed",
                    phase="failed",
                    error=str(exc),
                    finished_at=timestamp(),
                )
        return public_job(store.get(job_id))

    @staticmethod
    def _spawn(store: JobStore, record: dict, operation: str) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                (
                    str(PROJECT_ROOT),
                    environment.get("PYTHONPATH"),
                ),
            )
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lightrag.integrations.data_workflow.worker",
                "--store",
                str(store.path),
                "--job-id",
                record["job_id"],
                "--operation",
                operation,
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            # The worker may already have completed; never overwrite a terminal state.
            store.update(
                record["job_id"],
                expected=ACTIVE,
                worker_pid=process.pid,
                worker_identity=process_identity(process.pid),
            )
        except JobConflict:
            pass
        finally:
            threading.Thread(target=process.wait, daemon=True).start()

    def get(self, job_id: str) -> dict:
        _, store, _ = self._resources()
        return public_job(reconcile(store, store.get(job_id)))

    def list(self, limit: int, offset: int) -> dict:
        _, store, _ = self._resources()
        records, total = store.list(limit, offset)
        return {
            "items": [public_job(reconcile(store, record)) for record in records],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def logs(self, job_id: str, stream: str, offset: int, limit: int) -> dict:
        _, store, _ = self._resources()
        record = store.get(job_id)
        directory = Path(record["directory"])
        path = directory / f"{stream}.log"
        if not path.exists():
            return {
                "text": "",
                "offset": offset,
                "next_offset": offset,
                "has_more": False,
            }
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(limit)
            has_more = bool(handle.read(1))
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(raw, final=not has_more and record["status"] in TERMINAL)
        buffered = len(decoder.getstate()[0])
        return {
            "text": text,
            "offset": offset,
            "next_offset": offset + len(raw) - buffered,
            "has_more": has_more or bool(buffered),
        }
