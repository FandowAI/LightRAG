"""Optional name-only entity resolution for graph ingestion.

The name index is separate from the regular entity vector collection. A
candidate is always checked against the graph before it can be selected, so a
stale index entry cannot cause a merge after an interrupted deletion.
See ``docs/design/EntityNameMergeContract.md`` for the cross-store residue.
"""

from __future__ import annotations

import sqlite3
import json
import math
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from lightrag.kg.shared_storage import get_storage_keyed_lock
from lightrag.operate import (
    _normalize_and_validate_entity_type,
    collect_kg_merge_candidates,
)
from lightrag.utils import compute_mdhash_id, logger


@dataclass(frozen=True)
class EntityMergeSettings:
    enabled: bool = True
    similarity_threshold: float = 0.90
    margin_threshold: float = 0.05
    top_k: int = 20


class StaleEntityMergeRevision(ValueError):
    pass


class EntityMergeManager:
    def __init__(self, rag: Any, working_dir: str, workspace: str):
        self.rag = rag
        self.workspace = workspace or "default"
        self.path = Path(working_dir) / "entity_merge_settings.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.name_vdb = rag.vector_db_storage_cls(
            namespace="name_entities",
            workspace=rag.workspace,
            embedding_func=rag.embedding_func,
            meta_fields={"entity_name", "entity_type", "content"},
        )
        if self.name_vdb.index_config.metric_type.upper() != "COSINE":
            raise ValueError("Entity name resolution requires a COSINE Milvus index")
        # The query collection's 0.2 radius must not silently override the
        # threshold selected in the entity-merge settings.
        self.name_vdb.cosine_better_than_threshold = -1.0
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS entity_merge_settings ("
                "workspace TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                "enabled INTEGER NOT NULL, similarity_threshold REAL NOT NULL, "
                "margin_threshold REAL NOT NULL, top_k INTEGER NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    def snapshot(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM entity_merge_settings WHERE workspace = ?",
                (self.workspace,),
            ).fetchone()
        settings = (
            EntityMergeSettings(
                enabled=bool(row["enabled"]),
                similarity_threshold=row["similarity_threshold"],
                margin_threshold=row["margin_threshold"],
                top_k=row["top_k"],
            )
            if row
            else EntityMergeSettings()
        )
        return {
            "workspace": self.workspace,
            "revision": row["revision"] if row else 0,
            "settings": asdict(settings),
            "embedding_model": getattr(self.rag.embedding_func, "model_name", None),
            "embedding_dim": self.rag.embedding_func.embedding_dim,
        }

    def save(self, revision: int, settings: EntityMergeSettings) -> dict[str, Any]:
        if not 0 <= settings.similarity_threshold <= 1:
            raise ValueError("similarity_threshold must be between 0 and 1")
        if not 0 <= settings.margin_threshold <= 1:
            raise ValueError("margin_threshold must be between 0 and 1")
        if not 1 <= settings.top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM entity_merge_settings WHERE workspace = ?",
                (self.workspace,),
            ).fetchone()
            if (row["revision"] if row else 0) != revision:
                db.rollback()
                raise StaleEntityMergeRevision(
                    "Entity merge settings changed; reload and retry"
                )
            db.execute(
                "INSERT INTO entity_merge_settings VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace) DO UPDATE SET revision=excluded.revision, "
                "enabled=excluded.enabled, similarity_threshold=excluded.similarity_threshold, "
                "margin_threshold=excluded.margin_threshold, top_k=excluded.top_k",
                (
                    self.workspace,
                    revision + 1,
                    int(settings.enabled),
                    settings.similarity_threshold,
                    settings.margin_threshold,
                    settings.top_k,
                ),
            )
            db.commit()
        return self.snapshot()

    def lock(self):
        return get_storage_keyed_lock(
            "write", namespace=f"{self.workspace}:entity-name-resolution"
        )

    async def initialize(self) -> None:
        await self.name_vdb.initialize()

    async def finalize(self) -> None:
        await self.name_vdb.finalize()

    @staticmethod
    def _type(value: Any) -> str | None:
        if not isinstance(value, str) or value.strip().upper() == "UNKNOWN":
            return None
        return _normalize_and_validate_entity_type(value, "entity merge")

    async def candidates(
        self, name: str, entity_type: str, settings: EntityMergeSettings
    ) -> list[dict[str, Any]]:
        normalized_type = self._type(entity_type)
        if not normalized_type:
            return []
        # The type filter runs inside Milvus before Top K ranking. Filtering
        # only the returned hits could hide a same-type match behind 20 hits
        # of other types.
        hits = await self.name_vdb.query(
            name,
            top_k=settings.top_k,
            filter_expr=f"entity_type == {json.dumps(normalized_type, ensure_ascii=False)}",
        )
        candidates = []
        for hit in hits:
            candidate_name = hit.get("entity_name")
            if not candidate_name or candidate_name == name:
                continue
            if self._type(hit.get("entity_type")) != normalized_type:
                continue
            node = await self.rag.chunk_entity_relation_graph.get_node(candidate_name)
            if not node or self._type(node.get("entity_type")) != normalized_type:
                continue
            score = float(hit["distance"])
            if not math.isfinite(score):
                continue
            candidates.append(
                {
                    "name": candidate_name,
                    "type": normalized_type,
                    "score": score,
                }
            )
        return sorted(candidates, key=lambda item: item["score"], reverse=True)

    async def preview(self, name: str, entity_type: str) -> dict[str, Any]:
        settings = EntityMergeSettings(**self.snapshot()["settings"])
        if not name.strip() or not self._type(entity_type):
            raise ValueError("A non-empty name and valid entity type are required")
        existing = await self.rag.chunk_entity_relation_graph.get_node(name)
        candidates = (
            [] if existing else await self.candidates(name, entity_type, settings)
        )
        target = self._select(candidates, settings) if settings.enabled else None
        return {
            "existing_exact_name": bool(existing),
            "candidates": candidates,
            "decision": "exact_name" if existing else "merge" if target else "create",
            "target": target,
        }

    @staticmethod
    def _select(
        candidates: list[dict[str, Any]], settings: EntityMergeSettings
    ) -> str | None:
        if not candidates or candidates[0]["score"] < settings.similarity_threshold:
            return None
        if (
            len(candidates) > 1
            and (candidates[0]["score"] - candidates[1]["score"])
            < settings.margin_threshold
        ):
            return None
        return candidates[0]["name"]

    async def resolve(self, chunk_results: list) -> tuple[list, list[dict[str, Any]]]:
        """Resolve only against the committed name index, never this batch."""
        settings = EntityMergeSettings(**self.snapshot()["settings"])
        if not settings.enabled:
            return chunk_results, []
        types: dict[str, set[str | None]] = defaultdict(set)
        for nodes, _ in chunk_results:
            for name, records in nodes.items():
                types[name].update(
                    self._type(record.get("entity_type")) for record in records
                )
        mapping: dict[str, str] = {}
        decisions = []
        for name, record_types in types.items():
            if len(record_types) != 1 or None in record_types:
                decisions.append(
                    {"source": name, "target": None, "reason": "invalid_type"}
                )
                continue
            if await self.rag.chunk_entity_relation_graph.has_node(name):
                decisions.append(
                    {"source": name, "target": name, "reason": "exact_name"}
                )
                continue
            candidates = await self.candidates(name, next(iter(record_types)), settings)
            target = self._select(candidates, settings)
            if target:
                reason = "merged"
            elif not candidates:
                reason = "no_same_type_candidate"
            elif candidates[0]["score"] < settings.similarity_threshold:
                reason = "below_threshold"
            else:
                reason = "ambiguous_margin"
            decisions.append(
                {
                    "source": name,
                    "target": target,
                    "reason": reason,
                    "best_score": candidates[0]["score"] if candidates else None,
                }
            )
            if target:
                mapping[name] = target
        if not mapping:
            return chunk_results, decisions
        rewritten = []
        for nodes, edges in chunk_results:
            new_nodes: dict[str, list] = defaultdict(list)
            new_edges: dict[tuple[str, str], list] = defaultdict(list)
            for name, records in nodes.items():
                canonical = mapping.get(name, name)
                for record in records:
                    new_nodes[canonical].append({**record, "entity_name": canonical})
            for (source, target), records in edges.items():
                canonical_source = mapping.get(source, source)
                canonical_target = mapping.get(target, target)
                if canonical_source == canonical_target:
                    continue
                key = tuple(sorted((canonical_source, canonical_target)))
                for record in records:
                    new_edges[key].append(
                        {
                            **record,
                            "src_id": canonical_source,
                            "tgt_id": canonical_target,
                        }
                    )
            rewritten.append((dict(new_nodes), dict(new_edges)))
        logger.info("Entity name resolution mapped %d extracted names", len(mapping))
        return rewritten, decisions

    async def sync_candidates(self, chunk_results: list) -> None:
        """Publish names after graph writes; absent nodes never enter the index."""
        names, _ = collect_kg_merge_candidates(chunk_results)
        records = {}
        for name in names:
            node = await self.rag.chunk_entity_relation_graph.get_node(name)
            entity_type = self._type(node.get("entity_type")) if node else None
            if entity_type:
                records[compute_mdhash_id(name, prefix="ent-name-")] = {
                    "content": name,
                    "entity_name": name,
                    "entity_type": entity_type,
                }
        if records:
            await self.name_vdb.upsert(records)
            await self.name_vdb.index_done_callback()

    async def merge(
        self, chunk_results: list, *, already_resolved: bool = False, **kwargs: Any
    ) -> dict[str, int]:
        async with self.lock():
            return await self.merge_unlocked(
                chunk_results, already_resolved=already_resolved, **kwargs
            )

    async def merge_unlocked(
        self, chunk_results: list, *, already_resolved: bool = False, **kwargs: Any
    ) -> dict[str, int]:
        """Caller must hold ``lock()`` through its write-ahead journal."""
        from lightrag.operate import merge_nodes_and_edges

        resolved = chunk_results
        decisions: list[dict[str, Any]] = []
        if not already_resolved:
            resolved, decisions = await self.resolve(chunk_results)
        await merge_nodes_and_edges(chunk_results=resolved, **kwargs)
        await self.sync_candidates(resolved)
        return {
            reason: sum(item["reason"] == reason for item in decisions)
            for reason in (
                "merged",
                "exact_name",
                "no_same_type_candidate",
                "below_threshold",
                "ambiguous_margin",
                "invalid_type",
            )
        }

    async def delete_names(self, names: set[str] | list[str]) -> None:
        if names:
            await self.name_vdb.delete(
                [compute_mdhash_id(name, prefix="ent-name-") for name in names]
            )
            await self.name_vdb.index_done_callback()
