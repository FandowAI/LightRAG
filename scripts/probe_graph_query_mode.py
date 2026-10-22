#!/usr/bin/env python3
"""Probe the exact-entity graph query API and validate subgraph invariants.

The script uses only the Python standard library. It exercises both the single
``/query/data`` endpoint and the ordered ``/query/data/batch`` endpoint without
calling the answer LLM.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ENTITIES = ["商标续展", "宽展期", "总经办"]


def post_json(base_url: str, path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {path}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach {base_url}: {exc.reason}") from exc


def canonical_edge(src_id: str, tgt_id: str) -> tuple[str, str]:
    return tuple(sorted((src_id, tgt_id)))


def analyze_graph_response(
    requested_entity: str,
    depth: int,
    response: dict[str, Any],
    *,
    allow_relationless_after_dedup: bool = False,
) -> tuple[list[str], list[str], dict[str, Any]]:
    failures: list[str] = []
    warnings: list[str] = []
    data = response.get("data") or {}
    metadata = response.get("metadata") or {}
    entities = data.get("entities") or []
    relationships = data.get("relationships") or []
    chunks = data.get("chunks") or []
    names = {
        entity.get("entity_name")
        for entity in entities
        if entity.get("entity_name")
    }
    root = metadata.get("root_entity") or requested_entity

    if response.get("status") != "success":
        failures.append(
            f"request status is {response.get('status')!r}: {response.get('message', '')}"
        )
    if metadata.get("query_mode") != "graph":
        failures.append(f"query_mode is {metadata.get('query_mode')!r}, expected 'graph'")
    if root not in names:
        failures.append(f"root entity {root!r} is missing from returned entities")

    adjacency: dict[str, set[str]] = {name: set() for name in names}
    orphan_edges: list[tuple[str, str]] = []
    for relationship in relationships:
        src_id = relationship.get("src_id", "")
        tgt_id = relationship.get("tgt_id", "")
        if src_id not in names or tgt_id not in names:
            orphan_edges.append((src_id, tgt_id))
            continue
        adjacency[src_id].add(tgt_id)
        adjacency[tgt_id].add(src_id)
    if orphan_edges:
        failures.append(f"relationships have missing endpoints: {orphan_edges}")

    reachable: set[str] = set()
    if root in adjacency:
        queue: deque[str] = deque([root])
        reachable.add(root)
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current] - reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    disconnected = sorted(names - reachable)
    if disconnected:
        failures.append(
            f"{len(disconnected)} returned entities are disconnected from the root: "
            f"{disconnected[:8]}{' ...' if len(disconnected) > 8 else ''}"
        )

    candidate_relationships = metadata.get("candidate_relationships", 0)
    deduplicated_relationships = metadata.get("deduplicated_relationships") or []
    skipped_disconnected_relationships = (
        metadata.get("skipped_disconnected_relationships") or []
    )
    all_relations_were_intentionally_removed = (
        allow_relationless_after_dedup
        and candidate_relationships > 0
        and len(deduplicated_relationships)
        + len(skipped_disconnected_relationships)
        >= candidate_relationships
    )
    if (
        depth > 0
        and candidate_relationships
        and not relationships
        and not all_relations_were_intentionally_removed
    ):
        failures.append(
            f"all {candidate_relationships} candidate relationships were removed"
        )
    if not chunks:
        failures.append("no evidence chunks were returned")
    if metadata.get("is_truncated"):
        warnings.append("result was truncated by graph or token limits")

    counts = {
        "candidate_nodes": metadata.get("candidate_nodes"),
        "candidate_relationships": candidate_relationships,
        "returned_nodes": len(entities),
        "returned_relationships": len(relationships),
        "returned_chunks": len(chunks),
        "deduplicated_relationships": len(deduplicated_relationships),
        "skipped_disconnected_relationships": len(
            skipped_disconnected_relationships
        ),
        "is_truncated": bool(metadata.get("is_truncated")),
    }
    return failures, warnings, counts


def parse_depths(value: str) -> list[int]:
    try:
        depths = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("depths must be comma-separated integers") from exc
    if not depths or any(depth < 0 or depth > 5 for depth in depths):
        raise argparse.ArgumentTypeError("every depth must be between 0 and 5")
    return depths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9622")
    parser.add_argument("--entities", nargs="+", default=DEFAULT_ENTITIES)
    parser.add_argument("--depths", type=parse_depths, default=[0, 1, 2])
    parser.add_argument("--batch-depth", type=int, choices=range(0, 6), default=2)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the complete JSON responses and validation report.",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    report: dict[str, Any] = {"single": [], "batch": {}}
    failure_count = 0

    print(f"Graph query probe: {args.base_url}")
    print(f"Entities: {', '.join(args.entities)}")
    print()
    print("SINGLE-ENTITY CHECKS")
    print("entity | depth | candidates(n/e) | returned(n/e/c) | truncated | result")
    print("-" * 92)

    for entity in args.entities:
        for depth in args.depths:
            payload = {
                "query": entity,
                "mode": "graph",
                "graph_depth": depth,
                "max_entity_tokens": 6000,
                "max_relation_tokens": 8000,
                "max_total_tokens": 30000,
            }
            try:
                response = post_json(args.base_url, "/query/data", payload, args.timeout)
                failures, warnings, counts = analyze_graph_response(
                    entity, depth, response
                )
            except RuntimeError as exc:
                failures, warnings, counts = [str(exc)], [], {}
                response = {}

            failure_count += len(failures)
            result_label = "PASS" if not failures else "FAIL"
            print(
                f"{entity} | {depth} | "
                f"{counts.get('candidate_nodes', '-')}/{counts.get('candidate_relationships', '-')} | "
                f"{counts.get('returned_nodes', '-')}/{counts.get('returned_relationships', '-')}/"
                f"{counts.get('returned_chunks', '-')} | "
                f"{counts.get('is_truncated', '-')} | {result_label}"
            )
            for failure in failures:
                print(f"  FAIL: {failure}")
            for warning in warnings:
                print(f"  WARN: {warning}")
            report["single"].append(
                {
                    "entity": entity,
                    "depth": depth,
                    "counts": counts,
                    "failures": failures,
                    "warnings": warnings,
                    "response": response,
                }
            )

    print()
    print(f"BATCH CHECK (depth={args.batch_depth}, processed-root path pruning)")
    batch_payload = {
        "entities": args.entities,
        "mode": "graph",
        "graph_depth": args.batch_depth,
        "deduplicate_relations": True,
        "deduplicate_chunks_in_batch": False,
    }
    batch_failures: list[str] = []
    try:
        batch_response = post_json(
            args.base_url, "/query/data/batch", batch_payload, args.timeout
        )
        if batch_response.get("status") not in {"success", "partial_success"}:
            batch_failures.append(
                f"batch status is {batch_response.get('status')!r}"
            )
        result_entities = [item.get("entity") for item in batch_response.get("results", [])]
        if result_entities != args.entities:
            batch_failures.append(
                f"result order {result_entities!r} does not match input order"
            )

        processed_roots: set[str] = set()
        for item in batch_response.get("results", []):
            entity = item.get("entity", "")
            item_response = item.get("data") or {}
            failures, _, counts = analyze_graph_response(
                entity,
                args.batch_depth,
                item_response,
                allow_relationless_after_dedup=True,
            )
            batch_failures.extend(f"{entity}: {failure}" for failure in failures)
            relationships = (item_response.get("data") or {}).get("relationships") or []
            for relationship in relationships:
                src_id = relationship.get("src_id", "")
                tgt_id = relationship.get("tgt_id", "")
                blocked_endpoints = {src_id, tgt_id} & processed_roots
                if blocked_endpoints:
                    batch_failures.append(
                        f"relationship {(src_id, tgt_id)!r} enters an earlier root "
                        f"{sorted(blocked_endpoints)!r}"
                    )
            processed_roots.add(entity)
            print(
                f"  {entity}: nodes={counts.get('returned_nodes', '-')} "
                f"relations={counts.get('returned_relationships', '-')} "
                f"chunks={counts.get('returned_chunks', '-')} "
                f"deduplicated={counts.get('deduplicated_relationships', '-')} "
                f"path-skipped={counts.get('skipped_disconnected_relationships', '-')}"
            )
    except RuntimeError as exc:
        batch_response = {}
        batch_failures.append(str(exc))

    failure_count += len(batch_failures)
    for failure in batch_failures:
        print(f"  FAIL: {failure}")
    print(f"Batch result: {'PASS' if not batch_failures else 'FAIL'}")
    report["batch"] = {
        "depth": args.batch_depth,
        "failures": batch_failures,
        "response": batch_response,
    }

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Full report: {args.output.resolve()}")

    print()
    print(f"OVERALL: {'PASS' if failure_count == 0 else 'FAIL'} ({failure_count} issue(s))")
    return 0 if failure_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
