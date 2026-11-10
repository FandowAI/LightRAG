"""Verify actual canonical stores and persisted vectors before candidate readiness.

Registry/process observations in this module are explicit fakes. Vector building,
reopening, canonical audits and candidate comparisons use real local storages.
"""

# ruff: noqa: F811

from copy import deepcopy
import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from lightrag import LightRAG
from lightrag.batch_control.adoption_snapshot import vector_stores
from lightrag.batch_control.index_rebuild.audit import (
    KV_STORES,
    read_audit,
    snapshot_state,
    verify_state,
)
from lightrag.batch_control.index_rebuild.binding import bind_vectors
from lightrag.batch_control.index_rebuild.builder import build_target
from lightrag.batch_control.index_rebuild.projection import META_FIELDS
from lightrag.batch_control.index_rebuild.source import SourceLimits, write_source
from lightrag.batch_control.index_rebuild.verification import (
    compiled_transform,
    content_transform,
    verify_candidate,
    verify_restoration,
)
from lightrag.batch_control.index_rebuild.worker import operation_directory
from lightrag.batch_control.runtime_process import atomic_json
from lightrag.batch_control.storage import IntegrityError
from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage
from lightrag.utils import EmbeddingFunc
from .test_applications import setup  # noqa: F401
from .test_index_rebuild_binding import no_llm
from .test_index_rebuild_builder import shared_data  # noqa: F401
from .test_index_rebuild_projection import canonical, identity  # noqa: F401
from .test_index_rebuild_store import advance, rebuild, runtime_row  # noqa: F401

pytestmark = pytest.mark.offline


async def embeddings(texts, **kwargs):
    return np.array(
        [[len(text) + i + 1 for i in range(16)] for text in texts], dtype=np.float32
    )


def new_rag(tmp_path, identity):
    return LightRAG(
        working_dir=str(tmp_path / "rag"),
        input_dir=str(tmp_path / "inputs"),
        workspace=identity.workspace,
        llm_model_func=no_llm,
        embedding_func=EmbeddingFunc(
            16, embeddings, model_name="new-offline-embedding"
        ),
    )


@pytest.fixture
async def source_rag(tmp_path, identity, canonical):
    rag = new_rag(tmp_path, identity)
    await rag.initialize_storages()
    # The fixture is the sole writer. Persist attribution before graph changes.
    for name in KV_STORES:
        values = {"doc-a": {"source": name, "status": "processed"}}
        if name == "text_chunks":
            values = {
                row["id"]: {key: value for key, value in row.items() if key != "id"}
                for row in canonical["chunks"]
            }
        await getattr(rag, name).upsert(deepcopy(values))
        await getattr(rag, name).index_done_callback()
    graph = rag.chunk_entity_relation_graph
    for row in canonical["nodes"]:
        await graph.upsert_node(
            row["id"], {key: value for key, value in row.items() if key != "id"}
        )
    for row in canonical["edges"]:
        await graph.upsert_edge(
            row["source"],
            row["target"],
            {
                key: value
                for key, value in row.items()
                if key not in {"source", "target"}
            },
        )
    assert await graph.index_done_callback() is not False
    try:
        yield rag
    finally:
        await rag.finalize_storages()


async def test_audit_preserves_real_rows_and_checks_every_canonical_store(
    source_rag, identity
):
    audit, rows = await snapshot_state(source_rag, identity)
    assert set(audit.kv) == set(KV_STORES)
    assert all(value.rows == 1 for value in audit.kv.values())
    assert audit.graph.nodes == 2 and audit.graph.edges == 1
    assert rows["chunks"][0]["id"] == "chunk-a"
    assert await verify_state(source_rag, audit) == rows
    with pytest.raises(ValueError, match="row limit"):
        await snapshot_state(source_rag, identity, limits=SourceLimits(max_rows=3))


async def test_manager_transform_matches_actual_server_token_contract(source_rag):
    config = source_rag.entities_vdb.global_config
    config["embedding_token_limit"] = 3
    compiled = compiled_transform({"environment": {"EMBEDDING_TOKEN_LIMIT": "3"}})
    actual = content_transform(source_rag)
    text = "A long synthetic source with several distinct tokens 甲部门管理乙项目。"
    for role in ("entities", "relationships", "chunks", "name_entities"):
        assert compiled(role, "example", text) == actual(role, "example", text)
        if role in {"entities", "relationships"}:
            assert len(config["tokenizer"].encode(compiled(role, "example", text))) <= 3
        else:
            assert compiled(role, "example", text) == text


@pytest.mark.parametrize("name", KV_STORES)
async def test_equal_count_canonical_change_is_rejected(source_rag, identity, name):
    audit, _ = await snapshot_state(source_rag, identity)
    store = getattr(source_rag, name)
    key = "chunk-a" if name == "text_chunks" else "doc-a"
    row = await store.get_by_id(key)
    await store.upsert({key: {**row, "changed": "same number of records"}})
    with pytest.raises(IntegrityError, match="state changed"):
        await verify_state(source_rag, audit)


async def test_equal_count_graph_change_and_audit_redirection_are_rejected(
    source_rag, identity, tmp_path
):
    audit, _ = await snapshot_state(source_rag, identity)
    path = tmp_path / "audit.json"
    atomic_json(path, audit.model_dump())
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_audit(path, identity, checksum) == audit
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(IntegrityError, match="redirected"):
        read_audit(alias, identity, checksum)
    atomic_json(path, {**audit.model_dump(), "schema_version": 2})
    with pytest.raises(IntegrityError, match="changed"):
        read_audit(path, identity, checksum)
    await source_rag.chunk_entity_relation_graph.upsert_node(
        "甲部门", {"description": "changed content"}
    )
    with pytest.raises(IntegrityError, match="state changed"):
        await verify_state(source_rag, audit)


@pytest.fixture
def identity(rebuild):
    return rebuild.source


@pytest.fixture
async def verified_case(rebuild, source_rag, tmp_path):
    case = rebuild
    root = operation_directory(
        case.control.paths, case.control.registry, case.batch["id"], case.operation
    )
    audit, rows = await snapshot_state(source_rag, case.source)
    write_source(root / "source", case.source, **rows)
    atomic_json(root / "source" / "audit.json", audit.model_dump())
    evidence = {
        key: hashlib.sha256((root / "source" / filename).read_bytes()).hexdigest()
        for key, filename in (
            ("manifest_sha256", "source.json"),
            ("audit_sha256", "audit.json"),
        )
    }
    advance(case, "source_capturing")
    case.store.finish_capture(
        case.batch["id"],
        case.operation,
        case.source,
        evidence,
        case.source_lease.pid,
        case.source_lease.process_identity,
    )
    target = case.binding.target(case.source, evidence["manifest_sha256"])
    stores = {
        role: NanoVectorDBStorage(
            namespace=role,
            workspace=target.vector_workspace,
            global_config=source_rag.entities_vdb.global_config,
            embedding_func=source_rag.embedding_func,
            meta_fields=META_FIELDS[role],
        )
        for role in target.collections
    }
    receipt = await build_target(
        root / "source",
        root / "build",
        target,
        stores,
        transform=content_transform(source_rag),
    )
    advance(case, "stopping_source")
    advance(case, "builder_starting")
    builder = runtime_row(
        case, case.revision["id"], purpose="index_build", state="built"
    )
    advance(case, "building", builder_lease=builder)
    case.store.verify_build(
        case.batch["id"],
        case.operation,
        receipt,
        hashlib.sha256((root / "build" / "verified.json").read_bytes()).hexdigest(),
        disposition="owned",
    )
    advance(case, "candidate_starting")
    candidate = new_rag(tmp_path, case.source)
    bind_vectors(candidate, target)
    await candidate.initialize_storages()
    attempt = tmp_path / "candidate-attempt"
    attempt.mkdir()
    spec = {
        "data_batch_id": case.batch["id"],
        "application_id": case.operation,
        "config_revision_id": case.revision["id"],
        "attempt_directory": str(attempt),
    }
    try:
        yield SimpleNamespace(
            case=case, rag=candidate, root=root, spec=spec, audit=audit
        )
    finally:
        await candidate.finalize_storages()


async def test_candidate_reopens_complete_target_and_attests_canonical_state(
    verified_case,
):
    v = verified_case
    index = v.case.store.binding(v.case.batch["id"], v.case.operation)
    proof = await verify_candidate(
        v.rag, v.spec, index, v.case.control.registry, v.case.control.paths
    )
    assert proof == {
        "binding_digest": index["digest"],
        "receipt_sha256": index["evidence"]["receipt_sha256"],
        "source_audit_sha256": v.case.store.get(v.case.batch["id"], v.case.operation)[
            "source_evidence"
        ]["audit_sha256"],
    }
    advance(v.case, "rollback_stopping")
    advance(v.case, "rollback_starting")
    restored = await verify_restoration(
        v.rag,
        {
            **v.spec,
            "rebuild_rollback_id": v.case.operation,
            "config_revision_id": v.case.batch["active"],
        },
        v.case.control.registry,
        v.case.control.paths,
    )
    assert restored == proof["source_audit_sha256"]


@pytest.mark.parametrize(
    "corruption", ["canonical", "vectors", "audit", "receipt", "omitted_role"]
)
async def test_changed_candidate_never_receives_initial_attestation(
    verified_case, corruption, monkeypatch
):
    v = verified_case
    index = v.case.store.binding(v.case.batch["id"], v.case.operation)
    if corruption == "canonical":
        await v.rag.full_docs.upsert(
            {"doc-a": {"status": "processed", "content": "different original document"}}
        )
    elif corruption == "vectors":
        row = (await v.rag.entities_vdb.client_storage)["data"][0]
        await v.rag.entities_vdb.upsert(
            {row["__id__"]: {**row, "content": "different persisted content"}}
        )
        await v.rag.entities_vdb.index_done_callback()
    elif corruption == "audit":
        (v.root / "source" / "audit.json").write_text("{}")
    elif corruption == "receipt":
        (v.root / "build" / "verified.json").write_text("{}")
    else:
        stores = vector_stores(v.rag)
        stores.pop("chunks")
        monkeypatch.setattr(
            "lightrag.batch_control.index_rebuild.verification.vector_stores",
            lambda rag: stores,
        )
    with pytest.raises((IntegrityError, ValueError)):
        await verify_candidate(
            v.rag, v.spec, index, v.case.control.registry, v.case.control.paths
        )
    assert (
        v.case.control.registry.get(v.case.batch["id"])["active"]
        == v.case.batch["active"]
    )
    assert (
        v.case.store.binding(v.case.batch["id"], v.case.operation)["state"]
        == "verified"
    )
