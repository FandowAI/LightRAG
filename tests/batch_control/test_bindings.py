"""Adoption reuses exact storage scopes and verifies its initial graph once."""

# ruff: noqa: F811

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from lightrag.batch_control.bindings import (
    BindingStore,
    StorageBinding,
    rows_digest,
    verify_storage_binding,
)
from lightrag.batch_control.resources import digest
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_config import compile_startup
from lightrag.batch_control.storage import IntegrityError
from tests.batch_control.test_runtime import managed, runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline


def register_binding(control, batch, tmp_path, *, graph=None, actual_local=False):
    binding = StorageBinding(
        workspace=batch["workspace"],
        working_directory=str(tmp_path / "old-storage"),
        input_directory=str(tmp_path / "old-inputs"),
        storage_mode="local",
        collections={
            key: batch["workspace"]
            + "_"
            + ("chunk_entity_relation" if actual_local and key == "graph" else key)
            for key in ("graph", "entities", "relationships", "chunks")
        },
        embedding={"model": "offline-embedding", "dimensions": 8},
        graph=graph
        or {
            "nodes": 0,
            "edges": 0,
            "nodes_sha256": rows_digest([]),
            "edges_sha256": rows_digest([]),
        },
        source_observation_sha256="a" * 64,
    ).model_dump()
    store = BindingStore(control.registry)
    with store.transaction() as db:
        store.register(
            db,
            batch["id"],
            binding,
            {"adopted-test-v1": {"model_name": "original-model"}},
        )
    owner, _, document, prompts, manifest = control.read("user", batch["id"])
    revision = control.files.publish(
        owner,
        "rev-adopted",
        document,
        prompts,
        manifest["execution"],
        control.catalog.bridge(control.paths, owner, document),
        {},
    )
    with control.registry.transaction() as db:
        control.registry.add_revision(db, batch["id"], revision)
        db.execute(
            "UPDATE batches SET active=? WHERE id=?", (revision["id"], batch["id"])
        )
    return store, binding, control.registry.get(batch["id"])


def test_binding_identity_is_checked_before_storage_initialization(managed, tmp_path):
    control, runtime, _ = managed
    batch = control.create("user", "Old data", "old")
    _, binding, batch = register_binding(control, batch, tmp_path)
    _, _, document, _, manifest = control.read("user", batch["id"])
    startup, _ = compile_startup(
        control, "user", batch, batch["active"], document, manifest, runtime.policy
    )
    identity = {
        key: binding[key]
        for key in (
            "workspace",
            "working_directory",
            "input_directory",
            "embedding",
            "collections",
        )
    }
    identity["storage_classes"] = {
        key: startup["environment"]["LIGHTRAG_" + key.upper()]
        for key in (
            "kv_storage",
            "doc_status_storage",
            "graph_storage",
            "vector_storage",
        )
    }
    verify_storage_binding(identity, startup)
    for key in identity:
        corrupt = {**identity, key: "other-storage"}
        with pytest.raises(IntegrityError, match="before initialization"):
            verify_storage_binding(corrupt, startup)
    with pytest.raises(IntegrityError, match="startup binding changed"):
        verify_storage_binding(
            identity, {**startup, "storage_binding_digest": "b" * 64}
        )


@pytest.mark.parametrize(
    "field", ["collections", "adoption_graph", "storage_binding_digest"]
)
def test_invalid_adoption_proof_never_promotes_or_completes(managed, tmp_path, field):
    control, runtime, backend = managed
    batch = control.create("user", "Old data", "old")
    store, binding, batch = register_binding(control, batch, tmp_path)

    def wrong(proof):
        proof.update(
            storage_binding_digest=digest(binding), adoption_graph=binding["graph"]
        )
        proof[field] = {} if field != "storage_binding_digest" else "b" * 64
        return proof

    backend.change = wrong
    with pytest.raises(IntegrityError):
        runtime.start(batch["id"], batch["active"])
    assert store.get(batch["id"])["state"] == "registered"
    assert runtime.store.get(batch["id"])["state"] == "failed"
    assert "ready_recovered" not in [
        event["event"] for event in runtime.events(batch["id"])
    ]


def test_initial_adoption_is_verified_once_and_restart_preserves_later_edits(
    managed, tmp_path
):
    control, runtime, backend = managed
    batch = control.create("user", "Old data", "old")
    store, binding, batch = register_binding(control, batch, tmp_path)
    backend.change = lambda proof: {
        **proof,
        "storage_binding_digest": digest(binding),
        "adoption_graph": binding["graph"],
    }
    first = runtime.start(batch["id"], batch["active"])
    assert store.get(batch["id"])["state"] == "adopted"
    assert (
        backend.specs[first.pid]["startup"]["environment"]["WORKING_DIR"]
        == binding["working_directory"]
    )
    runtime.stop(batch["id"])
    # Later graph edits belong to the batch; its original graph is not restored.
    backend.change = lambda proof: {**proof, "storage_binding_digest": digest(binding)}
    again = runtime.start(batch["id"], batch["active"])
    assert again.generation == 2 and again.collections == first.collections
    assert store.profiles()["adopted-test-v1"]["model_name"] == "original-model"
    runtime.stop(batch["id"])


def test_binding_record_tampering_and_embedding_space_changes_are_refused(
    managed, tmp_path
):
    control, runtime, _ = managed
    batch = control.create("user", "Old data", "old")
    store, binding, batch = register_binding(control, batch, tmp_path)
    value = deepcopy(binding)
    value["embedding"]["dimensions"] = 4096
    with store.transaction() as db:
        db.execute(
            "UPDATE storage_bindings SET record=? WHERE batch=?",
            (json.dumps(value), batch["id"]),
        )
    with pytest.raises(IntegrityError, match="binding changed"):
        runtime.start(batch["id"], batch["active"])
    with store.transaction() as db:
        db.execute(
            "UPDATE storage_bindings SET record=?,digest=? WHERE batch=?",
            (json.dumps(value), digest(value), batch["id"]),
        )
    with pytest.raises(ValueError, match="Revision does not own"):
        runtime.start(batch["id"], batch["active"])


def test_completion_requires_the_same_owned_ready_generation(managed, tmp_path):
    control, runtime, backend = managed
    batch = control.create("user", "Old data", "old")
    store, binding, batch = register_binding(control, batch, tmp_path)
    backend.change = lambda proof: {
        **proof,
        "storage_binding_digest": digest(binding),
        "adoption_graph": binding["graph"],
    }
    lease = runtime.start(batch["id"], batch["active"])
    proof = backend.observe(runtime.store.get(batch["id"]))
    from lightrag.batch_control.storage import Conflict

    with pytest.raises(Conflict, match="matching ready generation"):
        store.complete(batch["id"], lease, {**proof, "pid": lease.pid + 1})
    runtime.stop(batch["id"])
    with pytest.raises(Conflict, match="matching ready generation"):
        store.complete(batch["id"], lease, proof)


def test_real_worker_reuses_existing_local_graph_and_documents(runtime_setup, tmp_path):
    import networkx as nx

    control, policy = runtime_setup
    batch = control.create("user", "Old local data", "old")
    node = {
        "id": "existing",
        "entity_type": "concept",
        "description": "Preserved evidence",
    }
    baseline = {
        "nodes": 1,
        "edges": 0,
        "nodes_sha256": rows_digest([node]),
        "edges_sha256": rows_digest([]),
    }
    store, binding, batch = register_binding(
        control, batch, tmp_path, graph=baseline, actual_local=True
    )
    folder = Path(binding["working_directory"]) / batch["workspace"]
    folder.mkdir(parents=True)
    graph = nx.Graph()
    graph.add_node("existing", entity_type="concept", description="Preserved evidence")
    nx.write_graphml(graph, folder / "graph_chunk_entity_relation.graphml")
    (folder / "kv_store_full_docs.json").write_text(
        json.dumps(
            {"doc-existing": {"content": "original text", "file_path": "original.md"}}
        )
    )
    runtime = RuntimeSupervisor(control, policy, actor="user")
    try:
        lease = runtime.start(batch["id"], batch["active"])
        proof = runtime.backend.observe(runtime.store.get(batch["id"]))
        assert proof["adoption_graph"] == baseline
        assert store.get(batch["id"])["state"] == "adopted"
        assert lease.collections == binding["collections"]
        assert (
            json.loads((folder / "kv_store_full_docs.json").read_text())[
                "doc-existing"
            ]["content"]
            == "original text"
        )
        evidence = {
            "schema_version": 1,
            "storage_mode": "local",
            "model_requests": 0,
            "lease": lease.model_dump(),
            "observation": {
                key: value for key, value in proof.items() if key != "capability"
            },
            "binding": binding,
            "document_content_preserved": True,
            "adoption_state": store.get(batch["id"])["state"],
        }
    finally:
        record = runtime.store.get(batch["id"])
        if record and runtime.backend.ownership(record) == "owned":
            runtime.stop(batch["id"])
    evidence["child_stopped"] = (
        runtime.backend.ownership(runtime.store.get(batch["id"])) == "gone"
    )
    assert evidence["child_stopped"]
    if os.getenv("LIGHTRAG_ADOPTION_VALIDATION_RECORD"):
        from lightrag.batch_control.runtime_process import atomic_json

        atomic_json(Path(os.environ["LIGHTRAG_ADOPTION_VALIDATION_RECORD"]), evidence)
