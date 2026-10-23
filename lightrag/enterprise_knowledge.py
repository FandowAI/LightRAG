"""Source-owned enterprise facts and identity mappings for graph reconstruction."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any


def json_value(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def identity_scope(node: dict) -> dict[str, str]:
    value = node.get("identity_scope")
    if not isinstance(value, dict):
        value = json_value(node.get("entity_resolution_scope"), {})
    if not isinstance(value, dict):
        return {}
    placeholders = {
        "unknown",
        "null",
        "none",
        "n/a",
        "未知",
        "未提供",
        "待确认",
        "待核实",
        "本公司",
        "公司",
    }
    return {
        str(key): value.strip()
        for key, value in value.items()
        if isinstance(value, str)
        and value.strip()
        and value.strip().casefold() not in placeholders
    }


def parsed_metadata(entity: dict) -> dict:
    """Accept optional JSON facts without trusting model-supplied source IDs."""
    result = {}
    scope = identity_scope(entity)
    if scope:
        result["identity_scope"] = scope
    aliases = entity.get("aliases")
    if isinstance(aliases, list):
        result["aliases"] = list(
            dict.fromkeys(
                a.strip() for a in aliases if isinstance(a, str) and a.strip()
            )
        )
    records = entity.get("knowledge_records")
    if isinstance(records, list):
        result["knowledge_records"] = [
            {
                key: value
                for key, value in record.items()
                if key not in {"source_id", "source_ids", "chunk_id", "doc_id"}
            }
            for record in records
            if isinstance(record, dict)
        ]
    return result


def alias_rows(node: dict) -> list[dict]:
    rows = json_value(node.get("entity_resolution_aliases"), [])
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _unique(rows: list[dict]) -> list[dict]:
    return list(
        {
            json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False): r
            for r in rows
        }.values()
    )


def metadata_for_graph(
    rows: list[dict], existing: dict | None, source_ids: list[str] | None = None
) -> dict:
    """Store complete facts separately from the lossy aggregate description."""
    existing = existing or {}
    if source_ids is not None:
        existing = {**existing, **surviving_metadata(existing, source_ids)}
    aliases = alias_rows(existing)
    records = json_value(existing.get("enterprise_knowledge"), [])
    records = records if isinstance(records, list) else []
    scope = identity_scope(existing)
    for row in rows:
        scope.update(identity_scope(row))
        source = row.get("source_id")
        aliases.extend(row.get("_resolution_aliases", []))
        if source:
            aliases.extend(
                {
                    "name": name,
                    "source_id": source,
                    "entity_type": row.get("entity_type"),
                    "scope": identity_scope(row),
                }
                for name in row.get("aliases", [])
                if isinstance(name, str) and name
            )
            records.extend(
                {"source_id": source, "record": record}
                for record in row.get("knowledge_records", [])
                if isinstance(record, dict)
            )
    result = {}
    if aliases or "entity_resolution_aliases" in existing:
        result["entity_resolution_aliases"] = json.dumps(
            _unique(aliases), ensure_ascii=False
        )
    if scope or "entity_resolution_scope" in existing:
        result["entity_resolution_scope"] = json.dumps(scope, ensure_ascii=False)
    if records or "enterprise_knowledge" in existing:
        result["enterprise_knowledge"] = json.dumps(
            _unique(records), ensure_ascii=False
        )
    return result


def surviving_metadata(node: dict, chunk_ids: list[str]) -> dict:
    """Remove facts and observed aliases whose owning chunks were deleted."""
    remaining = set(chunk_ids)
    result = {}
    for field in ("entity_resolution_aliases", "enterprise_knowledge"):
        if field not in node:
            continue
        rows = json_value(node[field], [])
        rows = rows if isinstance(rows, list) else []
        surviving = [
            r for r in rows if isinstance(r, dict) and r.get("source_id") in remaining
        ]
        result[field] = json.dumps(surviving, ensure_ascii=False)
        if field == "entity_resolution_aliases" and "entity_resolution_scope" in node:
            scope = {}
            for row in surviving:
                scope.update(
                    identity_scope(
                        {"identity_scope": row.get("canonical_scope", row.get("scope"))}
                    )
                )
            result["entity_resolution_scope"] = json.dumps(scope, ensure_ascii=False)
    return result


def knowledge_context(entity: dict) -> dict:
    """Expose source-owned facts to the existing token-budgeted query context."""
    records = json_value(entity.get("enterprise_knowledge"), [])
    if not isinstance(records, list) or not records:
        return {}
    return {
        "knowledge_records": [
            r for r in records if isinstance(r, dict) and r.get("source_id")
        ]
    }


async def remap_cached_fragments(
    chunk_entities, chunk_relations, names, graph, *, strict=True
):
    """Replay committed source-scoped names on purge; never call an identity model."""
    by_chunk = defaultdict(dict)
    errors = {}
    canonical_types = {}
    for target in sorted(names):
        try:
            node = await graph.get_node(target)
        except Exception as exc:
            if strict:
                raise
            errors[target] = exc
            continue
        if not node:
            continue
        canonical_types[target] = node.get("entity_type")
        for row in alias_rows(node):
            source, alias = row.get("source_id"), row.get("name")
            if source and alias:
                key = (
                    alias,
                    row.get("entity_type"),
                    json.dumps(row.get("scope", {}), sort_keys=True),
                )
                previous = by_chunk[source].get(key)
                if previous is not None and previous != target:
                    error = ValueError("Conflicting source-owned identity mapping")
                    if strict:
                        raise error
                    errors[target] = error
                    errors[previous] = error
                else:
                    by_chunk[source][key] = target
    for chunk_id, mapping in by_chunk.items():
        entities = chunk_entities.get(chunk_id, {})
        rewritten = defaultdict(list)
        endpoints = defaultdict(set)
        for name, rows in entities.items():
            for row in rows:
                key = (
                    name,
                    row.get("entity_type"),
                    json.dumps(identity_scope(row), sort_keys=True),
                )
                target = mapping.get(key, name)
                rewritten[target].append(
                    {
                        **row,
                        "entity_name": target,
                        "entity_type": canonical_types.get(target)
                        or row.get("entity_type"),
                    }
                )
                endpoints[name].add(target)
        chunk_entities[chunk_id] = dict(rewritten)
        edges = defaultdict(list)
        for (src, tgt), rows in chunk_relations.get(chunk_id, {}).items():
            left, right = endpoints.get(src, {src}), endpoints.get(tgt, {tgt})
            if len(left) != 1 or len(right) != 1:
                raise ValueError("Ambiguous cached relation identity during rebuild")
            src, tgt = next(iter(left)), next(iter(right))
            if src != tgt:
                edges[tuple(sorted((src, tgt)))].extend(
                    {**r, "src_id": src, "tgt_id": tgt} for r in rows
                )
        if chunk_id in chunk_relations:
            chunk_relations[chunk_id] = dict(edges)
    return errors
