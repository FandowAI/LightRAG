"""Persistent process reservations and append-only execution events in the registry."""

from __future__ import annotations

from contextlib import contextmanager
import json
import time
import uuid

from .storage import Conflict
from .runtime_process import process_scope

HELD = {"reserved", "starting", "ready", "stopping", "unknown", "built"}


class RuntimeStore:
    def __init__(self, registry):
        self.registry = registry

    @contextmanager
    def transaction(self):
        with self.registry.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runtimes (batch TEXT PRIMARY KEY, generation INTEGER NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT NOT NULL, generation INTEGER NOT NULL, record TEXT NOT NULL)"
            )
            yield db

    @staticmethod
    def _get(db, batch):
        row = db.execute(
            "SELECT record FROM runtimes WHERE batch=?", (batch,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _write(db, record, event, actor):
        record["updated_at"] = time.time()
        db.execute(
            "INSERT INTO runtimes VALUES (?,?,?,?) ON CONFLICT(batch) DO UPDATE SET generation=excluded.generation,state=excluded.state,record=excluded.record",
            (
                record["data_batch_id"],
                record["generation"],
                record["state"],
                json.dumps(record),
            ),
        )
        entry = {"event": event, "actor": actor, "time": record["updated_at"], **record}
        db.execute(
            "INSERT INTO runtime_events(batch,generation,record) VALUES (?,?,?)",
            (record["data_batch_id"], record["generation"], json.dumps(entry)),
        )

    def get(self, batch):
        with self.transaction() as db:
            return self._get(db, batch)

    @staticmethod
    def revision_allowed(
        db,
        owner,
        revision,
        application_id=None,
        rollback_id=None,
        rebuild_rollback_id=None,
    ):
        if rebuild_rollback_id is not None:
            if application_id is not None or rollback_id is not None:
                return False
            from .index_rebuild.authority import table_exists

            if not table_exists(db, "index_rebuilds"):
                return False
            row = db.execute(
                "SELECT record FROM index_rebuilds WHERE batch=? AND id=?",
                (owner["id"], rebuild_rollback_id),
            ).fetchone()
            recovery = json.loads(row[0]) if row else {}
            return bool(
                owner["pending"] == rebuild_rollback_id
                and owner["active"] == revision == recovery.get("previous_revision")
                and owner["desired"] == recovery.get("config_revision_id")
                and recovery.get("state") in {"rollback_starting", "rollback_verifying"}
            )
        if rollback_id is not None:
            if application_id is not None:
                return False
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='applications'"
            ).fetchone():
                return False
            row = db.execute(
                "SELECT record FROM applications WHERE batch=? AND id=?",
                (owner["id"], rollback_id),
            ).fetchone()
            recovery = json.loads(row[0]) if row else {}
            return bool(
                owner["pending"] == rollback_id
                and owner["active"] == revision == recovery.get("previous_revision")
                and owner["desired"] == recovery.get("config_revision_id")
                and recovery.get("state") in {"rollback_starting", "rollback_verifying"}
            )
        if application_id is None:
            return owner["active"] == revision and owner["pending"] is None
        operation = db.execute(
            "SELECT revision,status FROM operations WHERE batch=? AND id=?",
            (owner["id"], application_id),
        ).fetchone()
        return bool(
            operation
            and operation["revision"] == revision
            and operation["status"] == "pending_restart"
            and owner["desired"] == revision
            and owner["pending"] == application_id
        )

    def reserve(
        self,
        batch,
        revision,
        startup_digest,
        rag_digest,
        capacity,
        actor,
        *,
        application_id=None,
        rollback_id=None,
        rebuild_rollback_id=None,
        purpose="rag",
    ):
        with self.transaction() as db:
            if purpose not in {"rag", "index_build"}:
                raise ValueError("Unsupported executable runtime purpose")
            owner = self.registry.row(db, batch)
            if not self.revision_allowed(
                db, owner, revision, application_id, rollback_id, rebuild_rollback_id
            ):
                raise Conflict(
                    "Runtime requires the active, non-pending configuration revision"
                )
            previous = self._get(db, batch)
            if previous and previous["state"] in HELD:
                raise Conflict("This batch already holds a runtime reservation")
            if purpose == "index_build":
                from .index_rebuild.store import RebuildStore

                if not application_id or rollback_id or rebuild_rollback_id:
                    raise Conflict("Builder requires its exclusive rebuild operation")
                operation = RebuildStore.read(db, batch, application_id)
                RebuildStore.fenced(db, self.registry, operation)
                if (
                    operation["state"] != "builder_starting"
                    or operation["cancel_requested"]
                    or not operation.get("source_evidence")
                    or not previous
                    or previous["state"] not in {"stopped", "failed"}
                    or previous.get("purpose", "rag") != "rag"
                    or any(
                        previous.get(key) != operation["source_lease"].get(key)
                        for key in (
                            "generation",
                            "lease_id",
                            "pid",
                            "process_identity",
                            "startup_digest",
                        )
                    )
                ):
                    raise Conflict(
                        "Builder needs its captured and confirmed-retired source; attempts cannot be replayed"
                    )
            records = [
                json.loads(row[0]) for row in db.execute("SELECT record FROM runtimes")
            ]
            if sum(record["state"] in HELD for record in records) >= capacity:
                raise Conflict("Managed RAG process capacity is occupied")
            record = {
                "data_batch_id": batch,
                "config_revision_id": revision,
                "generation": previous["generation"] + 1 if previous else 1,
                "state": "reserved",
                "lease_id": "lease-" + uuid.uuid4().hex,
                "startup_digest": startup_digest,
                "rag_digest": rag_digest,
                "workspace": owner["workspace"],
                "pid": None,
                "process_identity": None,
                "process_scope": process_scope(),
                "application_id": application_id,
                "rollback_id": rollback_id,
                "rebuild_rollback_id": rebuild_rollback_id,
                "purpose": purpose,
            }
            self._write(db, record, "reserved", actor)
            db.execute(
                "UPDATE batches SET lifecycle='provisioning' WHERE id=?", (batch,)
            )
            return record

    def update(self, batch, generation, states, actor, event, **values):
        with self.transaction() as db:
            record = self._get(db, batch)
            if (
                not record
                or record["generation"] != generation
                or record["state"] not in states
            ):
                raise Conflict("Runtime generation or state changed")
            if values.get("state") == "ready":
                if record.get("purpose", "rag") != "rag":
                    raise Conflict("A builder cannot establish RAG readiness")
                owner = self.registry.row(db, batch)
                if not self.revision_allowed(
                    db,
                    owner,
                    record["config_revision_id"],
                    record.get("application_id"),
                    record.get("rollback_id"),
                    record.get("rebuild_rollback_id"),
                ):
                    raise Conflict("Configuration changed before runtime readiness")
            record.update(values)
            self._write(db, record, event, actor)
            if record["state"] in {"ready", "failed", "stopped"}:
                lifecycle = {
                    "ready": "ready",
                    "failed": "failed",
                    "stopped": "registered",
                }[record["state"]]
                db.execute(
                    "UPDATE batches SET lifecycle=?,index_state='unknown' WHERE id=?",
                    (lifecycle, batch),
                )
            return record

    def attach(self, batch, generation, startup_digest, pid, identity):
        with self.transaction() as db:
            record = self._get(db, batch)
            if (
                not record
                or record["generation"] != generation
                or record["startup_digest"] != startup_digest
                or record["state"] not in {"reserved", "starting"}
                or record.get("process_scope") != process_scope()
                or (
                    record["pid"] is not None
                    and (record["pid"], record["process_identity"]) != (pid, identity)
                )
            ):
                raise Conflict("Child process does not own this runtime reservation")
            if record["pid"] is None:
                record.update(pid=pid, process_identity=identity, state="starting")
                self._write(db, record, "process_attached", "runtime-worker")
            return record

    def events(self, batch):
        with self.transaction() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM runtime_events WHERE batch=? ORDER BY sequence",
                    (batch,),
                )
            ]
