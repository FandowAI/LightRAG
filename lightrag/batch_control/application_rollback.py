"""Restore the prior revision only after retiring its failed replacement writer."""

import json
import time

from .storage import Conflict, IntegrityError

STATES = {"rollback_stopping", "rollback_starting", "rollback_verifying"}


def commit(store, batch, operation, lease):
    with store.transaction() as db:
        record = store.read(db, batch, operation)
        owner = store.registry.row(db, batch)
        runtime_row = db.execute(
            "SELECT record FROM runtimes WHERE batch=?", (batch,)
        ).fetchone()
        runtime = json.loads(runtime_row[0]) if runtime_row else {}
        prior = db.execute(
            "SELECT components FROM revisions WHERE batch=? AND id=?",
            (batch, owner["active"]),
        ).fetchone()
        prior_rag = json.loads(prior[0])["rag"] if prior else None
        retained = bool(record and record.get("rollback_retained"))
        if (
            record is None
            or record["state"] != "rollback_verifying"
            or owner["pending"] != operation
            or owner["active"] != record["previous_revision"]
            or owner["desired"] != record["config_revision_id"]
            or runtime.get("rag_digest") != prior_rag
            or (
                not retained
                and (
                    runtime.get("config_revision_id") != record["previous_revision"]
                    or runtime.get("rollback_id") != operation
                )
            )
            or runtime.get("state") != "ready"
            or any(
                runtime.get(key) != getattr(lease, key)
                for key in (
                    "lease_id",
                    "generation",
                    "startup_digest",
                    "pid",
                    "process_identity",
                )
            )
        ):
            raise IntegrityError(
                "Previous configuration has no matching verified runtime"
            )
        db.execute(
            "UPDATE batches SET desired=NULL,pending=NULL,lifecycle='ready' WHERE id=?",
            (batch,),
        )
        db.execute(
            "UPDATE operations SET status='failed' WHERE batch=? AND id=?",
            (batch, operation),
        )
        record.update(
            state="rolled_back",
            terminal=True,
            rollback_lease=lease.model_dump(),
            waiting_reason=None,
            finished_at=time.time(),
        )
        return store.write(db, record, "previous_runtime_restored")


def retain(service, batch, operation, error):
    """A failed preflight can retain the untouched, verified original writer."""
    runtime = service.runtime
    try:
        with runtime.locked(batch):
            service.authorize(batch)
            lease = runtime._inspect(batch)
            if lease is None:
                return None
            service.store.update(
                batch,
                operation,
                event="original_runtime_retained",
                state="rollback_verifying",
                rollback_retained=True,
                error=error,
            )
            return commit(service.store, batch, operation, lease)
    except Exception:
        from lightrag.utils import logger

        logger.exception("Could not retain the previous runtime after failed preflight")
        return None


def advance(service, batch, operation):
    """Called under the application lock; admission stays closed until commit."""
    store, runtime = service.store, service.runtime
    try:
        service.authorize(batch)
        record = store.get(batch, operation)
        owner = service.control.registry.get(batch)
        if (
            owner["pending"] != operation
            or owner["active"] != record["previous_revision"]
            or owner["desired"] != record["config_revision_id"]
        ):
            raise IntegrityError("Rollback no longer owns the pending configuration")
        if record["state"] == "rollback_stopping":
            try:
                runtime.stop(batch)
            except (Conflict, TimeoutError):
                return store.update(
                    batch,
                    operation,
                    event="rollback_waiting_for_stop",
                    waiting_reason="Waiting for confirmed shutdown before restoring the previous configuration",
                )
            record = store.update(
                batch,
                operation,
                event="rollback_writer_retired",
                state="rollback_starting",
                waiting_reason=None,
            )
        if record["state"] == "rollback_starting":
            try:
                runtime.start(batch, record["previous_revision"], rollback_id=operation)
            except Conflict:
                return store.update(
                    batch,
                    operation,
                    event="rollback_waiting_for_runtime",
                    waiting_reason="Waiting for a verified runtime slot to restore the previous configuration",
                )
            record = store.update(
                batch,
                operation,
                event="rollback_runtime_ready",
                state="rollback_verifying",
                waiting_reason=None,
            )
        if record["state"] != "rollback_verifying":
            raise IntegrityError("Unsupported rollback phase")
        with runtime.locked(batch):
            service.authorize(batch)
            lease = runtime._inspect(batch)
            if lease is None:
                raise IntegrityError("Previous runtime readiness could not be verified")
            return commit(store, batch, operation, lease)
    except Exception as exc:
        from lightrag.utils import logger

        logger.exception("Previous configuration recovery failed: %s", operation)
        return store.update(
            batch,
            operation,
            event="rollback_failed",
            state="rollback_failed",
            rollback_error=type(exc).__name__ + ": inspect server logs",
            waiting_reason=None,
        )
