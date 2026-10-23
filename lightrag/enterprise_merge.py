"""Enterprise decisions inside the server's existing serialized merge window."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import hashlib
import json

from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.enterprise_identity import (
    IdentityRegistry,
    compatible,
    judge_identity,
    normalize_name,
)
from lightrag.enterprise_knowledge import alias_rows, identity_scope


class EnterpriseResolver:
    """Use indexed aliases and the name VDB, then verify identity before anchors."""

    def __init__(self, manager):
        self.manager = manager
        self.rag = manager.rag
        self.registry = IdentityRegistry()
        with closing(manager._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS enterprise_aliases (
                workspace TEXT NOT NULL, alias TEXT NOT NULL, entity_name TEXT NOT NULL,
                entity_type TEXT NOT NULL, PRIMARY KEY(workspace, alias, entity_name))""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS enterprise_alias_type ON enterprise_aliases(workspace, alias, entity_type)"
            )

    def aliases(self, name: str, kind: str) -> list[str]:
        with closing(self.manager._connect()) as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT entity_name FROM enterprise_aliases WHERE workspace=? AND alias=? AND entity_type=? LIMIT 32",
                    (self.manager.workspace, normalize_name(name), kind),
                )
            ]

    def delete_aliases(self, names) -> None:
        with closing(self.manager._connect()) as db, db:
            db.executemany(
                "DELETE FROM enterprise_aliases WHERE workspace=? AND entity_name=?",
                ((self.manager.workspace, name) for name in names),
            )

    def clear_aliases(self) -> None:
        with closing(self.manager._connect()) as db, db:
            db.execute(
                "DELETE FROM enterprise_aliases WHERE workspace=?",
                (self.manager.workspace,),
            )

    async def sync_aliases(self, names) -> None:
        records = []
        for name in names:
            node = await self.rag.chunk_entity_relation_graph.get_node(name)
            if not node:
                continue
            kind = self.manager._type(node.get("entity_type"))
            if not kind:
                continue
            aliases = {
                name,
                *(
                    r["name"]
                    for r in alias_rows(node)
                    if isinstance(r.get("name"), str)
                ),
            }
            records.extend(
                (self.manager.workspace, normalize_name(alias), name, kind)
                for alias in aliases
            )
        with closing(self.manager._connect()) as db, db:
            db.executemany(
                "DELETE FROM enterprise_aliases WHERE workspace=? AND entity_name=?",
                ((self.manager.workspace, name) for name in names),
            )
            db.executemany(
                "INSERT OR REPLACE INTO enterprise_aliases VALUES (?,?,?,?)", records
            )

    @staticmethod
    def _separate_name(name: str, key: tuple, rows: list[dict]) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [key, sorted({str(r.get("source_id", "")) for r in rows})],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        suffix = f" [{digest}]"
        prefix = name.encode()[: 512 - len(suffix.encode())].decode(errors="ignore")
        return prefix + suffix

    async def resolve(self, results: list, settings) -> tuple[list, list[dict]]:
        graph = self.rag.chunk_entity_relation_graph
        groups = defaultdict(list)
        for nodes, _ in results:
            for name, rows in nodes.items():
                for row in rows:
                    key = (
                        name,
                        self.manager._type(row.get("entity_type")),
                        json.dumps(identity_scope(row), sort_keys=True),
                    )
                    groups[key].append(row)
        mapping, provisional, decisions = {}, {}, []
        graph_cache = {}

        async def get_node(name):
            if name in provisional:
                return provisional[name]
            if name not in graph_cache:
                value = await graph.get_node(name)
                graph_cache[name] = (
                    {**value, "identity_scope": self.registry.normalize_scope(value)}
                    if value
                    else None
                )
            return graph_cache[name]

        for key, rows in groups.items():
            name, kind, _ = key
            node = {
                **rows[0],
                "entity_type": kind,
                "source_id": GRAPH_FIELD_SEP.join(
                    dict.fromkeys(str(r.get("source_id", "")) for r in rows)
                ),
                "description": "\n".join(
                    dict.fromkeys(r.get("description", "") for r in rows)
                ),
            }
            node["identity_scope"] = self.registry.normalize_scope(node)
            registered = self.registry.match(name, node)
            target, target_kind = None, kind
            reason, judgment = "new", None
            if registered:
                canonical = registered["canonical_name"]
                existing = await get_node(canonical)
                expected_scope = self.registry.normalize_scope(
                    {"identity_scope": registered.get("scope", {})}
                )
                expected = {"identity_scope": expected_scope}
                registry_kind = (
                    kind
                    if registered["entity_type"] == "*"
                    else registered["entity_type"]
                )
                valid_types = {
                    registry_kind,
                    *registered.get("accepted_observed_types", []),
                }
                if not existing or (
                    compatible(existing, expected)
                    and compatible(existing, node)
                    and existing.get("entity_type") in valid_types
                ):
                    target, target_kind = canonical, registry_kind
                    node["identity_scope"] = {**expected_scope, **identity_scope(node)}
                    reason = "verified_registry"
            if target is None and kind and settings.enabled:
                candidates = {}
                exact = await get_node(name)
                labels = {
                    name,
                    *(
                        a
                        for r in rows
                        for a in r.get("aliases", [])
                        if isinstance(a, str)
                    ),
                }
                for label in labels:
                    for candidate_name in self.aliases(label, kind):
                        candidate = await get_node(candidate_name)
                        if (
                            candidate
                            and self.manager._type(candidate.get("entity_type")) == kind
                        ):
                            candidates[candidate_name] = {
                                "name": candidate_name,
                                "node": candidate,
                                "score": 1.0,
                            }
                if exact and self.manager._type(exact.get("entity_type")) == kind:
                    candidates[name] = {"name": name, "node": exact, "score": 1.0}
                for candidate_name, candidate in provisional.items():
                    observed = {candidate_name, *candidate.get("aliases", [])}
                    if (
                        kind == self.manager._type(candidate.get("entity_type"))
                        and labels & observed
                    ):
                        candidates[candidate_name] = {
                            "name": candidate_name,
                            "node": candidate,
                            "score": 1.0,
                        }
                for hit in await self.manager.candidates(name, kind, settings):
                    if hit["score"] < settings.similarity_threshold:
                        continue
                    candidate = await get_node(hit["name"])
                    if candidate:
                        candidates.setdefault(hit["name"], {**hit, "node": candidate})
                accepted = [
                    c for c in candidates.values() if compatible(node, c["node"])
                ]
                if accepted:
                    target, judgment = await judge_identity(
                        self.rag,
                        name,
                        node,
                        sorted(accepted, key=lambda c: (-c["score"], c["name"])),
                    )
                    reason = "identity_verified" if target else "separate_uncertain"
                elif candidates:
                    reason = "separate_scope"
            if target is None:
                target = name
                if await get_node(target):
                    target = self._separate_name(name, key, rows)
                    existing = await get_node(target)
                    if existing and not compatible(existing, node):
                        raise ValueError("Deterministic separate identity collision")
            mapping[key] = (target, target_kind, identity_scope(node))
            previous = provisional.get(target, {})
            provisional[target] = {
                **node,
                "entity_name": target,
                "entity_type": target_kind,
                "source_id": GRAPH_FIELD_SEP.join(
                    dict.fromkeys(
                        (
                            str(previous.get("source_id", ""))
                            + GRAPH_FIELD_SEP
                            + node["source_id"]
                        ).split(GRAPH_FIELD_SEP)
                    )
                ),
            }
            decisions.append(
                {
                    "source": name,
                    "target": target,
                    "reason": reason,
                    "judgment": judgment,
                    "registry_sha256": self.registry.digest,
                }
            )

        rewritten = []
        for nodes, edges in results:
            output, relations, endpoints = (
                defaultdict(list),
                defaultdict(list),
                defaultdict(set),
            )
            for name, rows in nodes.items():
                for row in rows:
                    key = (
                        name,
                        self.manager._type(row.get("entity_type")),
                        json.dumps(identity_scope(row), sort_keys=True),
                    )
                    target, kind, scope = mapping[key]
                    fragment = {
                        **row,
                        "entity_name": target,
                        "entity_type": kind,
                        "identity_scope": scope,
                        "_resolution_aliases": [
                            {
                                "name": name,
                                "source_id": row.get("source_id"),
                                "entity_type": row.get("entity_type"),
                                "scope": identity_scope(row),
                                "canonical_scope": scope,
                            }
                        ],
                    }
                    output[target].append(fragment)
                    endpoints[name].add(target)
            for (src, tgt), rows in edges.items():
                left, right = endpoints.get(src, {src}), endpoints.get(tgt, {tgt})
                if len(left) != 1 or len(right) != 1:
                    # Preserve the whole source for retry, rather than attaching
                    # a relation to one arbitrarily selected same-name object.
                    raise ValueError(
                        "A relation has ambiguous same-name identity endpoints"
                    )
                src, tgt = next(iter(left)), next(iter(right))
                if src != tgt:
                    relations[tuple(sorted((src, tgt)))].extend(
                        {**r, "src_id": src, "tgt_id": tgt} for r in rows
                    )
            rewritten.append((dict(output), dict(relations)))
        return rewritten, decisions
