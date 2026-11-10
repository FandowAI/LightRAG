"""Real RAG construction preserves canonical stores and all future vector factories."""

# ruff: noqa: F811

from pathlib import Path

import pytest

from lightrag import LightRAG
from lightrag.batch_control.index_rebuild.binding import bind_vectors
from lightrag.batch_control.index_rebuild.projection import META_FIELDS
from lightrag.batch_control.storage import Conflict, IntegrityError
from .test_index_rebuild_builder import setup_target, shared_data  # noqa: F401
from .test_index_rebuild_projection import canonical, identity  # noqa: F401

pytestmark = pytest.mark.offline


async def no_llm(*args, **kwargs):
    raise AssertionError("Vector binding must not call a language model")


def setup_rag(tmp_path, identity, canonical):
    _, target, embed, stores = setup_target(tmp_path, identity, canonical)
    rag = LightRAG(
        working_dir=str(tmp_path / "rag"),
        input_dir=str(tmp_path / "inputs"),
        workspace=identity.workspace,
        embedding_func=stores["entities"].embedding_func,
        llm_model_func=no_llm,
    )
    return rag, target, embed


def test_binding_changes_only_vectors_and_covers_future_name_or_regular_construction(
    tmp_path, identity, canonical
):
    rag, target, embed = setup_rag(tmp_path, identity, canonical)
    originals = {
        key: getattr(rag, key)
        for key in (
            "full_docs",
            "text_chunks",
            "doc_status",
            "chunk_entity_relation_graph",
        )
    }
    replacements = bind_vectors(rag, target)
    assert all(getattr(rag, key) is value for key, value in originals.items())
    assert rag.workspace == identity.workspace
    assert {store.workspace for store in replacements.values()} == {
        target.vector_workspace
    }
    assert {
        role: store.final_namespace for role, store in replacements.items()
    } == target.collections
    later = rag.vector_db_storage_cls(
        namespace="chunks",
        workspace=rag.workspace,
        embedding_func=rag.embedding_func,
        meta_fields=META_FIELDS["chunks"],
    )
    assert later.final_namespace == target.collections["chunks"]
    assert embed.calls == 0
    with pytest.raises(Conflict):
        bind_vectors(rag, target)


async def test_loaded_old_vectors_cannot_be_rebound_and_pending_work_survives(
    tmp_path, identity, canonical
):
    rag, target, embed = setup_rag(tmp_path, identity, canonical)
    old = rag.entities_vdb
    await old.initialize()
    await old.upsert({"pending": {"content": "uncommitted original work"}})
    with pytest.raises(Conflict):
        bind_vectors(rag, target)
    assert rag.entities_vdb is old
    assert "pending" in old._pending_upserts
    assert embed.calls == 0
    await old.drop_pending_index_ops()


@pytest.mark.parametrize("change", ["workspace", "model", "missing_index"])
def test_inconsistent_worker_identity_is_rejected_before_mutating_vector_references(
    tmp_path, identity, canonical, change
):
    rag, target, embed = setup_rag(tmp_path, identity, canonical)
    old = rag.entities_vdb
    if change == "workspace":
        rag.workspace = "other_batch"
    elif change == "model":
        rag.embedding_func.model_name = "other-model"
    else:
        target = target.model_copy(
            update={
                "collections": {
                    **target.collections,
                    "name_entities": target.vector_workspace + "_name_entities",
                }
            }
        )
    with pytest.raises(IntegrityError):
        bind_vectors(rag, target)
    assert rag.entities_vdb is old and embed.calls == 0


def test_future_factory_rejects_cross_batch_and_unregistered_namespaces(
    tmp_path, identity, canonical
):
    rag, target, embed = setup_rag(tmp_path, identity, canonical)
    bind_vectors(rag, target)
    for workspace, role in (("other_batch", "chunks"), (rag.workspace, "unregistered")):
        with pytest.raises(IntegrityError):
            rag.vector_db_storage_cls(
                namespace=role,
                workspace=workspace,
                embedding_func=rag.embedding_func,
                meta_fields={"content"},
            )
    assert embed.calls == 0
    assert not (Path(rag.working_dir) / "other_batch").exists()
