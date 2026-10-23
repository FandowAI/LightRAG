"""Read published batch exports without re-expanding deduplicated evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import Field

from .config import Limits, StrictModel
from .storage import JobConflict


class BatchSnapshot(StrictModel):
    batch_id: str
    workspace: str
    directory: Path
    manifest: dict
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _read(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Batch export exceeds limits.max_input_bytes")
    return raw


def resolve_batch(rag, batch_id: str) -> BatchSnapshot:
    """Resolve only a completed batch owned by the initialized RAG workspace."""
    from lightrag.batch_retrieval import BatchManager

    manager = BatchManager(rag)
    job = manager.get(batch_id)
    if job["status"] not in {"completed", "completed_with_errors"}:
        raise JobConflict("Wait for batch retrieval to finish before distillation")
    directory = (manager.root / batch_id).resolve()
    if directory.parent != manager.root.resolve():
        raise ValueError("Batch directory is outside the workspace")
    if not job.get("package_count") or job.get("export_path") != str(
        directory / "packages.jsonl"
    ):
        raise JobConflict("Batch has no published original-text records")
    marker = directory / "manifest.json"
    if not marker.is_file():
        raise JobConflict("Batch publication is incomplete")
    raw = _read(marker, 65536)
    manifest = json.loads(raw)
    workspace = getattr(rag, "workspace", "")
    if (
        not isinstance(manifest, dict)
        or manifest.get("batch_id") != batch_id
        or manifest.get("workspace") != workspace
    ):
        raise ValueError("Batch manifest identity does not match the workspace")
    if manifest.get("schema_version") != "enterprise-evidence/1.0":
        raise ValueError("Unsupported batch export schema")
    return BatchSnapshot(
        batch_id=batch_id,
        workspace=workspace,
        directory=directory,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _query_result(row: dict, snapshot: BatchSnapshot) -> dict:
    if (
        not isinstance(row, dict)
        or row.get("schema_version") != "enterprise-evidence/1.0"
    ):
        raise ValueError("Expected an enterprise-evidence/1.0 package")
    if (
        row.get("batch_id") != snapshot.batch_id
        or row.get("workspace") != snapshot.workspace
    ):
        raise ValueError("Package identity does not match the batch workspace")
    for key in ("entities", "relationships", "original_texts"):
        if not isinstance(row.get(key), list) or any(
            not isinstance(x, dict) for x in row[key]
        ):
            raise ValueError(f"Package {key} must be an array of objects")
    return {
        "status": "success",
        "data": {
            "entities": [
                {
                    "entity_name": x.get("name"),
                    "entity_type": x.get("entity_type", "UNKNOWN"),
                    "description": x.get("description", ""),
                }
                for x in row["entities"]
            ],
            "relationships": [
                {
                    "src_id": x.get("source"),
                    "tgt_id": x.get("target"),
                    "description": x.get("description", ""),
                }
                for x in row["relationships"]
            ],
            "chunks": [{"content": x.get("text")} for x in row["original_texts"]],
        },
    }


def read_batch_results(snapshot: BatchSnapshot, limits: Limits):
    """Validate frozen publication hashes before yielding any workflow input.

    Only original_texts enter the teacher context. sources.jsonl remains an audit
    archive; shared_evidence references never cause duplicate texts to be expanded.
    """
    directory = snapshot.directory
    marker = directory / "manifest.json"
    if (
        marker.resolve().parent != directory
        or hashlib.sha256(_read(marker, 65536)).hexdigest() != snapshot.manifest_sha256
    ):
        raise ValueError("Batch manifest changed after submission")
    total = 0
    packages = []
    for filename in ("packages.jsonl", "sources.jsonl"):
        path = directory / filename
        if path.resolve().parent != directory:
            raise ValueError("Batch artifact is outside the workspace directory")
        info = snapshot.manifest.get("files", {}).get(filename)
        if (
            not isinstance(info, dict)
            or type(info.get("rows")) is not int
            or info["rows"] < 0
        ):
            raise ValueError(f"Missing batch artifact manifest: {filename}")
        raw = _read(path, limits.max_input_bytes - total)
        total += len(raw)
        if hashlib.sha256(raw).hexdigest() != info.get("sha256"):
            raise ValueError(f"Batch artifact checksum mismatch: {filename}")
        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) != info["rows"]:
            raise ValueError(f"Batch artifact row count mismatch: {filename}")
        if filename == "packages.jsonl":
            packages = lines
    if not packages or len(packages) > limits.max_records:
        raise ValueError("Batch package count must be within limits.max_records")
    for line in packages:
        try:
            row = json.loads(
                line,
                parse_constant=lambda x: (_ for _ in ()).throw(
                    ValueError("Nonfinite JSON")
                ),
            )
            yield _query_result(row, snapshot), None
        except (ValueError, UnicodeError) as exc:
            yield None, str(exc)
