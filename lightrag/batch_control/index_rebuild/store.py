"""Fence admission and publish config/vector identity in one registry transaction.

The coordinator must verify actual source/process/artifact/readiness evidence
before supplying it here. No status transition alone proves a writer stopped.
See docs/design/BatchIndexRebuild.md for runtime and rollback requirements.
"""

from contextlib import contextmanager
import json
import time
from typing import Annotated

from pydantic import Field, TypeAdapter

from ..contracts import Contract, RuntimeLease
from ..resources import digest
from ..storage import Conflict, IntegrityError
from ..task_store import TaskStore
from .authority import VectorBinding, head, read_binding
from .builder import TargetIdentity
from .source import SourceIdentity

Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CaptureEvidence(Contract):
    manifest_sha256: Checksum
    audit_sha256: Checksum


TERMINAL = {"applied", "rolled_back"}
TRANSITIONS = {
    "draining": {"source_capturing"},
    "source_capturing": {"source_captured"},
    "source_captured": {"stopping_source"},
    "stopping_source": {"builder_starting"},
    "builder_starting": {"building"},
    "building": {"stopping_builder"},
    "stopping_builder": {"candidate_starting"},
    "candidate_starting": {"candidate_verifying"},
    "candidate_verifying": set(),
    "rollback_stopping": {"rollback_starting", "rollback_failed"},
    "rollback_starting": {"rollback_verifying", "rollback_failed"},
    "rollback_verifying": {"rollback_failed"},
    "rollback_failed": {"rollback_stopping"},
}
IDENTITY = (
    "operation_id",
    "data_batch_id",
    "config_revision_id",
    "previous_revision",
    "binding_digest",
    "previous_binding_id",
    "actor",
    "source",
    "source_lease",
)
MUTABLE = {
    "state",
    "error",
    "rollback_error",
    "waiting_reason",
    "builder_lease",
    "candidate_lease",
    "restored_lease",
    "progress",
    "executor_actor",
}


class RebuildStore:
    def __init__(self, registry):
        self.registry = registry

    @contextmanager
    def transaction(self):
        with self.registry.transaction() as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS index_rebuilds (id TEXT PRIMARY KEY, batch TEXT NOT NULL, record TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS index_rebuild_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT NOT NULL, operation TEXT NOT NULL, record TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS index_vector_bindings (id TEXT PRIMARY KEY, batch TEXT NOT NULL, revision TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL, evidence TEXT)",
                "CREATE TABLE IF NOT EXISTS index_binding_heads (batch TEXT PRIMARY KEY, binding TEXT NOT NULL)",
            ):
                db.execute(statement)
            yield db

    @staticmethod
    def read(db, batch, operation):
        row = db.execute(
            "SELECT record FROM index_rebuilds WHERE batch=? AND id=?",
            (batch, operation),
        ).fetchone()
        if row is None:
            raise KeyError(operation)
        record = json.loads(row[0])
        if (
            record.get("operation_id") != operation
            or record.get("data_batch_id") != batch
            or digest({key: record[key] for key in IDENTITY})
            != record.get("identity_digest")
        ):
            raise IntegrityError("Rebuild operation identity changed")
        if (
            record.get("state") not in set(TRANSITIONS) | TERMINAL
            or type(record.get("version")) is not int
        ):
            raise IntegrityError("Invalid rebuild operation state")
        if (
            type(record.get("terminal")) is not bool
            or record["terminal"] != (record["state"] in TERMINAL)
            or record["version"] < 1
        ):
            raise IntegrityError("Invalid rebuild operation completion marker")
        return record

    @staticmethod
    def write(db, record, event):
        record["updated_at"] = time.time()
        record["terminal"] = record["state"] in TERMINAL
        db.execute(
            "INSERT INTO index_rebuilds VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
            (record["operation_id"], record["data_batch_id"], json.dumps(record)),
        )
        db.execute(
            "INSERT INTO index_rebuild_events(batch,operation,record) VALUES (?,?,?)",
            (
                record["data_batch_id"],
                record["operation_id"],
                json.dumps({"event": event, **record}),
            ),
        )
        return record

    def get(self, batch, operation):
        with self.transaction() as db:
            return self.read(db, batch, operation)

    def binding(self, batch, operation):
        with self.transaction() as db:
            return read_binding(db, batch, operation)

    @staticmethod
    def fenced(db, registry, record):
        owner = registry.row(db, record["data_batch_id"])
        if (
            owner["pending"] != record["operation_id"]
            or owner["active"] != record["previous_revision"]
            or owner["desired"] != record["config_revision_id"]
        ):
            raise Conflict("Rebuild no longer owns the pending configuration")
        return owner

    def reserve(
        self,
        actor,
        request_id,
        fingerprint,
        revision,
        binding: VectorBinding,
        source: SourceIdentity,
        lease: RuntimeLease,
    ):
        batch, operation = binding.data_batch_id, binding.operation_id
        if (
            binding.config_revision_id != revision["id"]
            or source.data_batch_id != batch
            or source.operation_id != operation
            or source.workspace != binding.source_workspace
        ):
            raise IntegrityError("Rebuild binding/revision/source identities disagree")
        with self.transaction() as db:
            previous = self.registry.prior(
                db, actor, "index_rebuild", batch, request_id, fingerprint
            )
            if previous:
                return self.read(db, batch, previous["operation_id"])
            owner = self.registry.row(db, batch)
            if (
                owner["pending"]
                or owner["active"] != source.config_revision_id
                or owner["workspace"] != source.workspace
                or revision["id"] == owner["active"]
            ):
                raise Conflict(
                    "Rebuild source revision changed or another operation is pending"
                )
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            parent = db.execute(
                "SELECT components FROM revisions WHERE batch=? AND id=?",
                (batch, owner["active"]),
            ).fetchone()
            if (
                runtime.get("state") != "ready"
                or runtime.get("purpose", "rag") != "rag"
                or runtime.get("rag_digest") != json.loads(parent[0])["rag"]
                or any(
                    runtime.get(key) != getattr(lease, key)
                    for key in (
                        "data_batch_id",
                        "generation",
                        "lease_id",
                        "pid",
                        "process_identity",
                        "startup_digest",
                    )
                )
                or source.generation != lease.generation
                or source.lease_id != lease.lease_id
                or source.startup_digest != lease.startup_digest
                or source.workspace != lease.workspace
                or runtime.get("collections") != lease.collections
            ):
                raise Conflict(
                    "Rebuild requires the verified current source generation"
                )
            self.registry.add_revision(db, batch, revision)
            value = binding.model_dump()
            checksum = digest(value)
            record = {
                "operation_id": operation,
                "data_batch_id": batch,
                "config_revision_id": revision["id"],
                "previous_revision": owner["active"],
                "binding_digest": checksum,
                "previous_binding_id": head(db, batch),
                "actor": actor,
                "source": source.model_dump(),
                "source_lease": lease.model_dump(),
                "state": "draining",
                "version": 1,
                "created_at": time.time(),
                "error": None,
                "rollback_error": None,
                "waiting_reason": None,
                "cancel_requested": False,
            }
            record["identity_digest"] = digest({key: record[key] for key in IDENTITY})
            db.execute(
                "INSERT INTO index_vector_bindings VALUES (?,?,?,?,?,?,NULL)",
                (
                    operation,
                    batch,
                    revision["id"],
                    checksum,
                    "declared",
                    json.dumps(value),
                ),
            )
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?)",
                (operation, batch, revision["id"], "pending_restart", 1),
            )
            db.execute(
                "UPDATE batches SET desired=?,pending=? WHERE id=?",
                (revision["id"], operation, batch),
            )
            self.registry.remember(
                db,
                actor,
                "index_rebuild",
                batch,
                request_id,
                fingerprint,
                {"operation_id": operation},
            )
            return self.write(db, record, "requested")

    def transition(self, batch, operation, version, *, event, **values):
        if set(values) - MUTABLE or "state" not in values:
            raise ValueError(
                "Rebuild transition may change only its declared mutable fields"
            )
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            self.fenced(db, self.registry, record)
            if record["version"] != version:
                raise Conflict("Rebuild phase changed")
            current, target = record["state"], values["state"]
            allowed = TRANSITIONS.get(current, set()) | (
                {"rollback_stopping"}
                if current in TRANSITIONS and not current.startswith("rollback_")
                else set()
            )
            if target != current and target not in allowed:
                raise Conflict("Invalid rebuild phase transition")
            if target != current and target in {"source_captured", "stopping_builder"}:
                raise Conflict("This phase requires its dedicated evidence verifier")
            record.update(values, version=version + 1)
            return self.write(db, record, event)

    def request_cancel(self, batch, operation):
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            if record["terminal"]:
                return record
            self.fenced(db, self.registry, record)
            record.update(cancel_requested=True, version=record["version"] + 1)
            return self.write(db, record, "cancellation_requested")

    def finish_capture(
        self, batch, operation, source: SourceIdentity, evidence, pid, process_identity
    ):
        evidence = CaptureEvidence.model_validate(evidence).model_dump()
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            self.fenced(db, self.registry, record)
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            if (
                record["state"] != "source_capturing"
                or record["source"] != source.model_dump()
                or runtime.get("state") != "ready"
                or any(
                    runtime.get(key) != value
                    for key, value in {
                        "generation": source.generation,
                        "lease_id": source.lease_id,
                        "startup_digest": source.startup_digest,
                        "pid": pid,
                        "process_identity": process_identity,
                    }.items()
                )
            ):
                raise Conflict("Captured source no longer owns this rebuild generation")
            record.update(
                state="source_captured",
                source_evidence=evidence,
                version=record["version"] + 1,
            )
            return self.write(db, record, "source_captured")

    def verify_build(self, batch, operation, receipt, receipt_sha256, *, disposition):
        TypeAdapter(Checksum).validate_python(receipt_sha256, strict=True)
        if disposition not in {"owned", "gone"}:
            raise Conflict("Builder ownership or confirmed exit is required")
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            self.fenced(db, self.registry, record)
            binding_record = read_binding(db, batch, operation)
            binding = VectorBinding.model_validate(binding_record["binding"])
            target = binding.target(
                SourceIdentity.model_validate(record["source"]),
                record["source_evidence"]["manifest_sha256"],
            )
            if (
                record["state"] != "building"
                or TargetIdentity.model_validate(receipt["identity"]) != target
                or receipt.get("collections") != binding.collections
            ):
                raise IntegrityError(
                    "Build receipt does not match its pending vector binding"
                )
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            expected = record.get("builder_lease") or {}
            if (
                runtime.get("purpose") != "index_build"
                or runtime.get("application_id") != operation
                or runtime.get("config_revision_id") != record["config_revision_id"]
                or any(
                    runtime.get(key) != expected.get(key)
                    for key in (
                        "generation",
                        "lease_id",
                        "pid",
                        "process_identity",
                        "startup_digest",
                    )
                )
                or disposition == "owned"
                and runtime.get("state") != "built"
                or disposition == "gone"
                and runtime.get("state") not in {"failed", "stopped"}
            ):
                raise Conflict(
                    "Build receipt has no matching owned builder reservation"
                )
            evidence = {
                "receipt_sha256": receipt_sha256,
                "source_audit_sha256": record["source_evidence"]["audit_sha256"],
                "receipt": receipt,
            }
            if binding_record["state"] != "declared":
                raise Conflict("Vector binding was already verified or abandoned")
            db.execute(
                "UPDATE index_vector_bindings SET state='verified',evidence=? WHERE batch=? AND id=?",
                (
                    json.dumps({"digest": digest(evidence), "value": evidence}),
                    batch,
                    operation,
                ),
            )
            record.update(
                state="stopping_builder",
                progress={"state": "verified", **receipt["counts"]},
                version=record["version"] + 1,
                waiting_reason=None,
            )
            return self.write(db, record, "build_verified")

    @staticmethod
    def runtime_matches(runtime, lease, revision):
        return (
            runtime.get("state") == "ready"
            and runtime.get("purpose", "rag") == "rag"
            and runtime.get("config_revision_id") == revision
            and runtime.get("workspace") == lease.workspace
            and runtime.get("collections") == lease.collections
            and all(
                runtime.get(key) == getattr(lease, key)
                for key in (
                    "data_batch_id",
                    "generation",
                    "lease_id",
                    "pid",
                    "process_identity",
                    "startup_digest",
                )
            )
        )

    def activate(self, batch, operation, lease: RuntimeLease, proof):
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            self.fenced(db, self.registry, record)
            binding = read_binding(db, batch, operation)
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            evidence = binding["evidence"] or {}
            expected = {
                "binding_digest": binding["digest"],
                "receipt_sha256": evidence.get("receipt_sha256"),
                "source_audit_sha256": evidence.get("source_audit_sha256"),
            }
            vector = binding["binding"]
            expected_collections = {
                "graph": record["source_lease"]["collections"]["graph"],
                **{
                    role: vector["collections"][role]
                    for role in ("entities", "relationships", "chunks")
                },
            }
            expected_auxiliary = (
                {"name_entities": vector["collections"]["name_entities"]}
                if "name_entities" in vector["collections"]
                else {}
            )
            if (
                record["state"] != "candidate_verifying"
                or record["cancel_requested"]
                or binding["state"] != "verified"
                or not self.runtime_matches(
                    runtime, lease, record["config_revision_id"]
                )
                or runtime.get("application_id") != operation
                or proof.get("index_initial_verified") != expected
                or proof.get("index_binding_id") != operation
                or proof.get("index_binding_digest") != binding["digest"]
                or any(
                    proof.get(key) != runtime.get(key)
                    for key in (
                        "data_batch_id",
                        "generation",
                        "pid",
                        "process_identity",
                        "startup_digest",
                    )
                )
            ):
                raise IntegrityError(
                    "Index activation has no exact verified full RAG generation"
                )
            revision = db.execute(
                "SELECT components FROM revisions WHERE batch=? AND id=?",
                (batch, record["config_revision_id"]),
            ).fetchone()
            if runtime.get("rag_digest") != json.loads(revision[0])["rag"]:
                raise IntegrityError(
                    "Candidate RAG configuration digest changed before activation"
                )
            if (
                proof.get("workspace") != vector["source_workspace"]
                or lease.collections != expected_collections
                or proof.get("collections") != expected_collections
                or proof.get("auxiliary_collections", {}) != expected_auxiliary
                or proof.get("embedding")
                != {
                    "model": vector["embedding_model"],
                    "dimensions": vector["dimensions"],
                }
            ):
                raise IntegrityError(
                    "Candidate storage/model bindings differ at index activation"
                )
            if head(db, batch) != record["previous_binding_id"]:
                raise Conflict("Previous vector binding changed before activation")
            active_tasks = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM tasks WHERE batch=?", (batch,)
                )
            ]
            if any(task["state"] in {"admitted", "running"} for task in active_tasks):
                raise Conflict("Batch still has active tasks before index activation")
            if record["previous_binding_id"] is not None:
                db.execute(
                    "UPDATE index_vector_bindings SET state='retired' WHERE batch=? AND id=? AND state='active'",
                    (batch, record["previous_binding_id"]),
                )
            db.execute(
                "UPDATE index_vector_bindings SET state='active' WHERE batch=? AND id=?",
                (batch, operation),
            )
            db.execute(
                "INSERT INTO index_binding_heads VALUES (?,?) ON CONFLICT(batch) DO UPDATE SET binding=excluded.binding",
                (batch, operation),
            )
            populated = evidence["receipt"]["counts"]["total"] > 0
            db.execute(
                "UPDATE batches SET active=desired,desired=NULL,pending=NULL,lifecycle='ready',index_state=? WHERE id=?",
                ("populated" if populated else "empty", batch),
            )
            db.execute(
                "UPDATE operations SET status='applied' WHERE batch=? AND id=?",
                (batch, operation),
            )
            for task in active_tasks:
                if task["state"] == "queued":
                    task.update(
                        state="interrupted",
                        phase="interrupted",
                        finished_at=time.time(),
                        error="Index binding changed before execution; retry this task explicitly",
                    )
                    TaskStore._write(db, task, "index_binding_retired")
            record.update(
                state="applied",
                candidate_lease=lease.model_dump(),
                version=record["version"] + 1,
                finished_at=time.time(),
                waiting_reason=None,
            )
            return self.write(db, record, "applied")

    def restore(self, batch, operation, lease: RuntimeLease, proof):
        with self.transaction() as db:
            record = self.read(db, batch, operation)
            self.fenced(db, self.registry, record)
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            if (
                record["state"] != "rollback_verifying"
                or not self.runtime_matches(runtime, lease, record["previous_revision"])
                or runtime.get("rebuild_rollback_id") != operation
                or head(db, batch) != record["previous_binding_id"]
                or proof.get("index_binding_id") != record["previous_binding_id"]
                or any(
                    proof.get(key) != runtime.get(key)
                    for key in (
                        "data_batch_id",
                        "generation",
                        "pid",
                        "process_identity",
                        "startup_digest",
                    )
                )
                or record.get("source_evidence")
                and proof.get("index_source_restored")
                != record["source_evidence"]["audit_sha256"]
            ):
                raise IntegrityError(
                    "Rebuild rollback has no verified previous runtime/source binding"
                )
            revision = db.execute(
                "SELECT components FROM revisions WHERE batch=? AND id=?",
                (batch, record["previous_revision"]),
            ).fetchone()
            if runtime.get("rag_digest") != json.loads(revision[0])["rag"]:
                raise IntegrityError("Restored RAG configuration digest changed")
            if (
                lease.collections != record["source_lease"]["collections"]
                or proof.get("collections") != lease.collections
                or proof.get("workspace") != lease.workspace
            ):
                raise IntegrityError(
                    "Restored runtime did not reuse the original physical stores"
                )
            db.execute(
                "UPDATE index_vector_bindings SET state='abandoned' WHERE batch=? AND id=?",
                (batch, operation),
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
                restored_lease=lease.model_dump(),
                version=record["version"] + 1,
                finished_at=time.time(),
                waiting_reason=None,
                rollback_error=None,
            )
            return self.write(db, record, "previous_index_restored")

    def pending(self):
        with self.transaction() as db:
            return [
                record
                for row in db.execute(
                    "SELECT batch,id FROM index_rebuilds ORDER BY rowid"
                )
                if (record := self.read(db, row["batch"], row["id"]))["state"]
                not in TERMINAL | {"rollback_failed"}
            ]

    def list(self, batch, limit=20):
        with self.transaction() as db:
            self.registry.row(db, batch)
            return [
                self.read(db, batch, row[0])
                for row in db.execute(
                    "SELECT id FROM index_rebuilds WHERE batch=? ORDER BY rowid DESC LIMIT ?",
                    (batch, limit),
                ).fetchall()
            ]

    def events(self, batch, operation):
        with self.transaction() as db:
            self.read(db, batch, operation)
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM index_rebuild_events WHERE batch=? AND operation=? ORDER BY sequence",
                    (batch, operation),
                )
            ]
