"""Advance one durable rebuild phase without replaying uncertain work.

The service lock serializes steps; runtime locks protect actual process leases.
Unconfirmed retirement retains the fence. See docs/design/BatchIndexRebuild.md.
"""

from ..http_store import HttpStore
from ..runtime_config import compile_startup
from ..runtime_store import HELD
from ..storage import Conflict, IntegrityError
from ..task_store import TaskStore
from .build_runtime import BuilderRuntime
from .store import TERMINAL
from .verification import context


class RebuildController:
    def __init__(self, service):
        self.service = service
        self.runtime, self.store, self.control = (
            service.runtime,
            service.store,
            service.control,
        )
        self.builder = BuilderRuntime(self.runtime)

    def change(self, record, state, event, **values):
        return self.store.transition(
            record["data_batch_id"],
            record["operation_id"],
            record["version"],
            state=state,
            event=event,
            **values,
        )

    def waiting(self, record, reason, **values):
        if record.get("waiting_reason") == reason and all(
            record.get(key) == value for key, value in values.items()
        ):
            return record
        return self.change(
            record, record["state"], "waiting", waiting_reason=reason, **values
        )

    def capacity_occupied(self):
        with self.runtime.store.transaction() as db:
            placeholders = ",".join("?" for _ in HELD)
            return (
                db.execute(
                    f"SELECT COUNT(*) FROM runtimes WHERE state IN ({placeholders})",
                    tuple(HELD),
                ).fetchone()[0]
                >= self.service.policy.max_processes
            )

    def active_work(self, batch, proof=None):
        return bool(
            proof
            and proof.get("business_activity", {}).get("busy")
            or HttpStore(self.control.registry, self.control.paths).active(batch)
            or TaskStore(self.control.registry).list(
                batch, states={"admitted", "running"}
            )
        )

    def source(self, record, *, fresh=False):
        batch = record["data_batch_id"]
        with self.runtime.locked(batch):
            row = self.runtime.store.get(batch)
            if not row or any(
                row.get(key) != record["source_lease"][key]
                for key in (
                    "generation",
                    "lease_id",
                    "pid",
                    "process_identity",
                    "startup_digest",
                )
            ):
                raise IntegrityError(
                    "The exact captured source generation was replaced"
                )
            ownership = self.runtime.backend.ownership(row)
            if ownership in {"gone", "reused"}:
                raise RuntimeError(
                    "The original source exited before its capture completed"
                )
            if ownership != "owned":
                return None
            self.runtime._spec(row)
            if fresh:
                return self.runtime._proof(row)
            return row

    def retire(self, record):
        """Signal once; later steps only observe that exact process until exit."""
        batch, operation = record["data_batch_id"], record["operation_id"]
        with self.runtime.locked(batch):
            row = self.runtime.store.get(batch)
            if row is None:
                raise IntegrityError("The rebuild's process reservation disappeared")
            source = all(
                row.get(key) == record["source_lease"][key]
                for key in (
                    "generation",
                    "lease_id",
                    "pid",
                    "process_identity",
                    "startup_digest",
                )
            )
            candidate = (
                row.get("application_id") == operation
                and row["config_revision_id"] == record["config_revision_id"]
            )
            restored = (
                row.get("rebuild_rollback_id") == operation
                and row["config_revision_id"] == record["previous_revision"]
            )
            if not (source or candidate or restored):
                raise IntegrityError(
                    "An unrelated runtime replaced the rebuild reservation"
                )
            if row.get("pid") is None:
                return (
                    row["state"] == "failed" and row.get("launch_not_started") is True
                )
            self.runtime._spec(row)
            ownership = self.runtime.backend.ownership(row)
            if ownership in {"gone", "reused"}:
                if row["state"] in HELD:
                    self.runtime.store.update(
                        batch,
                        row["generation"],
                        HELD,
                        self.service.actor,
                        "rebuild_exit_confirmed",
                        state="stopped",
                    )
                return True
            if ownership != "owned" or row["state"] == "stopping":
                return False
            # Rebuild cancellation does not cancel already accepted source work.
            if source:
                proof = self.runtime._proof(row)
                if proof is None or self.active_work(batch, proof):
                    return False
            row = self.runtime.store.update(
                batch,
                row["generation"],
                {row["state"]},
                self.service.actor,
                "rebuild_stop_intent",
                state="stopping",
            )
            try:
                self.runtime.backend.terminate(row, self.service.policy.stop_timeout)
            except TimeoutError:
                return False
            if self.runtime.backend.ownership(row) not in {"gone", "reused"}:
                return False
            self.runtime.store.update(
                batch,
                row["generation"],
                {"stopping"},
                self.service.actor,
                "rebuild_exit_confirmed",
                state="stopped",
            )
            return True

    def preflight(self, record):
        batch = record["data_batch_id"]
        owner, _, document, _, manifest = self.control.read(
            self.service.actor, batch, record["config_revision_id"]
        )
        if self.control.catalog.snapshot(document) != manifest["execution"]:
            raise IntegrityError(
                "Execution references changed after rebuild publication"
            )
        compile_startup(
            self.control,
            self.service.actor,
            owner,
            record["config_revision_id"],
            document,
            manifest,
            self.service.policy,
            index_purpose="index_preflight",
        )

    def forward(self, record):
        batch, operation, state = (
            record["data_batch_id"],
            record["operation_id"],
            record["state"],
        )
        if state == "draining":
            self.preflight(record)
            proof = self.source(record, fresh=True)
            if proof is None or self.active_work(batch, proof):
                return self.waiting(
                    record, "Waiting for accepted source work to finish"
                )
            return self.change(
                record,
                "source_capturing",
                "source_drain_complete",
                waiting_reason="Capturing complete canonical source evidence",
            )
        if state == "source_capturing":
            self.source(record)
            return self.waiting(
                record, "Waiting for the owning source worker to finish capture"
            )
        if state == "source_captured":
            context(self.control.registry, self.control.paths, batch, operation)
            return self.change(
                record,
                "stopping_source",
                "source_capture_verified",
                waiting_reason=None,
            )
        if state == "stopping_source":
            if not self.retire(record):
                return self.waiting(record, "Waiting for confirmed source process exit")
            return self.change(
                record, "builder_starting", "source_exit_confirmed", waiting_reason=None
            )
        if state == "builder_starting":
            row = self.runtime.store.get(batch)
            if (
                row
                and row.get("purpose") == "index_build"
                and row.get("application_id") == operation
            ):
                row, _ = self.builder.inspect(batch, operation)
                if self.runtime.backend.ownership(row) in {"gone", "reused"}:
                    # Require a complete receipt; an exited partial build is not replayed.
                    self.builder.receipt(batch, operation)
                elif row.get("pid") is None:
                    return self.waiting(
                        record, "Waiting for the existing builder's process identity"
                    )
            else:
                if self.capacity_occupied():
                    return self.waiting(record, "Waiting for shared process capacity")
                try:
                    row = self.builder.start(batch, operation)
                except Conflict as exc:
                    if str(exc) == "Managed RAG process capacity is occupied":
                        return self.waiting(
                            record, "Waiting for shared process capacity"
                        )
                    raise
            return self.change(
                record,
                "building",
                "builder_reserved",
                builder_lease=row,
                waiting_reason=None,
            )
        if state == "building":
            with self.runtime.locked(batch):
                row, proof = self.builder._inspect(batch, operation)
                if not row:
                    raise IntegrityError("The builder reservation disappeared")
                ownership = self.runtime.backend.ownership(row)
                if (
                    ownership in {"gone", "reused"}
                    or row["state"] == "built"
                    and ownership == "owned"
                ):
                    receipt, checksum = self.builder.receipt(batch, operation)
                    return self.store.verify_build(
                        batch,
                        operation,
                        receipt,
                        checksum,
                        disposition="gone"
                        if ownership in {"gone", "reused"}
                        else "owned",
                    )
                progress = proof.get("progress") if proof else record.get("progress")
                return self.waiting(
                    record,
                    "Building and verifying shadow vectors"
                    if proof
                    else "Waiting for a verified builder observation",
                    progress=progress,
                )
        if state == "stopping_builder":
            if not self.retire(record):
                return self.waiting(
                    record, "Waiting for confirmed builder process exit"
                )
            return self.change(
                record,
                "candidate_starting",
                "builder_exit_confirmed",
                waiting_reason=None,
            )
        if state == "candidate_starting":
            row = self.runtime.store.get(batch)
            if (not row or row["state"] not in HELD) and self.capacity_occupied():
                return self.waiting(record, "Waiting for shared process capacity")
            try:
                lease = self.runtime.start(
                    batch, record["config_revision_id"], application_id=operation
                )
            except Conflict as exc:
                if str(exc) == "Managed RAG process capacity is occupied":
                    return self.waiting(record, "Waiting for shared process capacity")
                raise
            return self.change(
                record,
                "candidate_verifying",
                "full_candidate_ready",
                candidate_lease=lease.model_dump(),
                waiting_reason=None,
            )
        if state == "candidate_verifying":
            with self.runtime.locked(batch):
                lease = self.runtime._inspect(batch)
                if lease is None:
                    raise IntegrityError(
                        "Full candidate readiness could not be verified"
                    )
                proof = self.runtime._proof(self.runtime.store.get(batch))
                if self.active_work(batch, proof):
                    return self.waiting(
                        record, "Waiting for candidate quiescence before activation"
                    )
                self.service.authorize(batch)
                return self.store.activate(batch, operation, lease, proof)
        raise IntegrityError("Unsupported rebuild phase")

    def rollback(self, record):
        batch, operation, state = (
            record["data_batch_id"],
            record["operation_id"],
            record["state"],
        )
        if state == "rollback_stopping":
            if not self.retire(record):
                return self.waiting(
                    record,
                    "Waiting for confirmed shutdown before restoring the previous index",
                )
            return self.change(
                record,
                "rollback_starting",
                "rollback_writer_retired",
                waiting_reason=None,
            )
        if state == "rollback_starting":
            row = self.runtime.store.get(batch)
            if (not row or row["state"] not in HELD) and self.capacity_occupied():
                return self.waiting(
                    record,
                    "Waiting for shared process capacity to restore the previous index",
                )
            try:
                lease = self.runtime.start(
                    batch, record["previous_revision"], rebuild_rollback_id=operation
                )
            except Conflict as exc:
                if str(exc) == "Managed RAG process capacity is occupied":
                    return self.waiting(
                        record,
                        "Waiting for shared process capacity to restore the previous index",
                    )
                raise
            return self.change(
                record,
                "rollback_verifying",
                "previous_runtime_ready",
                restored_lease=lease.model_dump(),
                waiting_reason=None,
            )
        if state == "rollback_verifying":
            with self.runtime.locked(batch):
                lease = self.runtime._inspect(batch)
                if lease is None:
                    raise IntegrityError(
                        "Previous runtime readiness could not be verified"
                    )
                self.service.authorize(batch)
                return self.store.restore(
                    batch,
                    operation,
                    lease,
                    self.runtime._proof(self.runtime.store.get(batch)),
                )
        raise IntegrityError("Unsupported index restoration phase")

    def step(self, batch, operation):
        with self.service.locked(batch):
            record = self.store.get(batch, operation)
            if record["state"] in TERMINAL | {"rollback_failed"}:
                return record
            try:
                self.service.authorize(batch)
                with self.store.transaction() as db:
                    self.store.fenced(db, self.control.registry, record)
                if record["cancel_requested"] and not record["state"].startswith(
                    "rollback_"
                ):
                    return self.change(
                        record,
                        "rollback_stopping",
                        "cancellation_observed",
                        error="Index rebuild cancelled",
                        waiting_reason=None,
                    )
                if record["state"].startswith("rollback_"):
                    return self.rollback(record)
                return self.forward(record)
            except Exception as exc:
                from lightrag.utils import logger

                current = self.store.get(batch, operation)
                if current["version"] != record["version"]:
                    return current
                logger.exception("Index rebuild step failed: %s", operation)
                recovery = record["state"].startswith("rollback_")
                return self.change(
                    current,
                    "rollback_failed" if recovery else "rollback_stopping",
                    "restoration_failed" if recovery else "rebuild_failed",
                    **{
                        "rollback_error" if recovery else "error": type(exc).__name__
                        + ": inspect retained worker/server logs"
                    },
                    waiting_reason=None,
                )
