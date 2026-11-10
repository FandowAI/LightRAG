"""Compare complete canonical stores while their owning worker is fenced.

The caller must prevent writers through both snapshots. These reads never flush
or repair the original stores. See docs/design/BatchIndexRebuild.md.
"""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
from typing import Literal

from pydantic import Field, model_validator

from ..bindings import GraphBaseline
from ..contracts import Contract, Name
from ..storage import IntegrityError
from .projection import encode, sha
from .source import SourceIdentity, SourceLimits, graph_digest, load_json

KV_STORES = (
    "full_docs",
    "text_chunks",
    "doc_status",
    "full_entities",
    "full_relations",
    "entity_chunks",
    "relation_chunks",
)


class StoreAudit(Contract):
    rows: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceAudit(Contract):
    schema_version: Literal[1] = 1
    identity: SourceIdentity
    graph: GraphBaseline
    kv: dict[Name, StoreAudit]

    @model_validator(mode="after")
    def complete(self):
        if set(self.kv) != set(KV_STORES):
            raise ValueError("Index audit must cover every canonical KV/status store")
        return self


async def snapshot_state(rag, identity: SourceIdentity, *, limits=None):
    """Return audit plus canonical rows; caller owns admission and pipeline locks."""
    limits = limits or SourceLimits()
    if rag.workspace != identity.workspace:
        raise IntegrityError("Index audit read a different source workspace")
    nodes = await rag.chunk_entity_relation_graph.get_all_nodes()
    edges = await rag.chunk_entity_relation_graph.get_all_edges()
    data, total, count = {}, 0, len(nodes) + len(edges)
    for name in KV_STORES:
        storage = getattr(rag, name)
        expected = "JsonDocStatusStorage" if name == "doc_status" else "JsonKVStorage"
        if (
            type(storage).__name__ != expected
            or storage.workspace != identity.workspace
            or storage._storage_lock is None
            or storage._data is None
        ):
            raise IntegrityError(
                "Index capture requires an initialized canonical JSON store: " + name
            )
        async with storage._storage_lock:
            rows = deepcopy(dict(storage._data))
        count += len(rows)
        if count > limits.max_rows:
            raise ValueError("Index audit exceeds its complete source row limit")
        for key, value in rows.items():
            if not isinstance(key, str) or not key or not isinstance(value, dict):
                raise IntegrityError("Invalid canonical KV/status record: " + name)
            size = len(encode({key: value}))
            total += size
            if size > limits.max_row_bytes or total > limits.max_evidence_bytes:
                raise ValueError("Index audit exceeds its complete source byte limit")
        data[name] = rows
    audit = SourceAudit(
        identity=identity,
        graph=graph_digest(nodes, edges),
        kv={
            name: StoreAudit(rows=len(rows), sha256=sha(rows))
            for name, rows in data.items()
        },
    )
    canonical = {
        "nodes": nodes,
        "edges": edges,
        "chunks": [{**row, "id": key} for key, row in data["text_chunks"].items()],
    }
    return audit, canonical


def read_audit(path, identity: SourceIdentity, expected_sha256):
    path = Path(path)
    if path != path.resolve():
        raise IntegrityError("Index source audit was redirected")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
            raise IntegrityError("Invalid index source audit file")
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())

    def version(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if (
        len(raw) > 65536
        or version(before) != version(after)
        or version(before) != version(path.stat())
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        raise IntegrityError("Index source audit changed")
    audit = SourceAudit.model_validate(load_json(raw))
    if audit.identity != identity:
        raise IntegrityError("Index source audit belongs to another generation")
    return audit


async def verify_state(rag, audit: SourceAudit, *, limits=None):
    actual, canonical = await snapshot_state(rag, audit.identity, limits=limits)
    if actual != audit:
        raise IntegrityError(
            "Canonical graph/document/status state changed after index capture"
        )
    return canonical
