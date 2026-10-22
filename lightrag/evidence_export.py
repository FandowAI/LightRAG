"""Publish versioned JSONL packages and a shared, immutable source snapshot.

Graph descriptions are context indexes, never substitutes for original evidence.
Consumers must resolve relationship source IDs in sources.jsonl before treating
an index description as a fact. manifest.json is the publication marker.
"""

from copy import deepcopy
import hashlib
import json
import os


SCHEMA_VERSION = "enterprise-evidence/1.0"


def record_for(package, batch_id, workspace):
    """Keep SourceRecord compatibility and expose the structured evidence fields."""
    record = deepcopy(package["distillation_input"])
    record.update(
        schema_version=SCHEMA_VERSION,
        batch_id=batch_id,
        workspace=workspace,
        package_id=package["package_id"],
        central_entity=package["center"],
        entities=deepcopy(package.get("entities", [])),
        relationships=deepcopy(package.get("relationships", [])),
        original_texts=[
            {
                "passage_id": chunk["chunk_id"],
                "text": chunk["content"],
                "source_units": deepcopy(chunk.get("source_units", [])),
                "provenance": deepcopy(chunk["provenance"]),
            }
            for chunk in package["chunks"]
        ],
        shared_evidence=deepcopy(record.get("metadata", {}).get("shared_evidence", [])),
        source_archive="sources.jsonl",
    )
    return record


def _source_id(row):
    return row.get("source_chunk_id") or row.get("chunk_id")


def publish(manager, job_id):
    """Publish complete files atomically; a failure never creates a valid manifest."""
    folder = manager.root / job_id
    records = list(manager.records(job_id))
    sources = {}
    from lightrag.folder_upload import source_paths

    paths = (
        source_paths(manager.rag.working_dir, getattr(manager.rag, "workspace", ""))
        if getattr(manager.rag, "working_dir", None)
        else {}
    )

    def add(chunk):
        cid, text = chunk["chunk_id"], chunk["content"]
        prior = sources.get(cid)
        if prior and prior["text"] != text:
            raise ValueError("Conflicting source snapshots; export refused")
        origins = deepcopy(prior["provenance"]) if prior else []
        for origin in chunk.get("provenance", []):
            origin = deepcopy(origin)
            basename = origin.get("file_path", "").replace("\\", "/").rsplit("/", 1)[-1]
            if basename in paths:
                origin["source_relative_path"] = paths[basename]
            if origin not in origins:
                origins.append(origin)
        sources[cid] = {
            "chunk_id": cid,
            "text": text,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "provenance": origins,
        }

    for path in sorted(folder.glob("candidate-*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        for package in result.get("packages", []):
            for chunk in package["chunks"]:
                add(chunk)
                for variant in chunk.get("variants", []):
                    add(variant)
    for path in sorted(folder.glob("focus-*.json")):
        audit = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(audit, dict):
            for chunk in audit.get("source_snapshots", []):
                add(chunk)

    package_ids = {record["package_id"] for record in records}
    for record in records:
        if any(
            ref["owner_package_id"] not in package_ids
            for ref in record["shared_evidence"]
        ):
            raise ValueError("Shared evidence owner is not published; export refused")
    required = set()
    for record in records:
        names = {entity["name"] for entity in record["entities"]}
        if record["central_entity"] not in names:
            raise ValueError("Package is missing its central entity")
        for edge in record["relationships"]:
            if not {edge["source"], edge["target"]} <= names:
                raise ValueError("Relationship endpoint missing from package")
            required.update(edge.get("source_chunk_ids", []))
        for entity in record["entities"]:
            required.update(entity.get("source_chunk_ids", []))
        for passage in record["original_texts"]:
            units = passage["source_units"]
            if units:
                pieces = []
                for unit in units:
                    cid = unit["source_chunk_id"]
                    source = sources.get(cid)
                    if source is None:
                        raise ValueError("Original source snapshot missing")
                    start, end = unit["start_char"], unit["end_char"]
                    if not 0 <= start < end <= len(source["text"]):
                        raise ValueError("Original source offset out of range")
                    required.add(cid)
                    pieces.append(unit.get("joiner", "") + source["text"][start:end])
                if "".join(pieces) != passage["text"]:
                    raise ValueError("Passage differs from its literal source spans")
            else:
                # Non-focused adapters retain whole chunks, including old jobs.
                cid = passage["passage_id"]
                if cid not in sources:
                    add(
                        dict(
                            chunk_id=cid,
                            content=passage["text"],
                            provenance=passage["provenance"],
                        )
                    )
                required.add(cid)
            for origin in passage["provenance"]:
                cid = _source_id(origin)
                if cid in sources:
                    required.add(cid)
    if required - sources.keys():
        raise ValueError("Graph evidence source missing from export snapshot")

    manifests = {}
    for filename, rows in (
        ("packages.jsonl", records),
        ("sources.jsonl", [sources[cid] for cid in sorted(required)]),
    ):
        destination = folder / filename
        temporary = destination.with_suffix(".jsonl.tmp")
        digest = hashlib.sha256()
        with temporary.open("wb") as stream:
            for row in rows:
                line = (
                    json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                ).encode("utf-8")
                stream.write(line)
                digest.update(line)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        manifests[filename] = {"rows": len(rows), "sha256": digest.hexdigest()}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": job_id,
        "workspace": getattr(manager.rag, "workspace", ""),
        "files": manifests,
    }
    marker = folder / "manifest.json.tmp"
    marker.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    marker.replace(folder / "manifest.json")
    return str((folder / "packages.jsonl").resolve())
