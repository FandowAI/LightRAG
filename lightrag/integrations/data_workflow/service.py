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
from collections.abc import Callable

from .batch_source import BatchSnapshot

from .config import (
    PROJECT_ROOT,
    configuration_path,
    load_settings,
    write_job_config,
    write_config_snapshot,
)
from .execution import validate_execution
from .progress import process_identity, public_job, reconcile
from .storage import ACTIVE, TERMINAL, JobConflict, JobStore, timestamp
from .recipe_settings import RecipeSettingsStore, RecipeValues
from .prompt_files import PromptFiles, validate_prompt, materialize_builtin_prompts


class DataWorkflowService:
    """Keep API requests short while a detached worker owns each complete run."""

    def __init__(
        self,
        workspace: str,
        config_path: Path | None = None,
        *,
        batch_lookup: Callable[[str], BatchSnapshot] | None = None,
    ):
        self.workspace = workspace
        self.config_path = config_path
        self.batch_lookup = batch_lookup

    def _resources(self):
        config = self.config_path or configuration_path()
        settings = load_settings(config)
        namespace = hashlib.sha256(self.workspace.encode("utf-8")).hexdigest()[:16]
        root = settings.jobs_directory / namespace
        return settings, JobStore(root / "jobs.sqlite3"), root

    def configuration(self) -> dict:
        settings, _, root = self._resources()
        issues = []
        try:
            validate_execution(
                settings, RecipeSettingsStore(settings, root).effective()
            )
        except (ValueError, OSError) as exc:
            issues.append(str(exc))
        return {
            "ready": not issues,
            "issues": issues,
            "config_file": str(self.config_path or configuration_path()),
            "workflow_config": str(settings.workflow_config),
            "input": settings.input.model_dump(),
            "jobs_directory": str(settings.jobs_directory),
            "project_directory": str(settings.project_directory),
            "python": str(settings.python),
            "credentials_file_configured": settings.credentials_file is not None,
            "limits": settings.limits.model_dump(),
        }

    def recipe_settings(self) -> dict:
        settings, _, root = self._resources()
        return RecipeSettingsStore(settings, root).get()

    def save_recipe_settings(self, revision: str, values: RecipeValues) -> dict:
        settings, _, root = self._resources()
        return RecipeSettingsStore(settings, root).save(revision, values)

    def prompt_files(self, stage: str) -> dict:
        settings, _, root = self._resources()
        return PromptFiles(settings, root.name).list(stage)

    def read_prompt(self, stage: str, path: str) -> dict:
        settings, _, root = self._resources()
        if not path:
            selected = RecipeSettingsStore(settings, root).effective()
            selected["workflow"]["parameters"][stage]["prompt_file"] = None
            recipe = materialize_builtin_prompts(settings, selected)
            path = recipe["workflow"]["parameters"][stage]["prompt_file"]
        return PromptFiles(settings, root.name).read(stage, path)

    def save_prompt(self, stage: str, filename: str, content: str) -> dict:
        settings, _, root = self._resources()
        return PromptFiles(settings, root.name).save(stage, filename, content)

    @staticmethod
    def preview_prompt(stage: str, content: str) -> dict:
        template, values = validate_prompt(stage, content)
        return {"preview": template.substitute(values)}

    def retry(self, job_id: str, request_id: str, start: bool = True) -> dict:
        _, store, _ = self._resources()
        previous = reconcile(store, store.get(job_id))
        retry_request = f"retry:{job_id}:{request_id}"
        existing = store.by_request(retry_request)
        if existing is not None:
            if existing.get("retry_of") != job_id:
                raise JobConflict("Retry request ID belongs to another operation")
            return self._existing(store, existing, start)
        if previous["status"] not in {"failed", "interrupted"}:
            raise JobConflict(
                "Only failed or interrupted jobs can be explicitly retried"
            )
        snapshot = None
        if previous.get("source", {}).get("type") == "batch":
            if self.batch_lookup is None:
                raise ValueError("Batch source lookup is unavailable")
            snapshot = self.batch_lookup(previous["source"]["batch_id"])
            if (
                snapshot.workspace != self.workspace
                or snapshot.batch_id != previous["source"]["batch_id"]
            ):
                raise ValueError("Batch source belongs to another workspace")
        return self._submit(retry_request, start, snapshot, retry_of=job_id)

    def submit_batch(self, batch_id: str, start: bool = True) -> dict:
        if self.batch_lookup is None:
            raise ValueError("Batch source lookup is unavailable")
        _, store, _ = self._resources()
        request_id = "batch:" + batch_id
        existing = store.by_request(request_id)
        if existing is not None:
            if existing.get("source", {}).get("batch_id") != batch_id:
                raise JobConflict("Request ID belongs to another source")
            return self._existing(store, existing, start)
        snapshot = self.batch_lookup(batch_id)
        if snapshot.workspace != self.workspace or snapshot.batch_id != batch_id:
            raise ValueError("Batch source belongs to another workspace")
        return self._submit(request_id, start, snapshot)

    def _existing(self, store: JobStore, record: dict, start: bool) -> dict:
        record = reconcile(store, record)
        if start and record["status"] == "ready":
            return self.start(record["job_id"])
        return public_job(record)

    def submit(self, request_id: str, start: bool = True) -> dict:
        if request_id.startswith("batch:"):
            raise ValueError("Batch request IDs are reserved")
        return self._submit(request_id, start)

    def _submit(
        self,
        request_id: str,
        start: bool,
        snapshot: BatchSnapshot | None = None,
        *,
        retry_of: str | None = None,
    ) -> dict:
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
                "retry_of": retry_of,
                "source": {"type": "batch", "batch_id": snapshot.batch_id}
                if snapshot
                else {"type": "directory"},
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
            return self._existing(store, record, start)
        try:
            directory = Path(record["directory"])
            directory.mkdir(parents=True, exist_ok=False)
            # No credential values are copied. Workers use an explicit credential file.
            write_config_snapshot(
                settings.workflow_config,
                directory,
                "settings.json",
                settings.model_dump_json(indent=2),
            )
            if snapshot is not None:
                (directory / "batch-source.json").write_text(
                    snapshot.model_dump_json(indent=2), encoding="utf-8"
                )
            recipe = RecipeSettingsStore(settings, root).effective()
            recipe = materialize_builtin_prompts(settings, recipe)
            write_job_config(settings.workflow_config, directory, recipe=recipe)
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

    def list(self, limit: int, offset: int, source_batch_id: str | None = None) -> dict:
        _, store, _ = self._resources()
        records, total = store.list(limit, offset, source_batch_id)
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
