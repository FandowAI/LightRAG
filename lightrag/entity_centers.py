"""Rank read-only center candidates; keep peripheral evidence in the graph.

Business type is a reviewable proxy, not an automated business-value judgment.
See docs/design/EntityCenterCandidates.md for thresholds and exceptions.
"""

from collections import Counter
from dataclasses import asdict, dataclass
import re

from lightrag.entity_packages import refs
from lightrag.utils import make_relation_chunk_key


BUSINESS_TYPES = {
    "企业主体",
    "品牌",
    "组织与部门",
    "人物",
    "人物角色",
    "岗位与职责",
    "产品与品类",
    "客户与需求",
    "渠道与经营阵地",
    "供应商与合作方",
    "资源、系统与工具",
    "战略、目标与计划",
    "项目与重要事件",
    "指标与经营事实",
    "制度与业务规则",
    "流程与操作规范",
    "文化与管理理念",
    "管理机制",
    "业务知识与专业方法",
    "决策与经验案例",
    "问题与风险",
}
REVIEW_TYPES = {
    "企业主体",
    "品牌",
    "组织与部门",
    "业务知识与专业方法",
    "决策与经验案例",
    "管理机制",
    "战略、目标与计划",
}


@dataclass(frozen=True)
class CenterSettings:
    min_neighbors: int = 3
    min_documents: int = 2

    def validate(self):
        if not 1 <= self.min_neighbors <= 1000 or not 1 <= self.min_documents <= 100:
            raise ValueError("Invalid center candidate thresholds")


def _core_nodes(adjacency):
    """Return the non-leaf structural core; star hubs use the degree fallback."""
    from collections import deque

    remaining = {name: len(neighbors) for name, neighbors in adjacency.items()}
    queue = deque(name for name, degree in remaining.items() if degree < 2)
    removed = set(queue)
    while queue:
        name = queue.popleft()
        for other in adjacency[name]:
            if other not in removed:
                remaining[other] -= 1
                if remaining[other] < 2:
                    removed.add(other)
                    queue.append(other)
    return set(adjacency) - removed


def select_centers(nodes, edges, chunks, entity_links, relation_links, settings):
    """Use completed chunks supplied by the adapter; never filter graph evidence."""
    settings.validate()
    adjacency = {name: set() for name in nodes}
    evidence = {name: set() for name in nodes}
    edge_count = 0
    for name, row in nodes.items():
        evidence[name].update(
            cid for cid in entity_links.get(name, refs(row)) if cid in chunks
        )
    seen_edges = set()
    for edge in edges:
        a, b = edge["source"], edge["target"]
        if a not in nodes or b not in nodes or a == b:
            continue
        key = make_relation_chunk_key(a, b)
        ids = {cid for cid in relation_links.get(key, refs(edge)) if cid in chunks}
        if not ids:
            continue
        adjacency[a].add(b)
        adjacency[b].add(a)
        evidence[a].update(ids)
        evidence[b].update(ids)
        if key not in seen_edges:
            seen_edges.add(key)
            edge_count += 1
    core = _core_nodes(adjacency)
    rows = []
    for name, node in nodes.items():
        ids = evidence[name]
        docs = {
            chunks[cid]["full_doc_id"] for cid in ids if chunks[cid].get("full_doc_id")
        }
        kind = re.sub(r"^\s*\d{1,2}\s*", "", str(node.get("entity_type") or "")).strip()
        degree, doc_count = len(adjacency[name]), len(docs)
        review_flag = str(node.get("needs_review", "")).lower() == "true"
        company_hub = kind == "企业主体" and degree >= 20 and doc_count >= 5
        if not docs:
            tier = "unavailable"
        elif review_flag or company_hub:
            tier = "review"
        elif (
            degree >= settings.min_neighbors
            and doc_count >= settings.min_documents
            and kind in BUSINESS_TYPES
        ):
            tier = "recommended"
        elif (
            kind in REVIEW_TYPES
            or degree >= settings.min_neighbors
            or doc_count >= settings.min_documents
        ):
            tier = "review"
        else:
            tier = "background"
        reasons = []
        if degree >= settings.min_neighbors:
            reasons.append("rich_connections")
        if doc_count >= settings.min_documents:
            reasons.append("multiple_documents")
        if kind in BUSINESS_TYPES:
            reasons.append("business_type")
        if name in core:
            reasons.append("structural_core")
        elif degree >= settings.min_neighbors:
            reasons.append("star_hub")
        if (
            tier == "review"
            and kind in REVIEW_TYPES
            and not (review_flag or company_hub)
        ):
            reasons.append("valuable_type_review")
        if tier == "background":
            reasons.append("peripheral_context")
        if tier == "unavailable":
            reasons.append("no_completed_evidence")
        cautions = []
        if review_flag:
            cautions.append("identity_review")
        if company_hub:
            cautions.append("broad_company_hub")
        if docs and kind not in BUSINESS_TYPES:
            cautions.append("unknown_type")
        rows.append(
            {
                "name": name,
                "entity_type": kind or "未分类",
                "tier": tier,
                "neighbor_count": degree,
                "document_count": doc_count,
                "chunk_count": len(ids),
                "in_structural_core": name in core,
                "reasons": reasons,
                "cautions": cautions,
            }
        )
    order = {"recommended": 0, "review": 1, "background": 2, "unavailable": 3}
    rows.sort(
        key=lambda row: (
            order[row["tier"]],
            -row["document_count"],
            -row["neighbor_count"],
            -int(row["in_structural_core"]),
            row["name"],
        )
    )
    counts = Counter(row["tier"] for row in rows)
    return {
        "policy": {
            "version": "core-centers-v1",
            **asdict(settings),
            "evidence_scope": "processed_documents_only",
        },
        "summary": {
            "graph_entities": len(nodes),
            "supported_relations": edge_count,
            "processed_documents": len(
                {r["full_doc_id"] for r in chunks.values() if r.get("full_doc_id")}
            ),
            **{tier: counts[tier] for tier in order},
        },
        "candidates": rows,
    }


async def from_rag(rag, settings):
    """Read complete anchors and document states without LLM/vector API calls."""
    graph = rag.chunk_entity_relation_graph
    raw_nodes, edges = await graph.get_all_nodes(), await graph.get_all_edges()
    nodes = {
        n.get("id") or n.get("entity_name") or n.get("entity_id"): n for n in raw_nodes
    }
    if len(nodes) > 10000:
        raise ValueError(
            "Candidate review currently supports at most 10000 graph entities"
        )
    entity_rows = await rag.entity_chunks.get_by_ids(list(nodes))
    keys = [make_relation_chunk_key(e["source"], e["target"]) for e in edges]
    relation_rows = await rag.relation_chunks.get_by_ids(keys)
    entity_links = {
        name: row["chunk_ids"]
        for name, row in zip(nodes, entity_rows)
        if row is not None
    }
    relation_links = {
        key: row["chunk_ids"]
        for key, row in zip(keys, relation_rows)
        if row is not None
    }
    ids = list(
        dict.fromkeys(
            [
                *(
                    cid
                    for n, row in nodes.items()
                    for cid in entity_links.get(n, refs(row))
                ),
                *(
                    cid
                    for key, row in zip(keys, edges)
                    for cid in relation_links.get(key, refs(row))
                ),
            ]
        )
    )
    raw_chunks = await rag.text_chunks.get_by_ids(ids)
    chunks = {
        cid: row for cid, row in zip(ids, raw_chunks) if row and row.get("full_doc_id")
    }
    docs = list({r["full_doc_id"] for r in chunks.values()})
    states = await rag.doc_status.get_by_ids(docs)
    allowed = set()
    for doc, state in zip(docs, states):
        value = (
            state.get("status")
            if isinstance(state, dict)
            else getattr(state, "status", None)
        )
        if getattr(value, "value", value) == "processed":
            allowed.add(doc)
    eligible = {
        cid: row for cid, row in chunks.items() if row["full_doc_id"] in allowed
    }
    return select_centers(
        nodes, edges, eligible, entity_links, relation_links, settings
    )
