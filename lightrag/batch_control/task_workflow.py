"""Execute a pinned workflow through existing conversion, CLI and receipt contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from lightrag.api.server_files import ServerFiles
from lightrag.integrations.data_workflow.config import (
    write_config_snapshot,
    write_job_config,
)
from lightrag.integrations.data_workflow.execution import execute
from lightrag.integrations.data_workflow.progress import process_identity, public_job
from lightrag.integrations.data_workflow.storage import JobStore, timestamp
from lightrag.integrations.data_workflow.worker import prepare

from .runtime_config import clean_environment
from .task_sources import open_snapshot
from .provider_quota.wiring import workflow_launcher


FINGERPRINT_SCRIPT = """
import hashlib, importlib.metadata, json, pathlib, subprocess, sys
project = pathlib.Path(sys.argv[1])
source = hashlib.sha256()
for path in sorted((project / 'src').rglob('*.py')):
    source.update(str(path.relative_to(project)).encode())
    source.update(path.read_bytes())
packages = sorted({(d.metadata['Name'].lower().replace('_','-'), d.version)
    for d in importlib.metadata.distributions() if d.metadata['Name']})
head = subprocess.run(['git','rev-parse','HEAD'], cwd=project, capture_output=True, text=True, check=True).stdout.strip()
print(json.dumps({'source': source.hexdigest(),
    'lock': hashlib.sha256((project/'uv.lock').read_bytes()).hexdigest(),
    'installed': hashlib.sha256(json.dumps(packages).encode()).hexdigest(),
    'python': list(sys.version_info[:3]), 'commit': head}))
"""


def workflow_fingerprint(settings):
    environment = clean_environment(settings.project_directory)
    environment["PYTHONPATH"] = str(settings.project_directory / "src")
    result = subprocess.run(
        [
            str(settings.python),
            "-c",
            FINGERPRINT_SCRIPT,
            str(settings.project_directory),
        ],
        cwd=settings.project_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


class WorkflowRun:
    """A RAG worker owns preparation and waits for the workflow's terminal receipt."""

    def __init__(self, paths, owner, task, settings, recipe):
        self.paths, self.owner, self.task = paths, owner, task
        self.settings, self.recipe = settings, recipe
        self.directory = paths.under(
            paths.runtime_root, "tasks", owner["namespace"], task["task_id"], "workflow"
        )
        self.store = JobStore(
            paths.under(
                paths.runtime_root, "data-workflow", owner["namespace"], "jobs.sqlite3"
            )
        )
        self.started = False

    def run(self, source=None, *, cancelled=lambda: False):
        task = self.task
        if (
            workflow_fingerprint(self.settings)
            != task["payload"]["workflow_fingerprint"]
        ):
            raise ValueError(
                "Workflow source, lock or installed environment changed after submission"
            )
        record, created = self.store.reserve(
            {
                "job_id": task["task_id"],
                "request_id": task["task_id"],
                "source": {"type": "batch", "batch_id": source.batch_id}
                if source
                else {"type": "directory"},
                "status": "preparing",
                "phase": "scanning",
                "directory": str(self.directory),
                "created_at": timestamp(),
                "updated_at": timestamp(),
                "worker_pid": os.getpid(),
                "worker_identity": process_identity(os.getpid()),
                "seen_sources": 0,
                "accepted_sources": 0,
                "rejected_sources": 0,
            },
            self.settings.limits.max_active_jobs,
        )
        if not created:
            raise ValueError(
                "Existing workflow attempt requires review; automatic replay is disabled"
            )
        self.started = True
        try:
            self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            settings = self.settings
            if source is not None:
                (self.directory / "batch-source.json").write_text(
                    source.model_dump_json(), encoding="utf-8"
                )
            else:
                inputs = task["payload"]["inputs"]
                reader = ServerFiles(roots=inputs["roots"])
                copies = []
                target = self.directory / "sources"
                target.mkdir(mode=0o700)
                for index, row in enumerate(inputs["files"]):
                    if cancelled():
                        raise InterruptedError("Task cancellation requested")
                    copy = target / (str(index) + Path(row["path"]).suffix)
                    with (
                        open_snapshot(reader, row, settings.limits.max_input_bytes)[
                            0
                        ] as stream,
                        copy.open("xb") as output,
                    ):
                        shutil.copyfileobj(stream, output)
                    copies.append(str(copy))
                settings = settings.model_copy(
                    update={
                        "input": settings.input.model_copy(update={"paths": copies})
                    }
                )
            write_config_snapshot(
                settings.workflow_config,
                self.directory,
                "settings.json",
                settings.model_dump_json(indent=2),
                config_root=self.paths.config_root,
            )
            write_job_config(
                settings.workflow_config,
                self.directory,
                recipe=self.recipe,
                config_root=self.paths.config_root,
            )
            prepare(self.store, task["task_id"], settings, self.directory)
            if cancelled():
                raise InterruptedError("Task cancellation requested")
            self.store.update(
                task["task_id"],
                status="running",
                phase="processing",
                started_at=timestamp(),
            )
            receipt = execute(
                settings,
                self.directory,
                cancel_requested=cancelled,
                child_environment=clean_environment(settings.project_directory),
                command_wrapper=workflow_launcher(
                    self.paths,
                    self.owner["id"],
                    task["task_id"],
                    self.recipe,
                    self.directory,
                ),
            )
            self.store.update(
                task["task_id"],
                status=receipt["status"],
                phase=receipt["status"],
                result=receipt,
                finished_at=timestamp(),
            )
            return self.status()
        except BaseException as exc:
            self.store.update(
                task["task_id"],
                status="interrupted" if isinstance(exc, InterruptedError) else "failed",
                phase="failed",
                error=type(exc).__name__,
                finished_at=timestamp(),
            )
            raise

    def status(self):
        return public_job(self.store.get(self.task["task_id"]))
