"""Capture rebuild evidence inside the exact owned and drained source generation.

Admission stays fenced until this source process is retired or restored. A
partial capture is retained and is never silently reused. See docs/design/BatchIndexRebuild.md.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from ..http_store import HttpStore
from ..legacy import legacy_busy
from ..paths import ControlPaths
from ..runtime_process import atomic_json, process_identity
from ..storage import Conflict, IntegrityError, Registry
from ..task_store import TaskStore
from ..writer_claim import sync_directory
from .audit import snapshot_state, verify_state
from .authority import is_rebuild
from .source import SourceIdentity, SourceLimits, write_source
from .store import RebuildStore


def operation_directory(paths, registry, batch, operation):
    owner = registry.get(batch)
    directory = paths.under(
        paths.runtime_root, "index-rebuilds", owner["namespace"], operation
    )
    if directory != directory.resolve():
        raise IntegrityError("Index operation directory was redirected")
    return directory


class CaptureRunner:
    def __init__(self, app, spec):
        self.app, self.spec = app, spec
        self.paths = ControlPaths(
            **{key: Path(value) for key, value in spec["control_paths"].items()}
        )
        self.registry = Registry(self.paths.registry)
        self.store = RebuildStore(self.registry)
        self.active = None

    def requested(self):
        with self.registry.transaction() as db:
            owner = self.registry.row(db, self.spec["data_batch_id"])
            operation = owner["pending"]
            if not operation or not is_rebuild(db, owner["id"], operation):
                return None
            record = self.store.read(db, owner["id"], operation)
            self.store.fenced(db, self.registry, record)
            source = SourceIdentity.model_validate(record["source"])
            lease = record["source_lease"]
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (owner["id"],)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            expected = {
                "data_batch_id": source.data_batch_id,
                "generation": source.generation,
                "lease_id": source.lease_id,
                "startup_digest": source.startup_digest,
                "pid": os.getpid(),
                "process_identity": process_identity(os.getpid()),
            }
            if (
                record["state"] != "source_capturing"
                or any(
                    getattr(source, key) != self.spec.get(key)
                    for key in (
                        "data_batch_id",
                        "generation",
                        "lease_id",
                        "startup_digest",
                    )
                )
                or lease["pid"] != os.getpid()
                or lease["process_identity"] != process_identity(os.getpid())
                or runtime.get("state") != "ready"
                or any(runtime.get(key) != value for key, value in expected.items())
            ):
                return None
            return record

    async def quiet(self, fence):
        if await legacy_busy(self.app, fence):
            return False
        # The RAG's actual release-drive accessor owns the strong task set.
        accessor = getattr(self.app.state.rag, "_admin_release_drives", None)
        drives = accessor() if accessor is not None else ()
        if any(not task.done() for task in drives):
            return False
        batch = self.spec["data_batch_id"]
        return not HttpStore(self.registry, self.paths).active(batch) and not TaskStore(
            self.registry
        ).list(batch, states={"admitted", "running"})

    async def capture(self, record):
        operation, batch = record["operation_id"], record["data_batch_id"]
        fence = self.app.state.legacy_fence
        try:
            from lightrag.kg.shared_storage import get_namespace_lock

            if fence.owner not in {None, operation}:
                raise Conflict("Another source freeze owns this workspace")
            fence.owner = operation
            # All accepted HTTP/task/background/admin drives must be idle. Holding
            # the pipeline lock then prevents a start/enqueue crossing the capture;
            # no manual-retry or destructive pipeline flags are borrowed here.
            async with fence.snapshot_lock:
                async with get_namespace_lock(
                    "pipeline_status", workspace=self.app.state.rag.workspace
                ):
                    if not await self.quiet(fence):
                        return
                    if self.store.get(batch, operation)["cancel_requested"]:
                        raise InterruptedError("Index rebuild cancelled before capture")
                    fence.snapshot_active = True
                    try:
                        identity = SourceIdentity.model_validate(record["source"])
                        limits = SourceLimits()
                        audit, rows = await snapshot_state(
                            self.app.state.rag, identity, limits=limits
                        )
                        directory = (
                            operation_directory(
                                self.paths, self.registry, batch, operation
                            )
                            / "source"
                        )
                        names = (
                            getattr(self.app.state.rag, "entity_merge", None)
                            is not None
                        )
                        await asyncio.to_thread(
                            write_source,
                            directory,
                            identity,
                            **rows,
                            names=names,
                            limits=limits,
                        )
                        await verify_state(self.app.state.rag, audit, limits=limits)
                        atomic_json(directory / "audit.json", audit.model_dump())
                        sync_directory(directory)
                        evidence = {
                            "manifest_sha256": hashlib.sha256(
                                (directory / "source.json").read_bytes()
                            ).hexdigest(),
                            "audit_sha256": hashlib.sha256(
                                (directory / "audit.json").read_bytes()
                            ).hexdigest(),
                        }
                        self.store.finish_capture(
                            batch,
                            operation,
                            identity,
                            evidence,
                            os.getpid(),
                            process_identity(os.getpid()),
                        )
                    finally:
                        fence.snapshot_active = False
        except Exception as exc:
            from lightrag.utils import logger

            logger.exception("Index source capture failed: %s", operation)
            current = self.store.get(batch, operation)
            if current["state"] == "source_capturing":
                self.store.transition(
                    batch,
                    operation,
                    current["version"],
                    event="source_capture_failed",
                    state="rollback_stopping",
                    error=type(exc).__name__ + ": inspect the retained worker log",
                    waiting_reason=None,
                )

    async def tick(self):
        if self.active is not None:
            if not self.active.done():
                return
            await self.active
            self.active = None
        record = self.requested()
        if record is not None:
            self.active = asyncio.create_task(self.capture(record))

    async def drain(self):
        if self.active is not None:
            await self.active
