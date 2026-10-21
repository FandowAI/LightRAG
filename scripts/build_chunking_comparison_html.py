"""Build a self-contained HTML viewer for the two-document chunking experiment."""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from lightrag.utils import normalize_entity_name


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "analysis" / "chunking_comparison_20261019"
TEMPLATE = ROOT / "scripts" / "templates" / "chunking_comparison.html"
OUTPUT = RESULTS / "index.html"


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def build_graph(run: dict, entity_rows: list[dict], responses: dict) -> dict:
    """Derive a graph preview from the extracted names and relationships."""
    nodes = {
        row["name"]: {
            "id": row["name"],
            "type": row["type"],
            "description": row["description"],
            "chunks": set(row["chunks"]),
            "inferred": False,
        }
        for row in entity_rows
    }
    edges: dict[tuple[str, str], dict] = {}
    for index, chunk in enumerate(run["chunks"], 1):
        parsed = responses[chunk["extraction_key"]].get("parsed") or {}
        for raw in parsed.get("relationships", []):
            if not isinstance(raw, dict):
                continue
            source = normalize_entity_name(str(raw.get("source") or ""))
            target = normalize_entity_name(str(raw.get("target") or ""))
            description = str(raw.get("description") or "").strip()
            if not source or not target or source == target or not description:
                continue
            for name in (source, target):
                if name not in nodes:
                    nodes[name] = {
                        "id": name,
                        "type": "Other",
                        "description": "此名称仅见于关系端点，未出现在已汇总的实体列表中。",
                        "chunks": set(),
                        "inferred": True,
                    }
                nodes[name]["chunks"].add(index)
            pair = tuple(sorted((source, target)))
            if pair not in edges:
                edges[pair] = {
                    "source": pair[0],
                    "target": pair[1],
                    "description": description,
                    "keywords": str(raw.get("keywords") or ""),
                    "chunks": set(),
                    "mentions": 0,
                    "evidence": [],
                }
            edges[pair]["chunks"].add(index)
            edges[pair]["mentions"] += 1
            edges[pair]["evidence"].append(
                {
                    "chunk": index,
                    "description": description,
                    "keywords": str(raw.get("keywords") or ""),
                }
            )

    network = nx.Graph()
    network.add_nodes_from(sorted(nodes))
    network.add_edges_from(sorted(edges))
    positions = nx.spring_layout(network, seed=42, iterations=120)
    for name, node in nodes.items():
        x, y = positions[name]
        node["x"] = round(float(x), 5)
        node["y"] = round(float(y), 5)
        node["chunks"] = sorted(node["chunks"])
    for edge in edges.values():
        edge["chunks"] = sorted(edge["chunks"])
    return {
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": sorted(edges.values(), key=lambda item: (item["source"], item["target"])),
    }


def main() -> None:
    docs = []
    runs = {}
    entities = {}
    for doc_index, label in ((1, "原版"), (2, "标题修复版")):
        source = ROOT / "data" / (
            "20250620高管茶话会.md"
            if doc_index == 1
            else "20250620高管茶话会标题修复版.md"
        )
        docs.append(
            {
                "id": f"doc{doc_index}",
                "label": label,
                "filename": source.name,
                "source": source.read_text(encoding="utf-8-sig"),
            }
        )
        for strategy in "FRVP":
            key = f"doc{doc_index}_{strategy}"
            runs[key] = load(RESULTS / "runs" / f"{key}.json")
            entities[key] = load(RESULTS / "entities" / f"{label if doc_index == 1 else '修复版'}-{strategy}.json")

    responses = {}
    for run in runs.values():
        for chunk in run["chunks"]:
            key = chunk["extraction_key"]
            if key not in responses:
                responses[key] = load(RESULTS / "responses" / f"{key}.json")

    graphs = {
        key: build_graph(run, entities[key], responses)
        for key, run in runs.items()
    }

    data = {
        "docs": docs,
        "runs": runs,
        "entities": entities,
        "responses": responses,
        "graphs": graphs,
        "summary": load(RESULTS / "summary.json"),
        "method": {
            "chunk_size": 1200,
            "overlap": 100,
            "notes": "实体和关系均来自 LightRAG JSON 初次抽取；图谱是这些结果的可视化预览，并非 Neo4j 数据库中的最终图。未做图谱描述合并或二次补抽。",
        },
    }
    encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    encoded = encoded.replace("</", "<\\/")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count("__DATA_JSON__") != 1:
        raise ValueError("HTML template must have exactly one data placeholder")
    OUTPUT.write_text(template.replace("__DATA_JSON__", encoded), encoding="utf-8")
    print(f"Created {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
