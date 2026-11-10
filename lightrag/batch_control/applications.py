"""Apply pending RAG configuration only after drain, owned restart and readiness proof.

No index is deleted or rebuilt. See docs/design/BatchConfigurationUI.md.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import json
import time

from .resources import digest
from .runtime import RuntimeSupervisor
from .runtime_config import compile_startup
from .runtime_process import ProcessBackend
from .storage import Conflict, IntegrityError
from .task_store import TaskStore
from .http_store import HttpStore
from .application_rollback import (
    STATES as ROLLBACK_STATES,
    advance as advance_rollback,
    retain as retain_runtime,
)

ACTIVE = {"queued", "draining", "stopping", "starting", "verifying"} | ROLLBACK_STATES


class ApplicationStore:
    def __init__(self, registry):
        self.registry = registry

    @contextmanager
    def transaction(self):
        with self.registry.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, batch TEXT NOT NULL, record TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS application_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT NOT NULL, operation TEXT NOT NULL, record TEXT NOT NULL)"
            )
            yield db

    @staticmethod
    def read(db, batch, operation):
        row = db.execute(
            "SELECT record FROM applications WHERE batch=? AND id=?", (batch, operation)
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def write(db, record, event):
        record["updated_at"] = time.time()
        db.execute(
            "INSERT INTO applications VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
            (record["operation_id"], record["data_batch_id"], json.dumps(record)),
        )
        db.execute(
            "INSERT INTO application_events(batch,operation,record) VALUES (?,?,?)",
            (
                record["data_batch_id"],
                record["operation_id"],
                json.dumps({"event": event, **record}),
            ),
        )
        return record

    def get(self, batch, operation):
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            if record is not None:
                return record
            row = db.execute(
                "SELECT revision,status FROM operations WHERE batch=? AND id=?",
                (batch, operation),
            ).fetchone()
            if row is None:
                raise KeyError(operation)
            return {
                "operation_id": operation,
                "data_batch_id": batch,
                "config_revision_id": row["revision"],
                "state": "awaiting_action"
                if row["status"] == "pending_restart"
                else row["status"],
                "terminal": row["status"] != "pending_restart",
            }

    def request(self, actor, batch, operation, expected, request_id):
        fingerprint = digest({"operation": operation, "revision": expected})
        with self.transaction() as db:
            from .index_rebuild.authority import is_rebuild

            if is_rebuild(db, batch, operation):
                raise Conflict("Use the index rebuild controller for this operation")
            previous = self.registry.prior(
                db, actor, "apply", batch, request_id, fingerprint
            )
            if previous:
                return self.read(db, batch, operation)
            owner = self.registry.row(db, batch)
            if owner["pending"] != operation or owner["desired"] != expected:
                raise Conflict("Pending configuration changed")
            record = self.read(db, batch, operation)
            if record is None or record["state"] not in ACTIVE:
                record = {
                    "operation_id": operation,
                    "data_batch_id": batch,
                    "config_revision_id": expected,
                    "previous_revision": owner["active"],
                    "actor": actor,
                    "state": "queued",
                    "attempt": (record or {}).get("attempt", 0) + 1,
                    "created_at": (record or {}).get("created_at", time.time()),
                    "terminal": False,
                    "error": None,
                    "waiting_reason": None,
                }
                db.execute(
                    "UPDATE operations SET status='pending_restart' WHERE batch=? AND id=?",
                    (batch, operation),
                )
                self.write(db, record, "requested")
            self.registry.remember(
                db,
                actor,
                "apply",
                batch,
                request_id,
                fingerprint,
                {"operation": operation},
            )
            return record

    def update(self, batch, operation, *, event, **values):
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            if record is None:
                raise KeyError(operation)
            record.update(values)
            record["terminal"] = record["state"] not in ACTIVE
            if record["state"] in {"failed", "rollback_failed"}:
                db.execute(
                    "UPDATE operations SET status='failed' WHERE batch=? AND id=?",
                    (batch, operation),
                )
            return self.write(db, record, event)

    def pending(self):
        with self.transaction() as db:
            return [
                record
                for row in db.execute("SELECT record FROM applications ORDER BY rowid")
                if (record := json.loads(row[0]))["state"] in ACTIVE
            ]

    def latest(self, batch):
        with self.transaction() as db:
            row = db.execute(
                "SELECT record FROM applications WHERE batch=? ORDER BY rowid DESC LIMIT 1",
                (batch,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def commit(self, batch, operation, lease):
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            owner = self.registry.row(db, batch)
            if (
                record is None
                or record["state"] != "verifying"
                or owner["pending"] != operation
                or owner["desired"] != record["config_revision_id"]
                or owner["active"] != record["previous_revision"]
            ):
                raise Conflict("Application identity changed before commit")
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            if (
                runtime.get("state") != "ready"
                or runtime.get("application_id") != operation
                or runtime.get("config_revision_id") != record["config_revision_id"]
                or runtime.get("lease_id") != lease.lease_id
                or runtime.get("generation") != lease.generation
                or runtime.get("startup_digest") != lease.startup_digest
            ):
                raise IntegrityError("New configuration has no matching ready runtime")
            db.execute(
                "UPDATE batches SET active=desired,desired=NULL,pending=NULL,lifecycle='ready' WHERE id=?",
                (batch,),
            )
            db.execute(
                "UPDATE operations SET status='applied' WHERE batch=? AND id=?",
                (batch, operation),
            )
            # Keep the runtime's application ID as historical ownership evidence.
            task_store = TaskStore(self.registry)
            for row in db.execute(
                "SELECT record FROM tasks WHERE batch=?", (batch,)
            ).fetchall():
                task = json.loads(row[0])
                if (
                    task["state"] == "queued"
                    and task["components"]["rag"] != runtime["rag_digest"]
                ):
                    task.update(
                        state="interrupted",
                        phase="interrupted",
                        finished_at=time.time(),
                        error="RAG configuration changed before execution; submit a new task explicitly",
                    )
                    task_store._write(db, task, "configuration_retired")
            record.update(
                state="applied",
                terminal=True,
                lease=lease.model_dump(),
                error=None,
                waiting_reason=None,
                finished_at=time.time(),
            )
            return self.write(db, record, "applied")

    def discard(self, actor, batch, operation):
        with self.transaction() as db:
            from .index_rebuild.authority import is_rebuild

            if is_rebuild(db, batch, operation):
                raise Conflict("Cancel index rebuilding through its own controller")
            owner = self.registry.row(db, batch)
            if owner["pending"] != operation:
                raise Conflict("Pending configuration changed")
            record = self.read(db, batch, operation) or {
                "operation_id": operation,
                "data_batch_id": batch,
                "config_revision_id": owner["desired"],
                "previous_revision": owner["active"],
                "created_at": time.time(),
            }
            record.update(
                actor=actor, state="discarded", terminal=True, finished_at=time.time()
            )
            db.execute(
                "UPDATE batches SET desired=NULL,pending=NULL WHERE id=?", (batch,)
            )
            db.execute(
                "UPDATE operations SET status='failed' WHERE batch=? AND id=?",
                (batch, operation),
            )
            return self.write(db, record, "discarded")

    def events(self, batch, operation):
        with self.transaction() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM application_events WHERE batch=? AND operation=? ORDER BY sequence",
                    (batch, operation),
                )
            ]


class ApplicationService:
    def __init__(self, control, policy, *, actor, backend=None):
        self.control, self.policy, self.actor = control, policy, actor
        self.store = ApplicationStore(control.registry)
        self.runtime = RuntimeSupervisor(control, policy, actor=actor, backend=backend)

    @contextmanager
    def locked(self, batch):
        self.control.allow(self.actor, "read", batch)
        self.control.registry.get(batch)
        path = self.control.paths.under(
            self.control.paths.runtime_root,
            "batch-control",
            "locks",
            batch + ".apply.lock",
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path != path.resolve():
            raise IntegrityError("Application lock was redirected")
        with path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict("Application step is in progress") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def authorize(self, batch):
        for action in ("edit", "apply_config", "run_runtime", "stop_runtime"):
            self.control.allow(self.actor, action, batch)

    def request(self, batch, operation, expected, request_id):
        self.authorize(batch)
        return self.store.request(self.actor, batch, operation, expected, request_id)

    def preflight(self, batch, record):
        owner, _, desired, _, manifest = self.control.read(
            self.actor, batch, record["config_revision_id"]
        )
        _, _, active, _, _ = self.control.read(
            self.actor, batch, record["previous_revision"]
        )
        if self.control.catalog.snapshot(desired) != manifest["execution"]:
            raise IntegrityError("Execution references changed after publication")
        if desired.rag.embedding_profile != active.rag.embedding_profile and (
            self.runtime.store.get(batch) is not None
            or owner["index_state"] != "unprovisioned"
        ):
            raise Conflict(
                "Embedding changes require a new unprovisioned batch; no existing index is rebuilt automatically"
            )
        compile_startup(
            self.control,
            self.actor,
            owner,
            record["config_revision_id"],
            desired,
            manifest,
            self.policy,
        )

    def step(self, batch, operation):
        with self.locked(batch):
            record = self.store.get(batch, operation)
            if record["state"] not in ACTIVE:
                return record
            if record["state"] in ROLLBACK_STATES:
                return advance_rollback(self, batch, operation)
            try:
                self.authorize(batch)
                owner = self.control.registry.get(batch)
                if (
                    owner["pending"] != operation
                    or owner["desired"] != record["config_revision_id"]
                ):
                    raise IntegrityError(
                        "Application no longer owns the pending revision"
                    )
                if record["state"] in {"queued", "draining"}:
                    if record["state"] == "queued":
                        self.preflight(batch, record)
                    activity = False
                    if self.runtime.store.get(batch):
                        with self.runtime.locked(batch):
                            lease = self.runtime._inspect(batch)
                            current_runtime = self.runtime.store.get(batch)
                            proof = (
                                self.runtime._proof(current_runtime) if lease else None
                            )
                            activity = bool(
                                proof and proof.get("business_activity", {}).get("busy")
                            )
                    if (
                        activity
                        or HttpStore(self.control.registry, self.control.paths).active(
                            batch
                        )
                        or TaskStore(self.control.registry).list(
                            batch, states={"admitted", "running"}
                        )
                    ):
                        return self.store.update(
                            batch,
                            operation,
                            event="draining",
                            state="draining",
                            waiting_reason="Waiting for the current batch task to finish",
                        )
                    if record["state"] == "draining":
                        self.preflight(batch, record)
                    self.store.update(
                        batch,
                        operation,
                        event="drained",
                        state="stopping",
                        waiting_reason=None,
                    )
                record = self.store.get(batch, operation)
                if record["state"] == "stopping":
                    self.runtime.stop(batch)
                    self.store.update(
                        batch, operation, event="old_runtime_stopped", state="starting"
                    )
                record = self.store.get(batch, operation)
                if record["state"] == "starting":
                    try:
                        self.runtime.start(
                            batch,
                            record["config_revision_id"],
                            application_id=operation,
                        )
                    except Conflict as exc:
                        return self.store.update(
                            batch, operation, event="waiting", waiting_reason=str(exc)
                        )
                    self.store.update(
                        batch,
                        operation,
                        event="new_runtime_ready",
                        state="verifying",
                        waiting_reason=None,
                    )
                with self.runtime.locked(batch):
                    self.authorize(batch)
                    lease = self.runtime._inspect(batch)
                    if lease is None:
                        raise IntegrityError(
                            "New runtime readiness could not be confirmed"
                        )
                    return self.store.commit(batch, operation, lease)
            except Exception as exc:
                from lightrag.utils import logger

                logger.exception("Configuration application failed: %s", operation)
                current = self.store.get(batch, operation)
                failure = (
                    str(exc)
                    if isinstance(exc, Conflict)
                    else type(exc).__name__ + ": inspect server logs"
                )
                if current["state"] in {"queued", "draining"}:
                    retained = retain_runtime(self, batch, operation, failure)
                    if retained is not None:
                        return retained
                if current["state"] in {"stopping", "starting", "verifying"}:
                    self.store.update(
                        batch,
                        operation,
                        event="rollback_requested",
                        state="rollback_stopping",
                        error=str(exc)
                        if isinstance(exc, Conflict)
                        else type(exc).__name__ + ": inspect server logs",
                        waiting_reason=None,
                    )
                    return advance_rollback(self, batch, operation)
                return self.store.update(
                    batch,
                    operation,
                    event="failed",
                    state="failed",
                    error=str(exc)
                    if isinstance(exc, Conflict)
                    else type(exc).__name__ + ": inspect server logs",
                    waiting_reason=None,
                )

    def discard(self, batch, operation):
        self.authorize(batch)
        with self.locked(batch):
            from .index_rebuild.authority import is_rebuild

            with self.control.registry.transaction() as db:
                if is_rebuild(db, batch, operation):
                    raise Conflict("Cancel index rebuilding through its own controller")
            owner = self.control.registry.get(batch)
            if owner["pending"] != operation:
                raise Conflict("Pending configuration changed")
            runtime = self.runtime.store.get(batch)
            active_rag = self.control.registry.revision(batch, owner["active"])[
                "components"
            ]["rag"]
            if runtime and runtime["rag_digest"] != active_rag:
                self.runtime.stop(batch)
            return self.store.discard(self.actor, batch, operation)


class ApplicationCoordinator:
    def __init__(self, control, policy, *, backend=None):
        self.control, self.policy = control, policy
        self.backend = backend or ProcessBackend()
        self.store = ApplicationStore(control.registry)
        self.stop_event = asyncio.Event()

    def tick(self):
        for record in self.store.pending():
            service = ApplicationService(
                self.control, self.policy, actor=record["actor"], backend=self.backend
            )
            try:
                service.step(record["data_batch_id"], record["operation_id"])
            except Conflict:
                continue

    async def run(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                from lightrag.utils import logger

                logger.exception(
                    "Application coordinator tick failed; requests remain durable"
                )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
            except TimeoutError:
                pass
