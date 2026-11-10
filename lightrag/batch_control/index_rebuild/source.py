"""Seal canonical source evidence in a private, operation-owned directory.

The owning worker must hold the rebuild admission fence through source capture
and confirmed retirement. This module does not freeze a running workspace.
See docs/design/BatchIndexRebuild.md for the coordinator contract.
"""

import hashlib
import json
import os
from pathlib import Path
import stat

from pydantic import Field

from ..bindings import rows_digest
from ..contracts import Contract, Name
from ..runtime_process import atomic_json
from ..storage import IntegrityError
from ..writer_claim import sync_directory
from .projection import bind_plan, encode, plan_digest, project

FILES = {"nodes": "nodes.jsonl", "edges": "edges.jsonl", "chunks": "chunks.jsonl"}


class SourceIdentity(Contract):
    data_batch_id: Name
    operation_id: Name
    config_revision_id: Name
    workspace: Name
    generation: int = Field(gt=0)
    lease_id: Name
    startup_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceLimits(Contract):
    max_evidence_bytes: int = Field(default=1073741824, gt=0)
    max_row_bytes: int = Field(default=134217728, gt=0)
    max_rows: int = Field(default=10000000, gt=0)


def graph_digest(nodes, edges):
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "nodes_sha256": rows_digest(nodes),
        "edges_sha256": rows_digest(edges),
    }


def write_source(
    directory,
    identity: SourceIdentity,
    nodes,
    edges,
    chunks,
    *,
    names=False,
    limits=None,
):
    """Publish a complete source marker last; never reuse a partial directory."""
    limits = limits or SourceLimits()
    if type(names) is not bool:
        raise ValueError("Name-index selection must be explicit")
    directory = Path(directory)
    if directory != directory.resolve():
        raise IntegrityError("Index source destination was redirected")
    values = {"nodes": list(nodes), "edges": list(edges), "chunks": list(chunks)}
    if sum(len(rows) for rows in values.values()) > limits.max_rows:
        raise ValueError("Index source exceeds its row limit; no subset was sealed")
    plan = bind_plan(project(**values, names=names), names=names)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    files, total = {}, 0
    for key, filename in FILES.items():
        part, target = directory / (filename + ".part"), directory / filename
        digest, size = hashlib.sha256(), 0
        with part.open("xb") as stream:
            os.chmod(part, 0o600)
            for row in values[key]:
                raw = encode(row) + b"\n"
                total += len(raw)
                size += len(raw)
                if len(raw) > limits.max_row_bytes or total > limits.max_evidence_bytes:
                    raise ValueError(
                        "Index source exceeds its byte limit; no subset was sealed"
                    )
                stream.write(raw)
                digest.update(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(part, target)
        part.unlink()
        files[filename] = {
            "rows": len(values[key]),
            "bytes": size,
            "sha256": digest.hexdigest(),
        }
    manifest = {
        "schema_version": 1,
        "identity": identity.model_dump(),
        "graph": graph_digest(values["nodes"], values["edges"]),
        "projection": plan_digest(plan),
        "name_index": names,
        "files": files,
    }
    atomic_json(directory / "source.json", manifest)
    sync_directory(directory)
    sync_directory(directory.parent)
    return manifest


def load_json(raw):
    def invalid(value):
        raise ValueError("Nonfinite JSON in index evidence: " + value)

    return json.loads(raw, parse_constant=invalid)


def iter_file_rows(path, expected, limits):
    """Yield read-only verification rows; only full exhaustion validates the artifact."""
    path = Path(path)
    if path != path.resolve():
        raise IntegrityError("Index source artifact was redirected")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > limits.max_evidence_bytes
        ):
            raise ValueError("Invalid or oversized index source artifact")
        count, size, digest = 0, 0, hashlib.sha256()
        while raw := stream.readline(limits.max_row_bytes + 1):
            count += 1
            size += len(raw)
            if (
                len(raw) > limits.max_row_bytes
                or size > limits.max_evidence_bytes
                or count > limits.max_rows
            ):
                raise ValueError("Index source exceeds configured limits")
            digest.update(raw)
            yield load_json(raw)
        after = os.fstat(stream.fileno())

    def fields(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if fields(before) != fields(after) or fields(path.stat()) != fields(before):
        raise IntegrityError("Index source artifact changed during reading")
    if {"rows": count, "bytes": size, "sha256": digest.hexdigest()} != expected:
        raise IntegrityError("Index source artifact does not match its sealed marker")


def file_rows(path, expected, limits):
    """Load a whole source artifact before a caller can use it for target writes."""
    return list(iter_file_rows(path, expected, limits))


def read_source(directory, identity: SourceIdentity, *, limits=None):
    """Check ownership, every artifact and the canonical projection before returning."""
    limits = limits or SourceLimits()
    directory = Path(directory)
    if directory != directory.resolve():
        raise IntegrityError("Index source directory was redirected")
    marker = directory / "source.json"
    if marker.is_symlink() or marker.stat().st_size > 65536:
        raise IntegrityError("Invalid index source marker")
    manifest = load_json(marker.read_bytes())
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("identity") != identity.model_dump()
        or type(manifest.get("name_index")) is not bool
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != set(FILES.values())
    ):
        raise IntegrityError(
            "Index source belongs to another operation or has an invalid catalog"
        )
    values = {}
    for info in manifest["files"].values():
        if not isinstance(info, dict) or any(
            type(info.get(field)) is not int or info[field] < 0
            for field in ("bytes", "rows")
        ):
            raise IntegrityError("Index source artifact metadata is invalid")
    if (
        sum(info["bytes"] for info in manifest["files"].values())
        > limits.max_evidence_bytes
        or sum(info["rows"] for info in manifest["files"].values()) > limits.max_rows
    ):
        raise ValueError("Index source exceeds aggregate evidence limits")
    for key, filename in FILES.items():
        info = manifest["files"][filename]
        values[key] = file_rows(directory / filename, info, limits)
    if (
        sum(info["bytes"] for info in manifest["files"].values())
        > limits.max_evidence_bytes
        or sum(len(rows) for rows in values.values()) > limits.max_rows
    ):
        raise ValueError("Index source exceeds aggregate evidence limits")
    plan = bind_plan(
        project(**values, names=manifest["name_index"]), names=manifest["name_index"]
    )
    if manifest.get("graph") != graph_digest(
        values["nodes"], values["edges"]
    ) or manifest.get("projection") != plan_digest(plan):
        raise IntegrityError("Index source projection or graph baseline changed")
    return manifest, values
