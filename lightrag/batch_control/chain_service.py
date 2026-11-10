"""Authorize immutable task commands; a dispatcher admits them to owned RAG workers."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from lightrag.integrations.data_workflow.batch_source import (
    resolve_batch,
    read_batch_results,
)
from lightrag.integrations.data_workflow.config import load_settings

from .resources import digest
from .runtime import RuntimeSupervisor
from .runtime_process import ProcessBackend
from .storage import Conflict, IntegrityError
from .task_contracts import TaskCommand, RetrievalOptions, QueryOptions
from .task_sources import freeze_inputs
from .task_store import TaskStore, TERMINAL, public_task
from .task_workflow import workflow_fingerprint


def retrieval_root(paths, workspace):
    from .bindings import BindingStore
    from .storage import Registry
    from pathlib import Path

    registry = Registry(paths.registry)
    if paths.registry.is_file():
        with registry.transaction() as db:
            row = db.execute(
                "SELECT id FROM batches WHERE workspace=?", (workspace,)
            ).fetchone()
        binding = BindingStore(registry).get(row[0]) if row else None
        if binding and binding["binding"]["retrieval_directory"]:
            return Path(binding["binding"]["retrieval_directory"])
    return paths.under(paths.runtime_root, "retrieval", workspace)


class ChainService:
    def __init__(self, control, policy, *, actor, backend=None):
        self.control, self.policy, self.actor = control, policy, actor
        self.tasks = TaskStore(control.registry)
        self.runtime = RuntimeSupervisor(control, policy, actor=actor, backend=backend)

    def submit(self, batch, command: TaskCommand):
        self.control.allow(self.actor, "submit", batch)
        self.control.allow(self.actor, "run_runtime", batch)
        if command.source.data_batch_id != batch:
            raise ValueError("Task source belongs to another data batch")
        # Retry identity is based on the request, never on a newly enumerated directory.
        fingerprint = digest(command.model_dump())
        previous = self.control.registry.previous(
            self.actor, "task", batch, command.request_id, fingerprint
        )
        if previous is not None:
            return self.get(batch, previous["task_id"])
        owner, record, document, _, _ = self.control.read(
            self.actor, batch, command.expected_revision
        )
        settings = load_settings(
            self.control.paths.revision(batch, record["id"]) / "data_workflow.yaml"
        )
        bridge = settings.model_dump(mode="json")
        source = command.source
        payload = {
            "version": 1,
            "command": command.model_dump(),
            "created_at": time.time(),
        }
        if command.kind in {"chain", "distillation"}:
            stamp = workflow_fingerprint(settings)
            if not self.control.catalog.environments[
                document.bridge.environment_id
            ].code_revision.startswith(stamp["commit"]):
                raise IntegrityError(
                    "Selected workflow commit changed after configuration publication"
                )
            payload["workflow_fingerprint"] = stamp
        if source.type == "directory":
            if set(source.input_ids) - set(document.bridge.input_ids):
                raise ValueError("Inputs do not belong to the pinned configuration")
            for ref in source.input_ids:
                self.control.allow(self.actor, "use_input", ref)
            payload["inputs"] = freeze_inputs(
                self.control.catalog,
                source.input_ids,
                bridge,
                documents=command.kind != "distillation",
            )
        if source.type == "retrieval":
            self.control.allow(
                self.actor, "read_retrieval", batch + ":" + source.retrieval_job_id
            )
            snapshot = resolve_batch(
                SimpleNamespace(
                    workspace=owner["workspace"],
                    working_dir=str(self.control.paths.runtime_root),
                ),
                source.retrieval_job_id,
                root=retrieval_root(self.control.paths, owner["workspace"]),
            )
            # Checksums are validated before registration and again before conversion.
            list(read_batch_results(snapshot, settings.limits))
            payload["retrieval_source"] = snapshot.model_dump(mode="json")
        if command.kind in {"retrieval", "chain"}:
            defaults = document.retrieval.get("batch", {})
            values = {
                **defaults,
                **(
                    command.retrieval.model_dump(exclude_unset=True)
                    if command.retrieval
                    else {}
                ),
            }
            payload["retrieval"] = RetrievalOptions.model_validate(values).model_dump()
        if command.kind == "query":
            payload["query"] = QueryOptions.model_validate(
                {
                    **document.retrieval.get("ordinary", {}),
                    **command.query.model_dump(exclude_unset=True),
                }
            ).model_dump()
        task = self.control.register_task(
            self.actor,
            batch,
            command.expected_revision,
            command.request_id,
            command.kind,
            source.model_dump(exclude_none=True),
            payload=payload,
            request_fingerprint=fingerprint,
        )
        return public_task(task)

    def get(self, batch, task):
        self.control.allow(self.actor, "read", batch)
        return self._public(self.tasks.get(batch, task))

    def _public(self, task):
        value = public_task(task)
        receipt = ((task.get("result") or {}).get("distillation") or {}).get("result")
        value["can_export_training"] = bool(
            value["terminal"]
            and task["state"] in {"completed", "completed_with_errors"}
            and task["kind"] in {"chain", "distillation"}
            and receipt
            and receipt.get("status") in {"completed", "completed_with_errors"}
            and self.control.authorize(
                self.actor, "export_training", task["data_batch_id"]
            )
        )
        if not value["terminal"]:
            status = self.provider_admission(task["data_batch_id"], task["task_id"])
            value["provider_admission"] = {
                "queued": sum(item["queued"] for item in status["items"]),
                "active": sum(item["active"] for item in status["items"]),
            }
        return value

    def provider_admission(self, batch, task=None):
        from .provider_quota.core import QuotaStore
        from .provider_quota.wiring import registry_path

        self.control.allow(self.actor, "read", batch)
        if task is not None:
            self.tasks.get(batch, task)
        path = registry_path(self.control.paths)
        return QuotaStore(path).summary(batch, task) if path.exists() else {"items": []}

    def list(self, batch):
        self.control.allow(self.actor, "read", batch)
        return [self._public(task) for task in self.tasks.list(batch)]

    def cancel(self, batch, task):
        self.control.allow(self.actor, "cancel_task", batch)
        return public_task(self.tasks.cancel(batch, task))

    def retry(self, batch, task, expected_revision, request_id):
        self.control.allow(self.actor, "read", batch)
        previous = self.tasks.get(batch, task)
        if previous["state"] not in {
            "failed",
            "cancelled",
            "interrupted",
            "completed_with_errors",
        }:
            raise Conflict("Only unsuccessful terminal tasks can be retried")
        command = TaskCommand.model_validate(
            {
                **previous["payload"]["command"],
                "expected_revision": expected_revision,
                "request_id": request_id,
            }
        )
        return self.submit(batch, command)

    def result_path(self, batch, task):
        self.control.allow(self.actor, "read", batch)
        record = self.tasks.get(batch, task)
        if record["state"] not in TERMINAL:
            raise Conflict("Wait for task completion before reading its result")
        owner = self.control.registry.get(batch)
        path = self.control.paths.under(
            self.control.paths.runtime_root,
            "tasks",
            owner["namespace"],
            task,
            "result.json",
        )
        if path != path.resolve() or not path.is_file():
            raise KeyError("Task result is unavailable")
        return path

    def artifact_path(self, batch, task_id, artifact):
        self.control.allow(self.actor, "read", batch)
        task = self.tasks.get(batch, task_id)
        if task["state"] not in TERMINAL:
            raise Conflict("Wait for task completion before exporting artifacts")
        owner = self.control.registry.get(batch)
        result = task.get("result") or {}
        if artifact.startswith("retrieval-"):
            job_id = (result.get("retrieval") or {}).get("job_id")
            if job_id is None:
                raise KeyError("No retrieval publication for this task")
            snapshot = resolve_batch(
                SimpleNamespace(
                    workspace=owner["workspace"],
                    working_dir=str(self.control.paths.runtime_root),
                ),
                job_id,
                root=retrieval_root(self.control.paths, owner["workspace"]),
            )
            settings = load_settings(
                self.control.paths.revision(batch, task["config_revision_id"])
                / "data_workflow.yaml"
            )
            list(read_batch_results(snapshot, settings.limits))
            filename = {
                "retrieval-packages": "packages.jsonl",
                "retrieval-sources": "sources.jsonl",
                "retrieval-manifest": "manifest.json",
            }[artifact]
            path = snapshot.directory / filename
        else:
            root = self.control.paths.under(
                self.control.paths.runtime_root,
                "tasks",
                owner["namespace"],
                task_id,
                "workflow",
            )
            if artifact == "workflow-input":
                path = root / "input.jsonl"
            else:
                receipt = (result.get("distillation") or {}).get("result")
                if receipt is None:
                    raise KeyError("No confirmed workflow receipt for this task")
                output = self.control.paths.under(root, "outputs", receipt["run_id"])
                if str(output) != receipt["directory"]:
                    raise IntegrityError(
                        "Workflow result directory disagrees with its task"
                    )
                path = (
                    output
                    / {"workflow-review": "review.jsonl", "workflow-run": "run.json"}[
                        artifact
                    ]
                )
        if path != path.resolve() or not path.is_file():
            raise KeyError("Artifact is unavailable")
        return path

    def artifacts(self, batch, task):
        self.control.allow(self.actor, "read", batch)
        self.tasks.get(batch, task)
        available = []
        for name in (
            "retrieval-packages",
            "retrieval-sources",
            "retrieval-manifest",
            "workflow-input",
            "workflow-review",
            "workflow-run",
        ):
            try:
                path = self.artifact_path(batch, task, name)
            except KeyError:
                continue
            available.append(
                {"artifact": name, "name": path.name, "bytes": path.stat().st_size}
            )
        return {"items": available}


class BatchDispatcher:
    """Only queued tasks restart admission; interrupted paid work is never replayed."""

    def __init__(self, control, policy, *, backend=None):
        self.control, self.policy = control, policy
        self.store = TaskStore(control.registry)
        self.stop_event = asyncio.Event()
        self.last_tick_at = None
        self.last_error = None
        self.backend = backend or ProcessBackend()

    def tick(self):
        for task in self.store.list(states={"queued", "admitted", "running"}):
            batch, task_id = task["data_batch_id"], task["task_id"]
            service = ChainService(
                self.control, self.policy, actor=task["actor"], backend=self.backend
            )
            try:
                if task["state"] in {"admitted", "running"}:
                    runtime = service.runtime.store.get(batch)
                    ownership = (
                        service.runtime.backend.ownership(runtime)
                        if runtime
                        else "gone"
                    )
                    if (
                        ownership in {"gone", "reused"}
                        and runtime
                        and runtime["state"] not in {"failed", "stopped"}
                    ):
                        # Reclaim only through the supervisor's batch lock/CAS.
                        # Revoked read permission retains the slot for an operator.
                        try:
                            service.runtime.inspect(batch)
                        except PermissionError:
                            pass
                    if (
                        runtime is None
                        or runtime["state"] in {"failed", "stopped"}
                        or runtime["generation"] != task.get("generation")
                        or ownership in {"gone", "reused"}
                    ):
                        self.store.update(
                            batch,
                            task_id,
                            event="owner_lost",
                            state="interrupted",
                            phase="interrupted",
                            error="Owning runtime exited; retained artifacts require explicit review and a new submission",
                        )
                    continue
                self.control.allow(task["actor"], "submit", batch)
                self.control.allow(task["actor"], "run_runtime", batch)
                if self.store.list(batch, states={"admitted", "running"}):
                    raise Conflict("Another task owns this batch runtime")
                owner = self.control.registry.get(batch)
                if (
                    self.control.registry.revision(batch, owner["active"])[
                        "components"
                    ]["rag"]
                    != task["components"]["rag"]
                ):
                    raise IntegrityError("Task requires a retired RAG startup revision")
                # start also checks source/environment policy before reusing a process.
                lease = service.runtime.start(batch, owner["active"])
                self.store.admit(batch, task_id, lease)
            except Conflict as exc:
                self.store.update(batch, task_id, waiting_reason=str(exc))
            except (
                PermissionError,
                ValueError,
                KeyError,
                OSError,
                RuntimeError,
            ) as exc:
                # Public errors are bounded classifications; private logs retain details.
                from lightrag.utils import logger

                logger.exception("Managed batch task admission failed: %s", task_id)
                if task["state"] == "queued":
                    self.store.update(
                        batch,
                        task_id,
                        event="admission_failed",
                        state="failed",
                        phase="failed",
                        error=type(exc).__name__
                        + ": task admission failed; inspect server logs",
                    )

    async def run(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self.tick)
                self.last_tick_at = time.time()
                self.last_error = None
            except Exception as exc:
                from lightrag.utils import logger

                logger.exception(
                    "Batch dispatcher tick failed; queued work remains durable"
                )
                self.last_error = type(exc).__name__
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def close(self):
        # Workers own their process lifespans; manager shutdown never kills them.
        self.stop_event.set()
