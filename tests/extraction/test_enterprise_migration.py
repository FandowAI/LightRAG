"""Enterprise migration preserves knowledge and never merges on similarity alone."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from lightrag.enterprise_identity import IdentityRegistry, evidence_valid
from lightrag.enterprise_knowledge import (
    metadata_for_graph,
    surviving_metadata,
    knowledge_context,
    remap_cached_fragments,
)
from lightrag.entity_merge import EntityMergeManager
from lightrag.operate import _process_json_extraction_result

pytestmark = pytest.mark.offline

CURRENT = "甲品牌运营部今年需要增加新客，计划先开展小范围测试。"
HISTORY = "甲品牌运营部去年负责线上经营，先测试再扩大投入。"


class Graph:
    def __init__(self, nodes=None):
        self.nodes = nodes or {}

    async def get_node(self, name):
        return self.nodes.get(name)

    async def has_node(self, name):
        return name in self.nodes

    async def upsert_node(self, name, node_data):
        self.nodes[name] = node_data

    async def get_all_nodes(self):
        raise AssertionError("Production identity checks must not scan the graph")


class Vectors:
    def __init__(self, **kwargs):
        self.index_config = SimpleNamespace(metric_type="COSINE")
        self.hits = []
        self.upserts = []

    async def query(self, *args, **kwargs):
        return self.hits

    async def upsert(self, records):
        self.upserts.append(records)

    async def index_done_callback(self):
        pass

    async def delete(self, ids):
        pass


class Chunks:
    def __init__(self, values):
        self.values = values

    async def get_by_ids(self, ids):
        return [self.values.get(key) for key in ids]


def node(name="运营部", source="new", scope=None):
    return {
        "entity_name": name,
        "entity_type": "组织与部门",
        "source_id": source,
        "description": CURRENT,
        "file_path": "meeting.md",
        "identity_scope": scope or {"brand": "甲品牌"},
    }


def judgment(decision="same", confidence=0.99):
    return json.dumps(
        {
            "decision": decision,
            "candidate": "运营部",
            "confidence": confidence,
            "reason": "Both original passages identify the same department and brand",
            "evidence": {
                "incoming": {"chunk_id": "new", "quote": CURRENT},
                "candidate": {"chunk_id": "old", "quote": HISTORY},
            },
        }
    )


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.delenv("ENTITY_IDENTITY_REGISTRY_FILE", raising=False)
    rag = SimpleNamespace(
        workspace="test",
        vector_db_storage_cls=Vectors,
        embedding_func=SimpleNamespace(model_name="test", embedding_dim=4),
        chunk_entity_relation_graph=Graph(
            {"运营部": {**node(source="old"), "description": HISTORY}}
        ),
        role_llm_funcs={"extract": AsyncMock(return_value=judgment())},
        text_chunks=Chunks({"new": {"content": CURRENT}, "old": {"content": HISTORY}}),
    )
    result = EntityMergeManager(rag, str(tmp_path), "test")
    result.name_vdb.hits = [
        {"entity_name": "运营部", "entity_type": "组织与部门", "distance": 0.99}
    ]
    return result


def results(name="运营中心", scope=None):
    return [
        (
            {name: [node(name, scope=scope)]},
            {("运营中心", "工具"): [{"description": "Uses a tool"}]},
        )
    ]


@pytest.mark.asyncio
async def test_similarity_only_proposes_and_original_evidence_authorizes_merge(manager):
    output, decisions = await manager.resolve(results())
    assert "运营部" in output[0][0]
    assert decisions[0]["reason"] == "identity_verified"
    assert ("工具", "运营部") in output[0][1]
    manager.rag.role_llm_funcs["extract"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["different", "related", "uncertain"])
async def test_non_same_keeps_original_objects_and_knowledge(manager, decision):
    manager.rag.role_llm_funcs["extract"].return_value = judgment(decision)
    data = results()
    data[0][0]["运营中心"][0]["knowledge_records"] = [
        {"内容": "先验证再扩张", "执行状态": "计划"}
    ]
    output, decisions = await manager.resolve(data)
    assert "运营中心" in output[0][0]
    assert (
        output[0][0]["运营中心"][0]["knowledge_records"]
        == data[0][0]["运营中心"][0]["knowledge_records"]
    )
    assert decisions[0]["reason"] == "separate_uncertain"


@pytest.mark.asyncio
async def test_different_brand_blocks_same_name_before_model(manager):
    data = [({"运营部": [node(scope={"brand": "乙品牌"})]}, {})]
    output, decisions = await manager.resolve(data)
    names = list(output[0][0])
    assert len(names) == 1 and names[0].startswith("运营部 [")
    assert decisions[0]["reason"] == "separate_scope"
    manager.rag.role_llm_funcs["extract"].assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_same_name_is_stable_and_preserves_both_entities(manager):
    manager.rag.role_llm_funcs["extract"].return_value = judgment("uncertain")
    data = [({"运营部": [node()]}, {})]
    a, _ = await manager.resolve(data)
    b, _ = await manager.resolve(data)
    assert list(a[0][0]) == list(b[0][0])
    assert "运营部" not in a[0][0]
    assert "运营部" in manager.rag.chunk_entity_relation_graph.nodes


@pytest.mark.asyncio
async def test_invalid_quote_and_high_confidence_cannot_merge(manager):
    value = json.loads(judgment())
    value["evidence"]["candidate"]["quote"] = (
        "This quote is not present in any original source"
    )
    manager.rag.role_llm_funcs["extract"].return_value = json.dumps(value)
    output, _ = await manager.resolve(results())
    assert "运营中心" in output[0][0]
    assert manager.rag.role_llm_funcs["extract"].await_count == 2


@pytest.mark.asyncio
async def test_second_check_can_confirm_a_low_confidence_first_same(manager):
    manager.rag.role_llm_funcs["extract"].side_effect = [
        judgment(confidence=0.8),
        judgment(),
    ]
    output, _ = await manager.resolve(results())
    assert "运营部" in output[0][0]
    assert manager.rag.role_llm_funcs["extract"].await_count == 2


@pytest.mark.asyncio
async def test_unavailable_model_keeps_content_and_does_not_invent_identity(manager):
    manager.rag.role_llm_funcs["extract"].side_effect = TimeoutError("offline")
    output, decisions = await manager.resolve(results())
    assert "运营中心" in output[0][0]
    assert "unavailable" in decisions[0]["judgment"]["reason"]


@pytest.mark.asyncio
async def test_cancelled_identity_propagates_cancellation(manager):
    manager.rag.role_llm_funcs["extract"].side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await manager.resolve(results())


@pytest.mark.asyncio
async def test_no_original_chunks_never_uses_model_summary_as_proof(manager):
    manager.rag.text_chunks.values.clear()
    output, _ = await manager.resolve(results())
    assert "运营中心" in output[0][0]
    manager.rag.role_llm_funcs["extract"].assert_not_awaited()


@pytest.mark.asyncio
async def test_same_source_batch_and_committed_aliases_have_bounded_lookup(manager):
    graph = manager.rag.chunk_entity_relation_graph
    graph.nodes["运营部"]["entity_resolution_aliases"] = json.dumps(
        [{"name": "运营中心", "source_id": "old", "entity_type": "组织与部门"}]
    )
    await manager.enterprise.sync_aliases(["运营部"])
    manager.name_vdb.hits = []
    output, _ = await manager.resolve(results())
    assert "运营部" in output[0][0]
    await manager.delete_names(["运营部"])
    assert manager.enterprise.aliases("运营中心", "组织与部门") == []


def registry_file(tmp_path, entries):
    path = tmp_path / "identities.private.json"
    path.write_text(
        json.dumps({"version": "test", "entries": entries}), encoding="utf-8"
    )
    return path


def entry(name="张测试", aliases=None, kind="人物", scope=None):
    return {
        "canonical_name": name,
        "entity_type": kind,
        "aliases": aliases or ["张经理"],
        "scope": scope or {},
        "verified": True,
        "evidence": [{"type": "explicit_confirmation"}],
    }


def test_unverified_registry_refuses_to_start(tmp_path):
    invalid = {**entry(), "verified": False}
    with pytest.raises(ValueError, match="unverified"):
        IdentityRegistry(str(registry_file(tmp_path, [invalid])))


def test_ambiguous_alias_does_not_resolve_without_scope(tmp_path):
    registry = IdentityRegistry(
        str(registry_file(tmp_path, [entry(), entry("张另人")]))
    )
    assert registry.match("张经理", {"entity_type": "人物"}) is None


def test_scope_can_disambiguate_a_confirmed_alias(tmp_path):
    registry = IdentityRegistry(
        str(
            registry_file(
                tmp_path,
                [
                    entry(scope={"company": "甲公司"}),
                    entry("张另人", scope={"company": "乙公司"}),
                ],
            )
        )
    )
    assert (
        registry.match(
            "张经理", {"entity_type": "人物", "identity_scope": {"company": "甲公司"}}
        )["canonical_name"]
        == "张测试"
    )


@pytest.mark.asyncio
async def test_verified_roster_renames_without_model_and_retains_historic_title(
    manager, tmp_path
):
    manager.enterprise.registry = IdentityRegistry(
        str(registry_file(tmp_path, [entry()]))
    )
    content = {
        "entity_name": "张经理",
        "entity_type": "人物",
        "source_id": "new",
        "description": "张经理当年提出试行计划",
    }
    output, decisions = await manager.resolve([({"张经理": [content]}, {})])
    assert output[0][0]["张测试"][0]["description"] == content["description"]
    assert decisions[0]["reason"] == "verified_registry"
    manager.rag.role_llm_funcs["extract"].assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_reports_candidates_instead_of_claiming_a_merge(manager):
    value = await manager.preview("运营中心", "组织与部门")
    assert value["decision"] == "candidate" and value["target"] is None
    manager.rag.role_llm_funcs["extract"].assert_not_awaited()


@pytest.mark.asyncio
async def test_json_parser_keeps_facts_but_program_owns_source_id():
    value = {
        "entities": [
            {
                "name": "甲品牌",
                "type": "品牌",
                "description": "甲品牌计划试行一个方案",
                "identity_scope": {"company": "甲公司"},
                "aliases": ["甲牌"],
                "knowledge_records": [
                    {"内容": "试行方案", "执行状态": "计划", "source_id": "fabricated"}
                ],
            }
        ],
        "relationships": [],
    }
    nodes, _ = await _process_json_extraction_result(
        json.dumps(value), "actual-chunk", 1, "source.md"
    )
    row = nodes["甲品牌"][0]
    assert row["source_id"] == "actual-chunk"
    assert row["knowledge_records"] == [{"内容": "试行方案", "执行状态": "计划"}]
    assert row["identity_scope"] == {"company": "甲公司"}


def test_records_survive_summary_and_deleted_sources_are_removed():
    a = {
        **node(source="a"),
        "knowledge_records": [{"内容": "过去的观点", "时间": "2020"}],
        "_resolution_aliases": [
            {"name": "旧称", "entity_type": "组织与部门", "scope": {}, "source_id": "a"}
        ],
    }
    b = {
        **node(source="b"),
        "knowledge_records": [{"内容": "后来的计划", "执行状态": "计划"}],
        "_resolution_aliases": [
            {"name": "新称", "entity_type": "组织与部门", "scope": {}, "source_id": "b"}
        ],
    }
    first = metadata_for_graph([a], None)
    combined = metadata_for_graph([b], first, ["a", "b"])
    assert len(json.loads(combined["enterprise_knowledge"])) == 2
    pruned = surviving_metadata(combined, ["b"])
    records = json.loads(pruned["enterprise_knowledge"])
    assert len(records) == 1 and records[0]["record"]["执行状态"] == "计划"
    assert json.loads(pruned["entity_resolution_aliases"])[0]["name"] == "新称"
    assert knowledge_context(pruned)["knowledge_records"] == records


@pytest.mark.asyncio
async def test_cached_purge_replays_committed_aliases_without_ai():
    graph = Graph(
        {
            "标准部门": {
                "entity_type": "组织与部门",
                "entity_resolution_aliases": json.dumps(
                    [
                        {
                            "name": "旧称",
                            "entity_type": "组织与部门",
                            "scope": {},
                            "source_id": "a",
                        }
                    ]
                ),
            }
        }
    )
    nodes = {
        "a": {
            "旧称": [
                {"entity_name": "旧称", "entity_type": "组织与部门", "source_id": "a"}
            ]
        }
    }
    edges = {"a": {("旧称", "工具"): [{"src_id": "旧称", "tgt_id": "工具"}]}}
    await remap_cached_fragments(nodes, edges, {"标准部门"}, graph)
    assert "标准部门" in nodes["a"]
    assert tuple(sorted(("标准部门", "工具"))) in edges["a"]


def test_versioned_profile_has_no_review_queue_and_keeps_field_adapter():
    root = Path(__file__).resolve().parents[2]
    profile = yaml.safe_load(
        (root / "prompts/entity_type/fando_enterprise.yml").read_text(encoding="utf-8")
    )
    guidance = profile["entity_types_guidance"]
    assert "knowledge_records" in guidance
    assert "needs_review" not in guidance
    assert "完整知识" in guidance and "事实记录" in guidance
    assert "不强行合并" in guidance


@pytest.mark.parametrize(
    "proof",
    [
        None,
        {},
        {"incoming": {"quote": "missing"}},
        {
            "incoming": {"quote": CURRENT, "chunk_id": "invented"},
            "candidate": {"quote": HISTORY, "chunk_id": "old"},
        },
    ],
)
def test_model_cannot_invent_original_source_evidence(proof):
    assert not evidence_valid(
        proof,
        [{"chunk_id": "new", "text": CURRENT}],
        [{"chunk_id": "old", "text": HISTORY}],
    )


@pytest.mark.asyncio
async def test_verified_alias_still_cannot_cross_explicit_company_scopes(
    manager, tmp_path
):
    manager.enterprise.registry = IdentityRegistry(
        str(registry_file(tmp_path, [entry()]))
    )
    manager.rag.chunk_entity_relation_graph.nodes["张测试"] = {
        "entity_type": "人物",
        "entity_resolution_scope": json.dumps({"company": "甲公司"}),
    }
    data = {
        "entity_type": "人物",
        "identity_scope": {"company": "乙公司"},
        "source_id": "new",
    }
    output, _ = await manager.resolve([({"张经理": [data]}, {})])
    assert "张测试" not in output[0][0]
    assert "张经理" in output[0][0]


@pytest.mark.asyncio
async def test_clearing_aliases_leaves_settings_and_registry_intact(manager):
    await manager.enterprise.sync_aliases(["运营部"])
    assert manager.enterprise.aliases("运营部", "组织与部门") == ["运营部"]
    manager.enterprise.clear_aliases()
    assert manager.enterprise.aliases("运营部", "组织与部门") == []
    assert manager.snapshot()["settings"]["enabled"]


@pytest.mark.asyncio
async def test_keep_summary_cap_does_not_discard_new_complete_knowledge():
    from lightrag.operate import _merge_nodes_then_upsert

    graph = Graph(
        {
            "运营部": {
                "entity_type": "组织与部门",
                "source_id": "old",
                "description": HISTORY,
            }
        }
    )
    tracking = SimpleNamespace(
        get_by_id=AsyncMock(return_value={"chunk_ids": ["old"], "count": 1}),
        upsert=AsyncMock(),
    )
    row = {
        **node(),
        "knowledge_records": [{"内容": "新方案仍是计划", "执行状态": "计划"}],
    }
    await _merge_nodes_then_upsert(
        "运营部",
        [row],
        graph,
        None,
        {"source_ids_limit_method": "KEEP", "max_source_ids_per_entity": 1},
        entity_chunks_storage=tracking,
    )
    actual = graph.nodes["运营部"]
    assert actual["description"] == HISTORY
    assert json.loads(actual["enterprise_knowledge"])[0]["source_id"] == "new"
    assert tracking.upsert.call_args.args[0]["运营部"]["chunk_ids"] == ["old", "new"]


@pytest.mark.asyncio
async def test_cancellation_after_identity_never_starts_graph_mutation(
    manager, monkeypatch
):
    from lightrag.exceptions import PipelineCancelledException
    import lightrag.operate as operate

    merge = AsyncMock()
    monkeypatch.setattr(operate, "merge_nodes_and_edges", merge)
    with pytest.raises(PipelineCancelledException):
        await manager.merge_unlocked(
            results(),
            pipeline_status={"cancellation_requested": True},
            pipeline_status_lock=asyncio.Lock(),
        )
    merge.assert_not_awaited()


def test_glossary_compiler_prefers_primary_names_and_rejects_pilot_hash_names():
    import importlib.util

    path = (
        Path(__file__).resolve().parents[2] / "scripts/compile_enterprise_registry.py"
    )
    spec = importlib.util.spec_from_file_location("compile_roster", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    glossary = {
        "version": "test",
        "source_workbook_sha256": "abc",
        "content_sha256": "def",
        "entities": [
            {
                "id": "p",
                "canonical": "张测试",
                "aliases": ["张经理"],
                "kind": "部门总监",
                "is_person": True,
                "source_cell": "A2",
            }
        ],
    }
    supplemental = {
        "entries": [entry("张经理", ["张经理"]), entry("李总 [1234567890]")]
    }
    compiled = module.compile_registry(glossary, supplemental)
    assert len(compiled["entries"]) == 1
    assert compiled["entries"][0]["canonical_name"] == "张测试"


def test_unscoped_glossary_alias_respects_corpus_boundary_without_assigning_employment(
    tmp_path,
):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "context_scope": {"company": "Company A"},
                "entries": [
                    {
                        "canonical_name": "Person A",
                        "entity_type": "人物",
                        "aliases": ["Manager A"],
                        "verified": True,
                        "evidence": ["confirmed glossary"],
                    }
                ],
            }
        )
    )
    registry = IdentityRegistry(str(path))
    assert (
        registry.match(
            "Manager A",
            {"entity_type": "人物", "identity_scope": {"company": "Company B"}},
        )
        is None
    )
    assert (
        registry.match(
            "Manager A",
            {"entity_type": "人物", "identity_scope": {"company": "Company A"}},
        )["canonical_name"]
        == "Person A"
    )
    unknown = {"entity_type": "人物"}
    assert registry.match("Manager A", unknown)["canonical_name"] == "Person A"
    assert "identity_scope" not in unknown
