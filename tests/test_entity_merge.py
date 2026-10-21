"""Name-only resolution must preserve graph anchors and type boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lightrag.entity_merge import (
    EntityMergeManager,
    EntityMergeSettings,
    StaleEntityMergeRevision,
)


class GraphStub:
    def __init__(self, nodes):
        self.nodes = nodes

    async def get_node(self, name):
        return self.nodes.get(name)

    async def has_node(self, name):
        return name in self.nodes


class VectorStub:
    def __init__(self, hits=None, **kwargs):
        self.hits = hits or []
        self.index_config = SimpleNamespace(metric_type="COSINE")
        self.query_calls = []
        self.upserts = []

    async def query(self, name, top_k, *, filter_expr):
        self.query_calls.append((name, top_k, filter_expr))
        return self.hits

    async def upsert(self, records):
        self.upserts.append(records)

    async def index_done_callback(self):
        pass


def manager_for(graph_nodes, hits):
    manager = EntityMergeManager.__new__(EntityMergeManager)
    manager.rag = SimpleNamespace(chunk_entity_relation_graph=GraphStub(graph_nodes))
    manager.name_vdb = VectorStub(hits)
    manager.snapshot = lambda: {
        "settings": {
            "enabled": True,
            "similarity_threshold": 0.90,
            "margin_threshold": 0.05,
            "top_k": 20,
        }
    }
    return manager


@pytest.mark.asyncio
async def test_name_only_resolution_rewrites_entity_and_relation_before_anchor():
    manager = manager_for(
        {"Douyin Store": {"entity_type": " organization "}},
        [
            {
                "entity_name": "Douyin Store",
                "entity_type": "organization",
                "distance": 0.96,
            }
        ],
    )
    results = [
        (
            {"抖音小店": [{"entity_name": "抖音小店", "entity_type": "organization"}]},
            {("抖音小店", "品牌"): [{"src_id": "抖音小店", "tgt_id": "品牌"}]},
        )
    ]
    resolved, decisions = await manager.resolve(results)
    assert list(resolved[0][0]) == ["Douyin Store"]
    assert resolved[0][0]["Douyin Store"][0]["entity_name"] == "Douyin Store"
    assert resolved[0][1][("Douyin Store", "品牌")][0]["src_id"] == "Douyin Store"
    assert decisions[0]["target"] == "Douyin Store"
    assert manager.name_vdb.query_calls[0][2] == 'entity_type == "organization"'


@pytest.mark.asyncio
async def test_resolution_drops_self_loops_and_coalesces_relation_pairs():
    manager = manager_for(
        {"Canonical": {"entity_type": "concept"}},
        [{"entity_name": "Canonical", "entity_type": "concept", "distance": 0.96}],
    )
    results = [
        (
            {"Alias": [{"entity_name": "Alias", "entity_type": "concept"}]},
            {
                ("Alias", "Canonical"): [{"src_id": "Alias", "tgt_id": "Canonical"}],
                ("Alias", "Other"): [{"src_id": "Alias", "tgt_id": "Other"}],
                ("Canonical", "Other"): [{"src_id": "Canonical", "tgt_id": "Other"}],
            },
        )
    ]
    resolved, _ = await manager.resolve(results)
    assert set(resolved[0][1]) == {("Canonical", "Other")}
    assert len(resolved[0][1][("Canonical", "Other")]) == 2


@pytest.mark.asyncio
async def test_type_mismatch_or_small_margin_does_not_merge():
    manager = manager_for(
        {"A4": {"entity_type": "concept"}, "A5": {"entity_type": "concept"}},
        [
            {"entity_name": "A4", "entity_type": "concept", "distance": 0.94},
            {"entity_name": "A5", "entity_type": "concept", "distance": 0.92},
        ],
    )
    candidates = await manager.candidates("新名称", "concept", EntityMergeSettings())
    assert manager._select(candidates, EntityMergeSettings()) is None
    manager.rag.chunk_entity_relation_graph.nodes["A4"]["entity_type"] = "person"
    candidates = await manager.candidates("新名称", "concept", EntityMergeSettings())
    assert [candidate["name"] for candidate in candidates] == ["A5"]


@pytest.mark.asyncio
async def test_new_names_in_same_batch_are_not_searched_as_candidates():
    manager = manager_for({}, [])
    results = [
        ({"一": [{"entity_type": "concept"}], "二": [{"entity_type": "concept"}]}, {})
    ]
    resolved, _ = await manager.resolve(results)
    assert set(resolved[0][0]) == {"一", "二"}
    assert len(manager.name_vdb.query_calls) == 2


def test_settings_are_persistent_and_revision_checked(tmp_path):
    rag = SimpleNamespace(
        workspace="test",
        embedding_func=SimpleNamespace(model_name="test-model", embedding_dim=4),
        vector_db_storage_cls=lambda **kwargs: VectorStub(),
    )
    manager = EntityMergeManager(rag, str(tmp_path), "test")
    assert manager.snapshot()["settings"]["similarity_threshold"] == 0.90
    saved = manager.save(0, EntityMergeSettings(similarity_threshold=0.93))
    assert saved["revision"] == 1
    assert (
        EntityMergeManager(rag, str(tmp_path), "test").snapshot()["settings"][
            "similarity_threshold"
        ]
        == 0.93
    )
    with pytest.raises(StaleEntityMergeRevision):
        manager.save(0, EntityMergeSettings())


def test_name_collection_has_explicit_type_filter_field():
    pytest.importorskip("pymilvus")
    from lightrag.kg.milvus_impl import MilvusVectorDBStorage

    storage = MilvusVectorDBStorage.__new__(MilvusVectorDBStorage)
    storage.namespace = "name_entities"
    storage.embedding_func = SimpleNamespace(embedding_dim=4096)
    schema = storage._create_schema_for_namespace()
    assert {field.name for field in schema.fields} >= {
        "entity_name",
        "entity_type",
        "content",
        "vector",
    }
    assert set(storage._get_required_fields_for_namespace()) >= {
        "entity_name",
        "entity_type",
        "content",
        "vector",
    }
