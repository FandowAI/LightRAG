"""Registry transactions fence rebuilds and require exact candidate/restore identity.

Runtime rows here are explicit fake observations; actual process readiness is a
separate acceptance gate. These tests do not establish live activation.
"""

# ruff: noqa: F811

import json
from types import SimpleNamespace

import pytest

from lightrag.batch_control.applications import ApplicationService
from lightrag.batch_control.contracts import Configuration, RuntimeLease
from lightrag.batch_control.index_rebuild.authority import VectorBinding, for_manifest
from lightrag.batch_control.index_rebuild.builder import target_namespace
from lightrag.batch_control.index_rebuild.source import SourceIdentity
from lightrag.batch_control.index_rebuild.store import RebuildStore
from lightrag.batch_control.resources import digest
from lightrag.batch_control.runtime_config import compile_startup
from lightrag.batch_control.storage import Conflict, IntegrityError
from lightrag.batch_control.settings import SettingsService
from lightrag.batch_control.task_store import TaskStore
from tests.batch_control.test_applications import setup  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.fixture
def rebuild(setup):
    return prepare_rebuild(*setup)


def prepare_rebuild(control, policy, backend, runtime, *, seed=None):
    control.catalog.profiles["embed-v2"] = {
        "model_name": "new-offline-embedding",
        "api_url": "http://127.0.0.1:9/v1",
        "dimensions": 16,
    }
    batch = control.create("user", "Rebuild", "create")
    if seed is not None:
        seed(batch)
    lease = runtime.start(batch["id"], batch["active"])
    queued = control.register_task(
        "user",
        batch["id"],
        batch["active"],
        "old-query",
        "query",
        {"data_batch_id": batch["id"], "type": "index"},
        payload={"version": 1},
    )
    owner, _, before, prompts, _ = control.read("user", batch["id"])
    value = before.model_dump()
    value["rag"]["embedding_profile"] = "embed-v2"
    document, prompts, execution, bridge = control._prepare(
        "user",
        owner,
        Configuration.model_validate(value),
        control._inherit(before, prompts),
    )
    operation, revision_id = "op-rebuild", "rev-index"
    namespace = target_namespace(batch["id"], operation)
    binding = VectorBinding(
        data_batch_id=batch["id"],
        operation_id=operation,
        config_revision_id=revision_id,
        source_workspace=batch["workspace"],
        vector_workspace=namespace,
        storage_mode="local",
        embedding_profile="embed-v2",
        embedding_profile_sha256=digest(execution["rag_profiles"]["embed-v2"]),
        embedding_model="new-offline-embedding",
        dimensions=16,
        collections={
            role: namespace + "_" + role
            for role in ("entities", "relationships", "chunks")
        },
    )
    revision = control.files.publish(
        owner,
        revision_id,
        document,
        prompts,
        execution,
        bridge,
        {
            "parent_revision": batch["active"],
            "index_binding_id": operation,
            "index_binding_digest": digest(binding.model_dump()),
        },
    )
    source = SourceIdentity(
        data_batch_id=batch["id"],
        operation_id=operation,
        config_revision_id=batch["active"],
        workspace=batch["workspace"],
        generation=lease.generation,
        lease_id=lease.lease_id,
        startup_digest=lease.startup_digest,
    )
    store = RebuildStore(control.registry)
    fingerprint = digest({"revision": batch["active"], "embedding_profile": "embed-v2"})
    record = store.reserve(
        "user", "rebuild", fingerprint, revision, binding, source, lease
    )
    return SimpleNamespace(
        control=control,
        policy=policy,
        backend=backend,
        runtime=runtime,
        batch=batch,
        source_lease=lease,
        queued=queued,
        binding=binding,
        revision=revision,
        source=source,
        store=store,
        record=record,
        fingerprint=fingerprint,
        operation=operation,
    )


def advance(case, state, **values):
    record = case.store.get(case.batch["id"], case.operation)
    return case.store.transition(
        case.batch["id"],
        case.operation,
        record["version"],
        event="test-phase",
        state=state,
        **values,
    )


def runtime_row(
    case, revision, *, purpose="rag", state="ready", collections=None, rollback=False
):
    previous = case.runtime.store.get(case.batch["id"])
    record = {
        **previous,
        "generation": previous["generation"] + 1,
        "lease_id": "lease-generation-" + str(previous["generation"] + 1),
        "config_revision_id": revision,
        "state": state,
        "purpose": purpose,
        "application_id": None if rollback else case.operation,
        "rebuild_rollback_id": case.operation if rollback else None,
        "pid": previous["pid"] + 1,
        "process_identity": "fake-birth-" + str(previous["generation"] + 1),
        "startup_digest": digest({"generation": previous["generation"] + 1}),
        "collections": collections or case.source_lease.collections,
        "rag_digest": case.control.registry.revision(case.batch["id"], revision)[
            "components"
        ]["rag"],
    }
    with case.control.registry.transaction() as db:
        db.execute(
            "UPDATE runtimes SET generation=?,state=?,record=? WHERE batch=?",
            (record["generation"], state, json.dumps(record), case.batch["id"]),
        )
    return record


def built(case):
    advance(case, "source_capturing")
    source = case.runtime.store.get(case.batch["id"])
    case.store.finish_capture(
        case.batch["id"],
        case.operation,
        case.source,
        {"manifest_sha256": "1" * 64, "audit_sha256": "2" * 64},
        source["pid"],
        source["process_identity"],
    )
    advance(case, "stopping_source")
    advance(case, "builder_starting")
    builder = runtime_row(
        case, case.revision["id"], purpose="index_build", state="built"
    )
    advance(case, "building", builder_lease=builder)
    target = case.binding.target(case.source, "1" * 64)
    receipt = {
        "schema_version": 1,
        "identity": target.model_dump(),
        "collections": case.binding.collections,
        "counts": {"total": 4, "written": 4, "verified": 4},
    }
    case.store.verify_build(
        case.batch["id"], case.operation, receipt, "3" * 64, disposition="owned"
    )
    return receipt


def candidate(case):
    built(case)
    advance(case, "candidate_starting")
    collections = {
        "graph": case.source_lease.collections["graph"],
        **case.binding.collections,
    }
    row = runtime_row(case, case.revision["id"], collections=collections)
    lease = RuntimeLease.model_validate(
        {key: row[key] for key in RuntimeLease.model_fields}
    )
    advance(case, "candidate_verifying")
    proof = {
        **row,
        "index_binding_id": case.operation,
        "index_binding_digest": case.record["binding_digest"],
        "index_initial_verified": {
            "binding_digest": case.record["binding_digest"],
            "receipt_sha256": "3" * 64,
            "source_audit_sha256": "2" * 64,
        },
        "embedding": {
            "model": case.binding.embedding_model,
            "dimensions": case.binding.dimensions,
        },
    }
    return lease, proof


def test_builder_reservation_cannot_be_ready_replayed_or_escape_source_identity(
    rebuild,
):
    case = rebuild
    advance(case, "source_capturing")
    case.store.finish_capture(
        case.batch["id"],
        case.operation,
        case.source,
        {"manifest_sha256": "1" * 64, "audit_sha256": "2" * 64},
        case.source_lease.pid,
        case.source_lease.process_identity,
    )
    advance(case, "stopping_source")
    case.runtime.stop(case.batch["id"])
    advance(case, "builder_starting")
    store = case.runtime.store
    row = store.reserve(
        case.batch["id"],
        case.revision["id"],
        "a" * 64,
        "b" * 64,
        2,
        "user",
        application_id=case.operation,
        purpose="index_build",
    )
    assert row["purpose"] == "index_build"
    with pytest.raises(Conflict, match="RAG readiness"):
        store.update(
            case.batch["id"],
            row["generation"],
            {"reserved"},
            "user",
            "invalid-builder-readiness",
            state="ready",
        )
    store.update(
        case.batch["id"],
        row["generation"],
        {"reserved"},
        "user",
        "test-confirmed-no-child",
        state="failed",
    )
    with pytest.raises(Conflict, match="cannot be replayed"):
        store.reserve(
            case.batch["id"],
            case.revision["id"],
            "a" * 64,
            "b" * 64,
            2,
            "user",
            application_id=case.operation,
            purpose="index_build",
        )
    assert case.control.registry.get(case.batch["id"])["active"] == case.batch["active"]


def test_reservation_is_atomic_idempotent_scoped_and_keeps_old_active(rebuild):
    case = rebuild
    owner = case.control.registry.get(case.batch["id"])
    assert (
        owner["active"] == case.batch["active"]
        and owner["desired"] == case.revision["id"]
        and owner["pending"] == case.operation
    )
    assert case.store.binding(case.batch["id"], case.operation)["state"] == "declared"
    assert (
        case.store.reserve(
            "user",
            "rebuild",
            case.fingerprint,
            case.revision,
            case.binding,
            case.source,
            case.source_lease,
        )
        == case.record
    )
    with pytest.raises(Conflict):
        case.store.reserve(
            "user",
            "rebuild",
            "other-fingerprint",
            case.revision,
            case.binding,
            case.source,
            case.source_lease,
        )
    with pytest.raises(Conflict):
        case.control.register_task(
            "user",
            case.batch["id"],
            case.batch["active"],
            "new-query",
            "query",
            {"data_batch_id": case.batch["id"], "type": "index"},
        )
    peer = case.control.create("user", "Peer", "peer")
    with pytest.raises(KeyError):
        case.store.get(peer["id"], case.operation)


def test_stale_or_unverified_phase_advances_are_rejected(rebuild):
    case = rebuild
    record = advance(case, "source_capturing")
    with pytest.raises(Conflict):
        case.store.transition(
            case.batch["id"],
            case.operation,
            case.record["version"],
            event="stale",
            state="rollback_stopping",
        )
    with pytest.raises(Conflict):
        case.store.transition(
            case.batch["id"],
            case.operation,
            record["version"],
            event="unproven",
            state="source_captured",
        )
    with pytest.raises(ValueError):
        case.store.transition(
            case.batch["id"],
            case.operation,
            record["version"],
            event="foreign",
            state="source_capturing",
            actor="other",
        )
    with pytest.raises(Conflict):
        case.store.finish_capture(
            case.batch["id"],
            case.operation,
            case.source,
            {"manifest_sha256": "1" * 64, "audit_sha256": "2" * 64},
            7,
            "foreign",
        )


def test_only_index_controller_can_apply_or_discard_this_pending_operation(rebuild):
    case = rebuild
    application = ApplicationService(
        case.control, case.policy, actor="user", backend=case.backend
    )
    with pytest.raises(Conflict, match="index rebuild"):
        application.request(
            case.batch["id"], case.operation, case.revision["id"], "wrong-controller"
        )
    with pytest.raises(Conflict, match="index rebuilding"):
        application.discard(case.batch["id"], case.operation)
    assert case.backend.signals == []
    assert case.control.registry.get(case.batch["id"])["pending"] == case.operation


def test_declared_binding_compiles_only_for_preflight_or_its_build_phase(rebuild):
    case = rebuild
    owner, _, document, _, manifest = case.control.read(
        "user", case.batch["id"], case.revision["id"]
    )
    with pytest.raises(Conflict):
        compile_startup(
            case.control,
            "user",
            owner,
            case.revision["id"],
            document,
            manifest,
            case.policy,
        )
    startup, _ = compile_startup(
        case.control,
        "user",
        owner,
        case.revision["id"],
        document,
        manifest,
        case.policy,
        index_purpose="index_preflight",
    )
    assert startup["index_binding"] == case.binding.model_dump()
    assert startup["index_binding_digest"] == case.record["binding_digest"]
    assert startup["environment"]["EMBEDDING_DIM"] == "16"
    assert startup["environment"]["WORKSPACE"] == case.batch["workspace"]


def test_activation_publishes_revision_and_binding_together_and_retires_old_queued_work(
    rebuild,
):
    case = rebuild
    lease, proof = candidate(case)
    result = case.store.activate(case.batch["id"], case.operation, lease, proof)
    owner = case.control.registry.get(case.batch["id"])
    assert (
        owner["active"] == case.revision["id"]
        and owner["pending"] is None
        and owner["desired"] is None
    )
    assert result["state"] == "applied" and result["terminal"] is True
    assert case.store.binding(case.batch["id"], case.operation)["state"] == "active"
    assert (
        case.control.registry.task(case.batch["id"], case.queued["task_id"])["state"]
        == "interrupted"
    )
    assert (
        case.store.reserve(
            "user",
            "rebuild",
            case.fingerprint,
            case.revision,
            case.binding,
            case.source,
            case.source_lease,
        )["state"]
        == "applied"
    )
    assert [
        row["event"] for row in case.store.events(case.batch["id"], case.operation)
    ][-1] == "applied"


@pytest.mark.parametrize(
    "corruption",
    [
        "builder",
        "generation",
        "collections",
        "embedding",
        "source_audit",
        "cancelled",
        "busy",
    ],
)
def test_activation_rejects_every_mismatched_candidate_without_releasing_admission(
    rebuild, corruption
):
    case = rebuild
    lease, proof = candidate(case)
    if corruption == "builder":
        row = case.runtime.store.get(case.batch["id"])
        row["purpose"] = "index_build"
        with case.control.registry.transaction() as db:
            db.execute(
                "UPDATE runtimes SET record=? WHERE batch=?",
                (json.dumps(row), case.batch["id"]),
            )
    elif corruption == "generation":
        proof["generation"] += 1
    elif corruption == "collections":
        proof["collections"] = {**proof["collections"], "entities": "foreign_vectors"}
    elif corruption == "embedding":
        proof["embedding"]["model"] = "other-model"
    elif corruption == "source_audit":
        proof["index_initial_verified"]["source_audit_sha256"] = "f" * 64
    elif corruption == "cancelled":
        case.store.request_cancel(case.batch["id"], case.operation)
    else:
        TaskStore(case.control.registry).update(
            case.batch["id"], case.queued["task_id"], state="running"
        )
    with pytest.raises((IntegrityError, Conflict)):
        case.store.activate(case.batch["id"], case.operation, lease, proof)
    owner = case.control.registry.get(case.batch["id"])
    assert (
        owner["active"] == case.batch["active"] and owner["pending"] == case.operation
    )
    assert case.store.binding(case.batch["id"], case.operation)["state"] == "verified"


def test_rollback_requires_exact_restored_generation_and_preserves_old_configuration(
    rebuild,
):
    case = rebuild
    built(case)
    advance(case, "rollback_stopping", error="candidate failed")
    advance(case, "rollback_starting")
    row = runtime_row(case, case.batch["active"], rollback=True)
    lease = RuntimeLease.model_validate(
        {key: row[key] for key in RuntimeLease.model_fields}
    )
    proof = {**row, "index_binding_id": None, "index_source_restored": "2" * 64}
    advance(case, "rollback_verifying")
    with pytest.raises(IntegrityError):
        case.store.restore(
            case.batch["id"],
            case.operation,
            lease,
            {**proof, "index_source_restored": "f" * 64},
        )
    result = case.store.restore(case.batch["id"], case.operation, lease, proof)
    owner = case.control.registry.get(case.batch["id"])
    assert result["state"] == "rolled_back" and result["error"] == "candidate failed"
    assert owner["active"] == case.batch["active"] and owner["pending"] is None
    assert case.store.binding(case.batch["id"], case.operation)["state"] == "abandoned"
    assert (
        case.control.registry.task(case.batch["id"], case.queued["task_id"])["state"]
        == "queued"
    )


def test_subsequent_recipe_only_revision_inherits_exact_active_binding(rebuild):
    case = rebuild
    lease, proof = candidate(case)
    case.store.activate(case.batch["id"], case.operation, lease, proof)
    current = case.control.read("user", case.batch["id"])[2].model_dump()
    current["recipe"]["workflow"]["parameters"]["questions_per_source"] = 5
    preview = SettingsService(case.control, case.policy, actor="user").preview(
        case.batch["id"], case.revision["id"], Configuration.model_validate(current)
    )
    assert (
        preview["draft"]["recipe"]["workflow"]["parameters"]["questions_per_source"]
        == 5
    )
    saved = case.control.save(
        "user",
        case.batch["id"],
        case.revision["id"],
        Configuration.model_validate(current),
        "later-prompt",
    )
    owner, _, document, _, manifest = case.control.read("user", case.batch["id"])
    assert not saved["requires_restart"]
    assert manifest["sources"]["index_binding_id"] == case.operation
    assert manifest["sources"]["index_binding_digest"] == case.record["binding_digest"]
    selected = for_manifest(case.control.registry, owner, saved["revision"], manifest)
    assert selected["binding"] == case.binding.model_dump()
    startup, _ = compile_startup(
        case.control, "user", owner, saved["revision"], document, manifest, case.policy
    )
    assert startup["index_binding"]["vector_workspace"] == case.binding.vector_workspace


@pytest.mark.parametrize("target", ["binding", "evidence", "operation"])
def test_mutated_immutable_registry_evidence_is_rejected(rebuild, target):
    case = rebuild
    built(case)
    with case.control.registry.transaction() as db:
        table = "index_rebuilds" if target == "operation" else "index_vector_bindings"
        column = "evidence" if target == "evidence" else "record"
        value = json.loads(
            db.execute(
                f"SELECT {column} FROM {table} WHERE batch=? AND id=?",
                (case.batch["id"], case.operation),
            ).fetchone()[0]
        )
        if target == "binding":
            value["embedding_model"] = "different-model"
        elif target == "evidence":
            value["value"]["receipt"]["counts"]["total"] += 1
        else:
            value["actor"] = "other-user"
        db.execute(
            f"UPDATE {table} SET {column}=? WHERE batch=? AND id=?",
            (json.dumps(value), case.batch["id"], case.operation),
        )
    with pytest.raises(IntegrityError):
        if target == "operation":
            case.store.get(case.batch["id"], case.operation)
        else:
            case.store.binding(case.batch["id"], case.operation)
