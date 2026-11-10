"""Freeze authorized source files and reuse descriptor-safe import snapshots."""

from __future__ import annotations

import hashlib
from pathlib import Path

from lightrag.api.server_files import ServerFiles
from lightrag.parser.registry import suffix_capabilities

from .storage import IntegrityError


def freeze_inputs(catalog, references, bridge, *, documents):
    allowed = set(bridge["input"]["paths"])
    paths = list(
        dict.fromkeys(str(path) for ref in references for path in catalog.inputs[ref])
    )
    if any(path not in allowed for path in paths):
        raise IntegrityError(
            "Input reference paths changed after configuration publication"
        )
    roots = list(
        dict.fromkeys(
            path if Path(path).is_dir() else str(Path(path).parent) for path in paths
        )
    )
    reader = ServerFiles(roots=roots)
    maximum = bridge["limits"]["max_input_bytes"]
    max_records = bridge["limits"]["max_records"]
    reader.max_files = max_records
    suffixes = (
        {"." + suffix for suffix in suffix_capabilities("native")}
        if documents
        else {".json", ".jsonl"}
    )
    files, excluded, seen, total = [], [], set(), 0
    for value in paths:
        path = Path(value)
        preview = reader.preview(
            str(path if path.is_dir() else path.parent),
            path.is_dir(),
            lambda name: (
                Path(name).suffix.lower() in suffixes
                and (path.is_dir() or name == path.name)
            ),
            None,
        )
        excluded.extend(preview["excluded"])
        for row in preview["files"]:
            if row["path"] in seen:
                continue
            seen.add(row["path"])
            with open_snapshot(reader, row, maximum)[0] as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append({**row, "sha256": sha})
            total += row["size"]
            if len(files) > max_records or total > maximum:
                raise ValueError(
                    "Selected inputs exceed the configured record/byte limits; no subset was submitted"
                )
    if not files:
        raise ValueError("Selected inputs contain no supported files")
    return {"roots": roots, "files": files, "excluded": excluded, "bytes": total}


def open_snapshot(reader, row, maximum):
    stream, size, relative = reader.snapshot(row["path"], row["version"], maximum)
    if "sha256" in row:
        if hashlib.file_digest(stream, "sha256").hexdigest() != row["sha256"]:
            stream.close()
            raise IntegrityError("Source content changed after task submission")
        stream.seek(0)
    return stream, size, relative
