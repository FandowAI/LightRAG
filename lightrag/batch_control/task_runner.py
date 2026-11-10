"""Execute admitted commands inside the batch-owned RAG lifespan.

Never replay interrupted work automatically. See docs/design/BatchFullChain.md
for progress, cancellation and the boundaries of each business stage.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from lightrag.api.server_files import ServerFiles
from lightrag.base import QueryParam
from lightrag.batch_retrieval import BatchManager
from lightrag.entity_packages import PackageSettings
from lightrag.integrations.data_workflow.batch_source import (
    BatchSnapshot,
    resolve_batch,
)
from lightrag.integrations.data_workflow.config import load_settings

from .chain_service import retrieval_root
from .paths import ControlPaths
from .resources import VersionFiles
from .runtime_process import atomic_json, process_identity
from .storage import Registry, IntegrityError
from .task_contracts import TaskCommand, CenterOptions
from .task_sources import open_snapshot
from .task_store import TaskStore
from .task_workflow import WorkflowRun


class TaskEngine:
    def __init__(
        self,
        app,
        spec,
        task,
        *,
        manager_factory=BatchManager,
        workflow_factory=WorkflowRun,
    ):
        self.app, self.spec, self.task = app, spec, task
        self.rag = app.state.rag
        self.paths = ControlPaths(
            **{k: Path(v) for k, v in spec["control_paths"].items()}
        )
        self.registry = Registry(self.paths.registry)
        self.store = TaskStore(self.registry)
        self.manager_factory, self.workflow_factory = manager_factory, workflow_factory
        self.batch, self.task_id = task["data_batch_id"], task["task_id"]
        self.results = {}
        self.retrieval_manager = None
        self.workflow = None
        self.workflow_pending = None

    def update(self, **values):
        return self.store.update(
            self.batch, self.task_id, generation=self.spec["generation"], **values
        )

    def cancelled(self):
        return bool(self.store.get(self.batch, self.task_id).get("cancel_requested"))

    def check_cancelled(self):
        if self.cancelled():
            raise InterruptedError("Task cancellation requested")

    async def request(self, client, method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Managed ingress returned HTTP {response.status_code}; inspect worker.log"
            )
        return response.json()

    def configuration(self):
        owner = self.registry.get(self.batch)
        record = self.registry.revision(self.batch, self.task["config_revision_id"])
        document, _, manifest = VersionFiles(self.paths).read(owner, record)
        if (
            record["digest"] != self.task["manifest_digest"]
            or manifest["components"] != self.task["components"]
            or manifest["execution"] != self.task["execution"]
        ):
            raise IntegrityError("Task configuration changed after submission")
        if (
            owner["workspace"] != self.rag.workspace
            or self.task["components"]["rag"] != self.spec["startup"]["rag_digest"]
        ):
            raise IntegrityError(
                "Task configuration belongs to a different RAG runtime"
            )
        command = TaskCommand.model_validate(self.task["payload"]["command"])
        if (
            command.source.data_batch_id != self.batch
            or command.expected_revision != record["id"]
            or command.kind != self.task["kind"]
        ):
            raise IntegrityError("Task command disagrees with its frozen envelope")
        settings = load_settings(
            self.paths.revision(self.batch, record["id"]) / "data_workflow.yaml"
        )
        return owner, document, command, settings

    async def ingest(self, client, settings):
        inputs = self.task["payload"]["inputs"]
        reader = ServerFiles(roots=inputs["roots"])
        tracks = []
        upload_error = None
        for row in inputs["files"]:
            if self.cancelled():
                break
            try:
                snapshot, _, relative = await asyncio.to_thread(
                    open_snapshot, reader, row, settings.limits.max_input_bytes
                )
                try:
                    result = await self.request(
                        client,
                        "POST",
                        "/documents/upload",
                        files={"file": (Path(row["path"]).name, snapshot)},
                        data={"relative_path": relative},
                    )
                finally:
                    snapshot.close()
                if not result.get("track_id") or result.get("status") != "success":
                    raise RuntimeError(
                        "Managed upload did not return an accepted track identity"
                    )
            except Exception as exc:
                upload_error = exc
                break
            tracks.append(result["track_id"])
            self.results["ingest"] = {"track_ids": tracks}
            self.update(
                phase="ingest",
                progress={
                    "unit": "documents",
                    "total": len(inputs["files"]),
                    "uploaded": len(tracks),
                    "processed": 0,
                    "failed": 0,
                },
                result={"ingest": {"track_ids": tracks}},
            )
        cancellation_sent = False
        while True:
            if self.cancelled() and not cancellation_sent:
                await self.request(client, "POST", "/documents/cancel_pipeline")
                cancellation_sent = True
            documents = []
            for track in tracks:
                documents.extend(
                    (
                        await self.request(
                            client, "GET", "/documents/track_status/" + track
                        )
                    )["documents"]
                )
            states = [
                str(row["status"]).upper().rsplit(".", 1)[-1] for row in documents
            ]
            done = sum(state in {"PROCESSED", "SKIPPED", "FAILED"} for state in states)
            failed = states.count("FAILED")
            self.update(
                phase="ingest",
                progress={
                    "unit": "documents",
                    "total": len(tracks),
                    "uploaded": len(tracks),
                    "processed": done,
                    "failed": failed,
                },
            )
            pipeline = await self.request(client, "GET", "/documents/pipeline_status")
            idle = not any(
                pipeline.get(key) for key in ("busy", "scanning", "pending_enqueues")
            )
            if len(documents) >= len(tracks) and done == len(documents) and idle:
                self.check_cancelled()
                if upload_error is not None:
                    raise upload_error
                return {
                    "status": "completed_with_errors" if failed else "completed",
                    "track_ids": tracks,
                    "documents": [
                        {
                            "id": row["id"],
                            "status": row["status"],
                            "file_path": row.get("file_path"),
                        }
                        for row in documents
                    ],
                    "excluded": inputs["excluded"],
                }
            await asyncio.sleep(0.2)

    async def retrieve(self, owner, command):
        manager = self.manager_factory(
            self.rag, root=retrieval_root(self.paths, owner["workspace"])
        )
        job = await manager.start(
            command.centers, PackageSettings(**self.task["payload"]["retrieval"])
        )
        self.retrieval_manager, self.retrieval_job = manager, job["id"]
        self.results["retrieval"] = {"job_id": job["id"]}
        while True:
            if self.cancelled():
                manager.cancel(job["id"])
            current = manager.get(job["id"])
            self.update(
                phase="retrieval",
                progress={
                    "unit": "centers",
                    "total": current["total"],
                    "processed": current["processed"],
                    "failed": current["failed"],
                    "packages": current["package_count"],
                    "current_center": current.get("current_center"),
                },
                result=self.results,
            )
            if current["status"] not in {"queued", "running"}:
                # Join the actual task before another command can use the RAG.
                if manager.tasks:
                    await asyncio.gather(*manager.tasks)
                self.check_cancelled()
                if current["status"] not in {"completed", "completed_with_errors"}:
                    raise RuntimeError("Evidence retrieval did not complete")
                return {**current, "job_id": current["id"]}
            await asyncio.sleep(0.2)

    async def distill(self, owner, document, settings, source):
        workflow = self.workflow_factory(
            self.paths, owner, self.task, settings, document.recipe
        )
        self.workflow = workflow
        pending = asyncio.create_task(
            asyncio.to_thread(workflow.run, source, cancelled=self.cancelled)
        )
        self.workflow_pending = pending
        while not pending.done():
            if workflow.started:
                try:
                    status = workflow.status()
                except (OSError, ValueError):
                    # A progress-read failure must not detach a paid CLI run.
                    status = None
                if status is not None:
                    self.results["distillation"] = status
                    self.update(
                        phase="distillation",
                        progress=status["progress"],
                        result={**self.results, "distillation": status},
                    )
            await asyncio.sleep(0.2)
        result = await pending
        self.check_cancelled()
        return result

    async def execute(self):
        from .provider_quota.hooks import TASK

        token = TASK.set(self.task_id)
        try:
            return await self._execute()
        finally:
            TASK.reset(token)

    async def _execute(self):
        state, error = "completed", None
        try:
            owner, document, command, settings = self.configuration()
            self.check_cancelled()
            async with AsyncClient(
                transport=ASGITransport(self.app),
                base_url="http://managed-runtime",
                headers={"X-API-Key": self.spec["capability"]},
            ) as client:
                if command.kind in {"ingest", "chain"}:
                    self.results["ingest"] = await self.ingest(client, settings)
                    if self.results["ingest"]["status"] != "completed":
                        if command.kind == "chain":
                            raise RuntimeError(
                                "Ingestion contains failed documents; chain stopped before retrieval"
                            )
                        state = "completed_with_errors"
                if command.kind in {"retrieval", "chain"}:
                    self.check_cancelled()
                    self.results["retrieval"] = await self.retrieve(owner, command)
                    if self.results["retrieval"]["status"] == "completed_with_errors":
                        state = "completed_with_errors"
                if command.kind in {"distillation", "chain"}:
                    self.check_cancelled()
                    source = (
                        resolve_batch(
                            self.rag,
                            self.results["retrieval"]["job_id"],
                            root=retrieval_root(self.paths, owner["workspace"]),
                        )
                        if command.kind == "chain"
                        else BatchSnapshot.model_validate(
                            self.task["payload"]["retrieval_source"]
                        )
                        if command.source.type == "retrieval"
                        else None
                    )
                    self.results["distillation"] = await self.distill(
                        owner, document, settings, source
                    )
                    workflow_state = self.results["distillation"]["status"]
                    if workflow_state != "completed":
                        state = workflow_state
                    elif self.results["distillation"]["progress"]["rejected_sources"]:
                        state = "completed_with_errors"
                if command.kind == "query":
                    self.update(
                        phase="query",
                        progress={"unit": "queries", "total": 1, "processed": 0},
                    )
                    options = dict(self.task["payload"]["query"])
                    text = options.pop("query")
                    self.results["query"] = await self.rag.aquery_data(
                        text, param=QueryParam(**options, enable_rerank=False)
                    )
                    self.update(
                        progress={"unit": "queries", "total": 1, "processed": 1}
                    )
                if command.kind == "centers":
                    from lightrag.entity_centers import CenterSettings, from_rag

                    self.update(phase="centers", progress=None)
                    self.results["centers"] = await from_rag(
                        self.rag,
                        CenterSettings(
                            **(command.selection or CenterOptions()).model_dump()
                        ),
                    )
        except InterruptedError:
            state = "cancelled"
        except Exception as exc:
            from lightrag.utils import logger

            logger.exception("Managed task failed: %s", self.task_id)
            state, error = "failed", type(exc).__name__ + ": inspect private worker.log"
        # Never release the slot while a stage's background work still runs.
        if self.retrieval_manager is not None and self.retrieval_manager.tasks:
            self.retrieval_manager.cancel(self.retrieval_job)
            await asyncio.gather(
                *list(self.retrieval_manager.tasks), return_exceptions=True
            )
        if self.workflow_pending is not None:
            await asyncio.gather(self.workflow_pending, return_exceptions=True)
            if self.workflow.started:
                self.results["distillation"] = self.workflow.status()
        # Wait for every accepted upload before releasing the batch task slot.
        background = getattr(self.app.state, "background_tasks", set())
        if background:
            await asyncio.gather(*list(background), return_exceptions=True)
        directory = self.paths.under(
            self.paths.runtime_root,
            "tasks",
            self.registry.get(self.batch)["namespace"],
            self.task_id,
        )
        atomic_json(
            directory / "result.json",
            {
                "task_id": self.task_id,
                "data_batch_id": self.batch,
                "config_revision_id": self.task["config_revision_id"],
                "state": state,
                "results": self.results,
                "error": error,
            },
        )
        self.update(
            event="finished",
            state=state,
            phase=state,
            result=self.results,
            error=error,
            waiting_reason=None,
        )


class TaskRunner:
    def __init__(self, app, spec):
        self.app, self.spec = app, spec
        self.store = TaskStore(Registry(Path(spec["registry"])))
        self.active = None

    async def tick(self):
        if self.active is not None:
            if not self.active.done():
                return
            await self.active
            self.active = None
        task = self.store.claim(
            self.spec["data_batch_id"],
            self.spec["generation"],
            self.spec["startup_digest"],
            os.getpid(),
            process_identity(os.getpid()),
        )
        if task is not None:
            self.active = asyncio.create_task(
                TaskEngine(self.app, self.spec, task).execute()
            )

    async def drain(self):
        if self.active is not None:
            await self.active
