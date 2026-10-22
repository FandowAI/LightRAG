"""One complete neighborhood per package, with conservative duplicate removal."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.entity_packages import PackageSettings, assemble, deduplicate, from_rag
from lightrag.utils import make_relation_chunk_key

pytestmark = pytest.mark.offline


def chunk(cid, text=None, title="source.md", doc="done"):
    return {
        "content": text or ("Evidence " + cid),
        "file_path": title,
        "full_doc_id": doc,
    }


def record(cid, text, title="source.md"):
    return {
        "chunk_id": cid,
        "content": text,
        "provenance": [{"file_path": title, "chunk_id": cid}],
    }


def approval(left, right):
    return {
        **{
            k: True
            for k in [
                "equivalent",
                "same_scope",
                "same_time",
                "same_conditions",
                "same_numbers_units",
                "same_conclusions",
                "no_unique_information",
            ]
        },
        "reason": "Same information",
        "quotes": {"left": left["content"], "right": right["content"]},
    }


def graph(pairs):
    names = list(dict.fromkeys(n for p in pairs for n in p))
    nodes = {n: {"id": n, "source_id": "c" + n} for n in names}
    chunks = {"c" + n: chunk("c" + n) for n in names}
    edges = [{"source": a, "target": b, "source_id": "c" + a} for a, b in pairs]
    return nodes, edges, chunks


@pytest.mark.asyncio
async def test_three_hops_form_one_package_with_all_entities():
    pairs = [
        ("a", "b"),
        ("b", "c"),
        *(("c", f"leaf{i:02}") for i in range(22)),
        ("leaf00", "outside"),
    ]
    nodes, edges, chunks = graph(pairs)
    result = await assemble(
        "a",
        nodes,
        edges,
        chunks,
        {},
        {},
        [],
        PackageSettings(graph_depth=3, near_dedup=False),
        len,
    )
    assert {e["name"] for e in result["entities"]} == {
        "a",
        "b",
        "c",
        *(f"leaf{i:02}" for i in range(22)),
    }
    assert len(result["packages"]) == 1
    assert len(result["packages"][0]["entities"]) == 25
    assert result["packages"][0]["entities"] == result["entities"]
    assert all(
        e["depth"] <= 3 and set(e["path"]) <= {n["name"] for n in p["entities"]}
        for p in result["packages"]
        for e in p["entities"]
    )
    seen = {
        s["chunk_id"]
        for p in result["packages"]
        for c in p["chunks"]
        for s in c["provenance"]
    }
    assert set(chunks) - {"coutside"} <= seen
    assert "coutside" not in seen


@pytest.mark.asyncio
async def test_rag_top_thirty_is_separate_from_graph_coverage():
    nodes, edges, chunks = graph([("a", "b")])
    chunks.update({f"v{i}": chunk(f"v{i}") for i in range(35)})
    result = await assemble(
        "a",
        nodes,
        edges,
        chunks,
        {},
        {},
        [{"id": f"v{i}"} for i in range(35)],
        PackageSettings(near_dedup=False),
        len,
    )
    seen = {
        s["chunk_id"]
        for p in result["packages"]
        for c in p["chunks"]
        for s in c["provenance"]
    }
    assert seen == {"ca", "cb", *(f"v{i}" for i in range(30))}
    assert result["summary"]["vector_candidates"] == 30


@pytest.mark.asyncio
async def test_center_evidence_is_not_all_repeated_for_every_entity_group():
    nodes, edges, chunks = graph([("a", f"leaf{i:02}") for i in range(24)])
    anchors = {"a": ["ca", *(f"background{i}" for i in range(20))]}
    chunks.update({f"background{i}": chunk(f"background{i}") for i in range(20)})
    result = await assemble(
        "a",
        nodes,
        edges,
        chunks,
        anchors,
        {},
        [],
        PackageSettings(near_dedup=False),
        len,
    )
    occurrences = [c["chunk_id"] for p in result["packages"] for c in p["chunks"]]
    assert all(occurrences.count(f"background{i}") == 1 for i in range(20))
    assert set(chunks) <= set(occurrences)
    assert {e["name"] for p in result["packages"] for e in p["entities"]} == set(nodes)


@pytest.mark.asyncio
async def test_cross_branch_relation_has_both_endpoints_and_paths_in_a_package():
    pairs = [("a", f"leaf{i:02}") for i in range(20)] + [("leaf00", "leaf19")]
    nodes, edges, chunks = graph(pairs)
    chunks["cross"] = chunk("cross")
    edges[-1]["source_id"] = "cross"
    result = await assemble(
        "a",
        nodes,
        edges,
        chunks,
        {},
        {make_relation_chunk_key("leaf00", "leaf19"): ["cross"]},
        [],
        PackageSettings(near_dedup=False),
        len,
    )
    matching = [
        p
        for p in result["packages"]
        if any(c["chunk_id"] == "cross" for c in p["chunks"])
    ]
    assert matching
    assert all(
        {"a", "leaf00", "leaf19"} <= {e["name"] for e in p["entities"]}
        for p in matching
    )


@pytest.mark.asyncio
async def test_long_chunk_is_never_split_or_truncated():
    nodes, edges, chunks = graph([("a", "b")])
    chunks["ca"]["content"] = "原始事实必须完整保留，不能因长度上限丢掉后半部分。" * 600
    result = await assemble(
        "a",
        nodes,
        edges,
        chunks,
        {},
        {},
        [],
        PackageSettings(near_dedup=False),
        len,
    )
    assert len(result["packages"]) == 1
    package = result["packages"][0]
    assert package["tokens"] > 12000
    assert package["length_notice"] == {
        "review_threshold": 12000,
        "exceeds_review_threshold": True,
        "was_truncated": False,
    }
    pieces = [
        c for p in result["packages"] for c in p["chunks"] if c["chunk_id"] == "ca"
    ]
    assert "".join(p["content"] for p in pieces) == chunks["ca"]["content"]
    assert len(pieces) == 1
    assert chunks["ca"]["content"] in package["distillation_input"]["text"]


@pytest.mark.asyncio
async def test_whole_package_dedup_preserves_all_entity_links_and_graph_relations():
    nodes, edges, chunks = graph([("a", "b"), ("b", "c"), ("c", "d")])
    chunks["cb"]["content"] = chunks["cd"]["content"] = (
        "同一条业务经验必须统一保留来源。"
    )
    result = await assemble(
        "a",
        nodes,
        edges,
        chunks,
        {},
        {},
        [{"id": "cb"}],
        PackageSettings(graph_depth=3),
        len,
    )
    package = result["packages"][0]
    assert len(result["packages"]) == 1
    assert result["summary"]["exact_merges"] == 1
    assert len(package["relationships"]) == 3
    assert package["text"].count(chunks["cb"]["content"]) == 1
    kept = next(c for c in package["chunks"] if c["chunk_id"] == "cb")
    assert {"b", "d"} <= set(kept["via_entities"])
    assert set(kept["origins"]) == {"entity", "relation", "vector"}
    assert {p["chunk_id"] for p in kept["provenance"]} == {"cb", "cd"}
    assert kept["variants"][0]["content"] == chunks["cd"]["content"]


@pytest.mark.asyncio
async def test_no_completed_evidence_produces_no_empty_package():
    result = await assemble(
        "a",
        {"a": {"id": "a"}},
        [],
        {},
        {},
        {},
        [],
        PackageSettings(),
        len,
    )
    assert result["packages"] == []
    assert result["summary"]["packages"] == 0


@pytest.mark.asyncio
async def test_equivalent_near_duplicates_merge_but_original_variants_remain():
    left = record("one", "公司应先完成流程梳理，再逐步推进信息化。" * 8)
    right = record("two", "公司应先完成流程梳理，再逐步推进信息化！" * 8)
    judge = AsyncMock(side_effect=approval)
    kept, audit = await deduplicate([left, right], PackageSettings(), judge)
    assert len(kept) == 1 and audit[0]["action"] == "merged"
    assert {p["chunk_id"] for p in kept[0]["provenance"]} == {"one", "two"}
    assert kept[0]["variants"][0]["content"] == right["content"]
    judge.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "left,right,left_title,right_title",
    [
        ("规格为24片，每盒独立包装。", "规格为10片，每盒独立包装。", "x", "x"),
        ("容量为25ml，适用于该产品。", "容量为25g，适用于该产品。", "x", "x"),
        ("公司建议推进信息化。", "公司不建议推进信息化。", "x", "x"),
        ("明年计划推进信息化。", "今年计划推进信息化。", "x", "x"),
        ("计划推进信息化。", "计划推进信息化。", "2021年计划.md", "2022年计划.md"),
    ],
)
async def test_numeric_units_negation_and_time_differences_are_protected(
    left, right, left_title, right_title
):
    judge = AsyncMock(side_effect=approval)
    kept, _ = await deduplicate(
        [record("one", left, left_title), record("two", right, right_title)],
        PackageSettings(),
        judge,
    )
    assert len(kept) == 2
    judge.assert_not_awaited()


@pytest.mark.asyncio
async def test_high_similarity_does_not_override_unique_information():
    left = record("one", "企业流程应围绕核心价值持续改进。" * 10)
    right = record("two", left["content"] + "需要额外审批。")
    judge = AsyncMock(
        return_value={"equivalent": False, "reason": "Extra approval requirement"}
    )
    kept, audit = await deduplicate([left, right], PackageSettings(), judge)
    assert len(kept) == 2 and audit[0]["action"] == "kept"


@pytest.mark.asyncio
async def test_judge_failure_keeps_both_and_reports_failure():
    left = record("one", "企业流程应围绕核心价值持续改进。" * 8)
    right = record("two", left["content"].replace("。", "！"))
    kept, audit = await deduplicate(
        [left, right],
        PackageSettings(),
        AsyncMock(side_effect=RuntimeError("provider")),
    )
    assert len(kept) == 2 and audit[0]["reason"] == "equivalence_check_failed"


class KV:
    def __init__(self, rows):
        self.rows = rows

    async def get_by_ids(self, keys):
        return [self.rows.get(k) for k in keys]


@pytest.mark.asyncio
async def test_live_adapter_uses_completed_documents_only_and_complete_anchor_chunks():
    nodes, edges, chunks = graph([("a", "b"), ("b", "failed-node")])
    chunks["cfailed-node"]["full_doc_id"] = "failed"
    edges[1]["source_id"] = "cfailed-node"
    chunks["extra"] = chunk("extra")
    rag = SimpleNamespace(
        chunk_entity_relation_graph=SimpleNamespace(
            get_all_nodes=AsyncMock(side_effect=AssertionError("No full graph scan")),
            get_all_edges=AsyncMock(side_effect=AssertionError("No full graph scan")),
            get_node=AsyncMock(side_effect=lambda name: nodes.get(name)),
            get_node_edges=AsyncMock(
                side_effect=lambda name: [
                    (e["source"], e["target"])
                    for e in edges
                    if name in (e["source"], e["target"])
                ]
            ),
            get_edge=AsyncMock(
                side_effect=lambda a, b: next(
                    (e for e in edges if {e["source"], e["target"]} == {a, b}), None
                )
            ),
        ),
        text_chunks=KV(chunks),
        doc_status=KV(
            {"done": {"status": "processed"}, "failed": {"status": "failed"}}
        ),
        entity_chunks=KV({"a": {"chunk_ids": ["ca", "extra"]}}),
        relation_chunks=KV({}),
        chunks_vdb=SimpleNamespace(
            query=AsyncMock(return_value=[{"id": "cfailed-node"}])
        ),
        tokenizer=SimpleNamespace(encode=list),
        role_llm_funcs={"extract": AsyncMock()},
    )
    result = await from_rag(rag, "a", PackageSettings(near_dedup=False))
    assert {e["name"] for e in result["entities"]} == {"a", "b"}
    ids = {
        s["chunk_id"]
        for p in result["packages"]
        for c in p["chunks"]
        for s in c["provenance"]
    }
    assert ids == {"ca", "cb", "extra"}
    rag.chunks_vdb.query.assert_awaited_once_with("a", top_k=30)
    rag.role_llm_funcs["extract"].assert_not_awaited()


@pytest.mark.parametrize(
    "settings",
    [
        PackageSettings(graph_depth=7),
        PackageSettings(rag_top_k=0),
        PackageSettings(near_threshold=0.1),
    ],
)
def test_invalid_settings(settings):
    with pytest.raises(ValueError):
        settings.validate()


@pytest.mark.asyncio
async def test_capped_graph_is_not_silently_published_as_complete():
    from unittest.mock import patch

    traversal = SimpleNamespace(is_truncated=True)
    with patch(
        "lightrag.operate._traverse_graph_from_entity",
        AsyncMock(return_value=traversal),
    ):
        with pytest.raises(ValueError, match="safety cap"):
            await from_rag(
                SimpleNamespace(chunk_entity_relation_graph=object()),
                "a",
                PackageSettings(),
            )


@pytest.mark.asyncio
async def test_batch_caps_do_not_change_interactive_graph_defaults(monkeypatch):
    from unittest.mock import patch
    from lightrag.operate import GRAPH_QUERY_MAX_NODES, GRAPH_QUERY_MAX_RELATIONS

    monkeypatch.setenv("LIGHTRAG_BATCH_MAX_GRAPH_NODES", "800")
    monkeypatch.setenv("LIGHTRAG_BATCH_MAX_GRAPH_RELATIONS", "2000")
    shared = AsyncMock(return_value=None)
    graph = object()
    with patch("lightrag.operate._traverse_graph_from_entity", shared):
        with pytest.raises(ValueError, match="Entity not found"):
            await from_rag(
                SimpleNamespace(chunk_entity_relation_graph=graph),
                "a",
                PackageSettings(),
            )
    shared.assert_awaited_once_with("a", 2, graph, max_nodes=800, max_relations=2000)
    assert (GRAPH_QUERY_MAX_NODES, GRAPH_QUERY_MAX_RELATIONS) == (100, 200)
