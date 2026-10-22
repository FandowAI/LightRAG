"""Centers need supported relationships and distinct documents, not layout."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.entity_centers import CenterSettings, from_rag, select_centers
from lightrag.utils import make_relation_chunk_key

pytestmark = pytest.mark.offline


def dataset(documents=2, leaves=3, kind="组织与部门"):
    nodes = {"center": {"entity_type": kind, "source_id": "c0"}}
    nodes.update(
        {
            f"leaf{i}": {"entity_type": "问题与风险", "source_id": "c0"}
            for i in range(leaves)
        }
    )
    chunks = {
        f"c{i}": {"content": "Evidence", "full_doc_id": f"doc{i}"}
        for i in range(documents)
    }
    links = {"center": list(chunks)}
    edges = [
        {"source": "center", "target": f"leaf{i}", "source_id": "c0"}
        for i in range(leaves)
    ]
    return nodes, edges, chunks, links


def run(data, **kwargs):
    nodes, edges, chunks, links = data
    return select_centers(nodes, edges, chunks, links, {}, CenterSettings(**kwargs))


def test_star_hub_is_recommended_without_cyclic_core_and_leaves_remain_context():
    data = dataset()
    before = deepcopy(data)
    result = run(data)
    row = result["candidates"][0]
    assert row["name"] == "center" and row["tier"] == "recommended"
    assert row["document_count"] == 2 and row["neighbor_count"] == 3
    assert row["in_structural_core"] is False and "star_hub" in row["reasons"]
    assert result["summary"]["background"] == 3
    assert data == before


def test_many_chunks_in_one_document_do_not_fake_cross_document_support():
    data = dataset()
    data[2]["c1"]["full_doc_id"] = "doc0"
    row = next(r for r in run(data)["candidates"] if r["name"] == "center")
    assert row["document_count"] == 1 and row["tier"] == "review"


def test_duplicate_edges_and_self_loops_do_not_inflate_neighbor_count():
    data = dataset(leaves=1)
    data[1].extend(
        [data[1][0].copy(), {"source": "center", "target": "center", "source_id": "c0"}]
    )
    row = next(r for r in run(data)["candidates"] if r["name"] == "center")
    assert row["neighbor_count"] == 1 and row["tier"] == "review"


def test_sparse_decision_case_stays_available_for_manual_review():
    data = dataset(documents=1, leaves=0, kind="决策与经验案例")
    row = run(data)["candidates"][0]
    assert row["tier"] == "review" and "valuable_type_review" in row["reasons"]


def test_company_wide_hub_requires_review_instead_of_winning_by_degree():
    data = dataset(documents=5, leaves=20, kind="企业主体")
    row = run(data)["candidates"][0]
    assert row["name"] == "center" and row["tier"] == "review"
    assert "broad_company_hub" in row["cautions"]


@pytest.mark.parametrize(
    "kind", ["品牌", "18业务知识与专业方法", "资源、系统与工具", "人物"]
)
def test_business_domains_are_not_a_department_allowlist(kind):
    assert run(dataset(kind=kind))["candidates"][0]["tier"] == "recommended"


def test_rich_unknown_type_is_reviewed_and_explicit_identity_uncertainty_wins():
    row = run(dataset(kind="table"))["candidates"][0]
    assert row["tier"] == "review" and "unknown_type" in row["cautions"]
    data = dataset()
    data[0]["center"]["needs_review"] = "true"
    row = run(data)["candidates"][0]
    assert row["tier"] == "review" and "identity_review" in row["cautions"]


def test_thresholds_change_recommendations_and_core_is_not_geometric_position():
    data = dataset(leaves=2)
    data[1].append({"source": "leaf0", "target": "leaf1", "source_id": "c0"})
    data[0]["center"].update(x=999999, y=-999999)
    row = next(
        r for r in run(data, min_neighbors=2)["candidates"] if r["name"] == "center"
    )
    assert row["tier"] == "recommended" and row["in_structural_core"]
    assert (
        next(r for r in run(data)["candidates"] if r["name"] == "center")["tier"]
        == "review"
    )


class KV:
    def __init__(self, rows):
        self.rows = rows

    async def get_by_ids(self, ids):
        return [self.rows.get(key) for key in ids]


@pytest.mark.asyncio
async def test_adapter_uses_complete_tracking_anchors_and_only_processed_docs_without_models():
    nodes, edges, chunks, _ = dataset(documents=2, leaves=1)
    nodes["center"]["id"] = "center"
    nodes["leaf0"]["id"] = "leaf0"
    chunks["c0"]["full_doc_id"] = "failed"
    rag = SimpleNamespace(
        chunk_entity_relation_graph=SimpleNamespace(
            get_all_nodes=AsyncMock(return_value=list(nodes.values())),
            get_all_edges=AsyncMock(return_value=edges),
        ),
        entity_chunks=KV({"center": {"chunk_ids": ["c0", "c1"]}}),
        relation_chunks=KV(
            {make_relation_chunk_key("center", "leaf0"): {"chunk_ids": ["c1"]}}
        ),
        text_chunks=KV(chunks),
        doc_status=KV(
            {"failed": {"status": "failed"}, "doc1": {"status": "processed"}}
        ),
    )
    result = await from_rag(rag, CenterSettings())
    row = next(r for r in result["candidates"] if r["name"] == "center")
    assert row["document_count"] == 1 and row["chunk_count"] == 1
    assert row["neighbor_count"] == 1 and result["summary"]["processed_documents"] == 1


@pytest.mark.parametrize(
    "settings", [CenterSettings(0, 2), CenterSettings(3, 0), CenterSettings(1001, 2)]
)
def test_invalid_thresholds(settings):
    with pytest.raises(ValueError):
        settings.validate()
