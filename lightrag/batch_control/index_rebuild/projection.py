"""Project complete frozen graph/chunk evidence; reject ambiguous vector identities.

No old vector row is an authority for rebuilding missing or stale indexes.
See docs/design/BatchIndexRebuild.md for activation and ownership boundaries.
"""

from collections.abc import Iterable, Mapping
import hashlib
import json
import math

from lightrag.utils import compute_mdhash_id
from ..storage import IntegrityError

META_FIELDS = {
    "entities": {"entity_name", "source_id", "content", "file_path"},
    "relationships": {"src_id", "tgt_id", "source_id", "content", "file_path"},
    "chunks": {"full_doc_id", "content", "file_path"},
    "name_entities": {"entity_name", "entity_type", "content"},
}


def encode(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sha(value):
    return hashlib.sha256(encode(value)).hexdigest()


def text(record, key, *, required=False):
    value = record.get(key, "")
    if not isinstance(value, str) or required and not value.strip():
        raise IntegrityError("Index source has an invalid text field: " + key)
    return value


def project(
    nodes: Iterable[dict], edges: Iterable[dict], chunks: Iterable[dict], *, names=False
):
    """Yield (collection role, vector ID, fields) from canonical source records.

    Chunk inputs carry their actual KV key as `id`. Content is untruncated;
    callers bind any target-model token transformation before recording a plan.
    Consuming this generator completely is required before using its output.
    """
    node_ids, vector_ids, edge_ids, chunk_ids = set(), set(), set(), set()

    def unique(role, key):
        identity = role, key
        if identity in vector_ids:
            raise IntegrityError("Index projection has a duplicate vector identity")
        vector_ids.add(identity)
        return role, key

    for node in nodes:
        if not isinstance(node, dict):
            raise IntegrityError("Index graph node must be an object")
        name = text(node, "id", required=True)
        if name in node_ids:
            raise IntegrityError("Index graph has duplicate node identities")
        node_ids.add(name)
        yield (
            *unique("entities", compute_mdhash_id(name, prefix="ent-")),
            {
                "entity_name": name,
                "source_id": text(node, "source_id"),
                "content": name + "\n" + text(node, "description"),
                "file_path": text(node, "file_path"),
            },
        )
        if names:
            from lightrag.entity_merge import EntityMergeManager

            kind = EntityMergeManager._type(node.get("entity_type"))
            if kind:
                yield (
                    *unique(
                        "name_entities", compute_mdhash_id(name, prefix="ent-name-")
                    ),
                    {
                        "entity_name": name,
                        "entity_type": kind,
                        "content": name,
                    },
                )
    for edge in edges:
        if not isinstance(edge, dict):
            raise IntegrityError("Index graph relation must be an object")
        source, target = sorted(
            (text(edge, "source", required=True), text(edge, "target", required=True))
        )
        pair = source, target
        if source not in node_ids or target not in node_ids or pair in edge_ids:
            raise IntegrityError(
                "Index graph has missing endpoints or duplicate relations"
            )
        edge_ids.add(pair)
        yield (
            *unique("relationships", compute_mdhash_id(source + target, prefix="rel-")),
            {
                "src_id": source,
                "tgt_id": target,
                "source_id": text(edge, "source_id"),
                "content": f"{text(edge, 'keywords')}\t{source}\n{target}\n{text(edge, 'description')}",
                "file_path": text(edge, "file_path"),
            },
        )
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise IntegrityError("Index text chunk must be an object")
        key = text(chunk, "id", required=True)
        if key in chunk_ids:
            raise IntegrityError("Index source has duplicate chunk keys")
        chunk_ids.add(key)
        yield (
            *unique("chunks", key),
            {
                "full_doc_id": text(chunk, "full_doc_id", required=True),
                "content": text(chunk, "content", required=True),
                "file_path": text(chunk, "file_path"),
            },
        )


def bind_plan(rows, *, transform=lambda role, key, content: content, names=False):
    """Validate all rows before writing a target; retain exact expected metadata.

    A target's token transformation must be applied once here and frozen in the
    plan. Verification compares the persisted content, not just collection size.
    """
    roles = {"entities", "relationships", "chunks"} | (
        {"name_entities"} if names else set()
    )
    plan = {role: {} for role in roles}
    for role, key, record in rows:
        if (
            role not in roles
            or not isinstance(key, str)
            or not key
            or key in plan[role]
        ):
            raise IntegrityError(
                "Index plan has an invalid role or duplicate primary key"
            )
        if not isinstance(record, dict) or set(record) != META_FIELDS[role]:
            raise IntegrityError("Index plan does not declare its complete metadata")
        record = dict(record)
        record["content"] = transform(role, key, record["content"])
        if not isinstance(record["content"], str) or not record["content"].strip():
            raise IntegrityError(
                "Index content transformation produced an empty record"
            )
        encode(record)
        plan[role][key] = record
    return plan


def plan_digest(plan):
    return {
        role: {"rows": len(rows), "sha256": sha(rows)}
        for role, rows in sorted(plan.items())
    }


def verify_rows(
    role: str, expected: Mapping[str, dict], rows: Iterable[dict], dimensions: int
):
    """Require exact primary keys/content/metadata and finite nonzero cosine vectors."""
    if role not in META_FIELDS or type(dimensions) is not int or dimensions <= 0:
        raise ValueError("Invalid target vector schema")
    seen, vector_hashes = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise IntegrityError("Target vector row is not an object")
        key = row.get("id", row.get("__id__"))
        if not isinstance(key, str) or key not in expected or key in seen:
            raise IntegrityError(
                "Target index contains an unexpected or duplicate primary key"
            )
        metadata = {field: row.get(field) for field in META_FIELDS[role]}
        if metadata != expected[key]:
            raise IntegrityError(
                "Target index content or metadata differs from its plan"
            )
        vector = row.get("vector", row.get("__vector__"))
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            raise IntegrityError("Target index has an incorrect vector dimension")
        if any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in vector
        ):
            raise IntegrityError("Target index contains a non-finite or invalid vector")
        if not any(value != 0 for value in vector):
            raise IntegrityError("Target index contains an unusable zero cosine vector")
        seen.add(key)
        vector_hashes.append(sha({"id": key, **metadata, "vector": vector}))
    if seen != set(expected):
        raise IntegrityError("Target index is incomplete")
    return {
        "rows": len(seen),
        "content_sha256": sha(dict(expected)),
        "vectors_sha256": sha(sorted(vector_hashes)),
        "dimensions": dimensions,
    }
