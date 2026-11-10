"""Supervisor proof checks permit only the exact registered vector-only exception."""

# ruff: noqa: F811

from pathlib import Path
import json
import os
import time

import pytest

from lightrag.batch_control.storage import IntegrityError
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_process import ProcessBackend
from lightrag.batch_control.index_rebuild.store import RebuildStore
from .test_runtime import runtime_setup  # noqa: F401
from .test_index_rebuild_store import advance, built, rebuild  # noqa: F401
from .test_index_rebuild_store import prepare_rebuild
from .test_applications import setup  # noqa: F401

pytestmark = pytest.mark.offline


def configured_candidate(case, corruption=None):
    built(case)
    advance(case, "candidate_starting")

    def observation(proof):
        spec = case.backend.specs[proof["pid"]]
        index = spec["startup"].get("index_binding")
        if index is None:
            return proof
        root = Path(spec["startup"]["environment"]["WORKING_DIR"])
        proof["storage_workspaces"]["vector_storage"] = index["vector_workspace"]
        proof["collections"].update(
            {
                key: index["collections"][key]
                for key in ("entities", "relationships", "chunks")
            }
        )
        proof["storage_files"].update(
            {
                key: str(root / index["vector_workspace"] / ("vdb_" + key + ".json"))
                for key in ("entities", "relationships", "chunks")
            }
        )
        proof["index_binding_id"] = index["operation_id"]
        proof["index_binding_digest"] = spec["startup"]["index_binding_digest"]
        proof["index_initial_verified"] = {
            "binding_digest": case.record["binding_digest"],
            "receipt_sha256": "3" * 64,
            "source_audit_sha256": "2" * 64,
        }
        if corruption == "vector_scope":
            proof["storage_workspaces"]["vector_storage"] = case.batch["workspace"]
        elif corruption == "graph_scope":
            proof["storage_workspaces"]["graph_storage"] = index["vector_workspace"]
        elif corruption == "digest":
            proof["index_binding_digest"] = "f" * 64
        elif corruption == "collection":
            proof["collections"]["entities"] = case.batch["workspace"] + "_entities"
        elif corruption == "file":
            proof["storage_files"]["entities"] = str(
                root / case.batch["workspace"] / "vdb_entities.json"
            )
        elif corruption == "model":
            proof["embedding"]["model"] = "previous-model"
        return proof

    case.backend.change = observation


def test_matching_candidate_keeps_graph_kv_scope_and_can_publish_then_be_recovered(
    rebuild,
):
    case = rebuild
    configured_candidate(case)
    lease = case.runtime.start(
        case.batch["id"], case.revision["id"], application_id=case.operation
    )
    assert lease.collections["graph"] == case.source_lease.collections["graph"]
    assert {
        lease.collections[key] for key in ("entities", "relationships", "chunks")
    } == set(case.binding.collections.values())
    advance(case, "candidate_verifying")
    proof = case.runtime._proof(case.runtime.store.get(case.batch["id"]))
    case.store.activate(case.batch["id"], case.operation, lease, proof)
    assert case.runtime.inspect(case.batch["id"]) == lease
    # Binding metadata stays identical when verified -> active; no spurious
    # restart is needed just because the operation committed.
    assert case.runtime.start(case.batch["id"], case.revision["id"]) == lease


@pytest.mark.parametrize(
    "corruption",
    ["vector_scope", "graph_scope", "digest", "collection", "file", "model"],
)
def test_invalid_candidate_is_never_ready_or_activated(rebuild, corruption):
    case = rebuild
    configured_candidate(case, corruption)
    with pytest.raises(IntegrityError):
        case.runtime.start(
            case.batch["id"], case.revision["id"], application_id=case.operation
        )
    owner = case.control.registry.get(case.batch["id"])
    assert (
        owner["active"] == case.batch["active"] and owner["pending"] == case.operation
    )
    assert case.store.binding(case.batch["id"], case.operation)["state"] == "verified"


@pytest.mark.parametrize("capture_before_cancel", [False, True])
def test_real_source_runtime_is_restored_after_cancel_without_affecting_peer(
    runtime_setup,
    capture_before_cancel,
):
    control, policy = runtime_setup
    backend = ProcessBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    peer = control.create("user", "Peer", "peer")
    stoppers = {}
    evidence = None
    try:
        peer_lease = runtime.start(peer["id"], peer["active"])
        case = prepare_rebuild(control, policy, backend, runtime)
        if capture_before_cancel:
            advance(case, "source_capturing")
            deadline = time.monotonic() + 30
            while True:
                captured = case.store.get(case.batch["id"], case.operation)
                if captured["state"] != "source_capturing":
                    break
                assert time.monotonic() < deadline, runtime.store.get(case.batch["id"])
                assert backend.ownership(runtime.store.get(case.batch["id"])) == "owned"
                time.sleep(0.05)
            assert captured["state"] == "source_captured", captured
            assert set(captured["source_evidence"]) == {
                "manifest_sha256",
                "audit_sha256",
            }
        case.store.request_cancel(case.batch["id"], case.operation)
        advance(case, "rollback_stopping", error="Cancelled by the process test")
        previous_runtime = runtime.store.get(case.batch["id"])
        runtime.stop(case.batch["id"])
        assert backend.ownership(previous_runtime) == "gone"
        assert runtime.inspect(peer["id"]) == peer_lease
        advance(case, "rollback_starting")
        # A reconstructed manager uses the durable rollback request. It does not
        # need to reuse any Python object from the original coordinator.
        restored = RuntimeSupervisor(control, policy, actor="user")
        stoppers[case.batch["id"]] = restored
        lease = restored.start(
            case.batch["id"], case.batch["active"], rebuild_rollback_id=case.operation
        )
        assert lease.pid != case.source_lease.pid
        assert lease.collections == case.source_lease.collections
        advance(case, "rollback_verifying")
        proof = restored._proof(restored.store.get(case.batch["id"]))
        if capture_before_cancel:
            assert (
                proof["index_source_restored"]
                == captured["source_evidence"]["audit_sha256"]
            )
        else:
            assert "index_source_restored" not in proof
        result = RebuildStore(control.registry).restore(
            case.batch["id"], case.operation, lease, proof
        )
        assert result["state"] == "rolled_back"
        assert control.registry.get(case.batch["id"])["pending"] is None
        assert restored.inspect(case.batch["id"]) == lease
        assert runtime.inspect(peer["id"]) == peer_lease
        evidence = {
            "scope": "actual source capture/restoration"
            if capture_before_cancel
            else "actual cancelled-before-capture restoration",
            "full_index_rebuild_acceptance": False,
            "source_evidence": captured["source_evidence"]
            if capture_before_cancel
            else None,
            "operation_id": case.operation,
            "source_lease": case.source_lease.model_dump(),
            "restored_lease": lease.model_dump(),
            "peer_lease": peer_lease.model_dump(),
            "peer_generation_unchanged": True,
            "source_previous_process_exited": True,
            "state": result["state"],
            "binding_state": case.store.binding(case.batch["id"], case.operation)[
                "state"
            ],
        }
    finally:
        with control.registry.transaction() as db:
            batches = [row[0] for row in db.execute("SELECT id FROM batches")]
        for batch in batches:
            stoppers.get(batch, runtime).stop(batch)
        assert all(
            runtime.store.get(batch) is None
            or runtime.store.get(batch)["state"] == "stopped"
            for batch in batches
        )
        assert all(
            stoppers.get(batch, runtime).backend.ownership(runtime.store.get(batch))
            == "gone"
            for batch in batches
            if runtime.store.get(batch) is not None
        )
        if evidence is not None and os.getenv(
            "LIGHTRAG_INDEX_ROLLBACK_VALIDATION_RECORD"
        ):
            evidence["all_recorded_children_stopped"] = True
            path = Path(os.environ["LIGHTRAG_INDEX_ROLLBACK_VALIDATION_RECORD"])
            if capture_before_cancel:
                path = path.with_stem(path.stem + "-after-capture")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2) + "\n")
