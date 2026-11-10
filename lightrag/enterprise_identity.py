"""Verified alias registry and explicit identity-scope compatibility rules."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re

from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.enterprise_knowledge import identity_scope


def enabled() -> bool:
    return os.getenv("ENTERPRISE_KNOWLEDGE_ENABLED", "false").lower() in {
        "true",
        "1",
        "yes",
    }


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def compatible(left: dict, right: dict) -> bool:
    a, b = identity_scope(left), identity_scope(right)
    return not any(a[key] != b[key] for key in a.keys() & b.keys())


class IdentityRegistry:
    """A frozen, versioned private roster; ambiguous aliases never become facts."""

    def __init__(self, path: str | None = None):
        self.entries = []
        self.index = defaultdict(list)
        self.digest = "none"
        self.version = None
        self.context_scope = {}
        path = path or os.getenv("ENTITY_IDENTITY_REGISTRY_FILE")
        if not path:
            return
        raw = Path(path).read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        value = json.loads(raw)
        self.version = value.get("version")
        self.context_scope = identity_scope(
            {"identity_scope": value.get("context_scope")}
        )
        entries = value.get("entries")
        if not isinstance(entries, list):
            raise ValueError("Identity registry must contain entries")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or entry.get("verified") is not True
                or not isinstance(entry.get("canonical_name"), str)
                or not entry["canonical_name"].strip()
                or not entry.get("entity_type")
                or not entry.get("evidence")
            ):
                raise ValueError(
                    "Identity registry contains an unverified or invalid entry"
                )
            self.entries.append(entry)
            for name in {entry["canonical_name"], *entry.get("aliases", [])}:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("Identity registry alias must be nonempty text")
                self.index[normalize_name(name)].append(entry)

    def match(self, name: str, node: dict) -> dict | None:
        matches = []
        for entry in self.index.get(normalize_name(name), []):
            if entry["entity_type"] != "*" and node.get("entity_type") not in {
                entry["entity_type"],
                *entry.get("accepted_observed_types", []),
            }:
                continue
            scope = self.normalize_scope(node)
            for field, aliases in entry.get("scope_aliases", {}).items():
                scope[field] = aliases.get(scope.get(field), scope.get(field))
                if scope[field] is None:
                    scope.pop(field)
            projected = {"identity_scope": scope}
            expected_scope = self.normalize_scope(
                {"identity_scope": entry.get("scope", {})}
            )
            if not compatible(projected, {"identity_scope": expected_scope}):
                continue
            # A glossary's corpus boundary does not assert anyone's employment.
            # An explicit entry scope overrides that default boundary.
            context = self.normalize_scope({"identity_scope": self.context_scope})
            if any(
                scope[k] != v
                for k, v in context.items()
                if k in scope and k not in expected_scope
            ):
                continue
            if entry.get("closed_scope") and any(
                k not in entry.get("scope", {}) for k in scope
            ):
                continue
            paths = set(str(node.get("file_path", "")).split(GRAPH_FIELD_SEP)) - {""}
            if entry.get("source_files") and (
                not paths or not paths <= set(entry["source_files"])
            ):
                continue
            text = node.get("description", "")
            required = entry.get("description_requires_any", [])
            if required and not any(term in text for term in required):
                continue
            if any(term in text for term in entry.get("description_excludes", [])):
                continue
            matches.append(entry)
        identities = {(e["canonical_name"], e["entity_type"]) for e in matches}
        return matches[0] if len(identities) == 1 else None

    def normalize_scope(self, node: dict) -> dict:
        scope = identity_scope(node)
        kinds = {"company": "企业主体", "brand": "品牌", "department": "组织与部门"}
        for field, kind in kinds.items():
            if field not in scope:
                continue
            matches = {
                e["canonical_name"]
                for e in self.index.get(normalize_name(scope[field]), [])
                if e["entity_type"] == kind
            }
            if len(matches) == 1:
                scope[field] = next(iter(matches))
        return scope

    def public_snapshot(self) -> dict:
        return {
            "version": self.version,
            "sha256": self.digest,
            "entries": len(self.entries),
        }
