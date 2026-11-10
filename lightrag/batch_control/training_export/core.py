"""Keep export inputs stable and publish all artifacts before the final marker.

See docs/design/BatchTrainingExports.md for selection and publication semantics.
The workflow interpreter loads this module without importing LightRAG.
"""

import hashlib
import json
import os
from pathlib import Path
import stat

FILES = {
    "dataset": "dataset.jsonl",
    "provenance": "provenance.jsonl",
    "manifest": "export.json",
    "log": "stderr.log",
}


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="backslashreplace")


def snapshot(path, maximum):
    path = Path(path)
    if path != path.resolve():
        raise ValueError("Export input was redirected")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("Export input exceeds the configured evidence limit")
        digest, total = hashlib.sha256(), 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > maximum:
                raise ValueError("Export input exceeds the configured evidence limit")
            digest.update(chunk)
        after = os.fstat(stream.fileno())

    def fields(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if fields(before) != fields(after) or fields(path.stat()) != fields(before):
        raise ValueError("Export input changed while it was read")
    return {
        "bytes": total,
        "sha256": digest.hexdigest(),
        "version": list(fields(before)),
    }


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".temporary")
    with temporary.open("wb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def publish_file(part, target):
    os.link(part, target)
    Path(part).unlink()
    sync_directory(Path(target).parent)


def verify_publication(directory, identity, maximum):
    directory = Path(directory)
    marker = directory / FILES["manifest"]
    if directory != directory.resolve() or marker.is_symlink():
        raise ValueError("Training export publication was redirected")
    if marker.stat().st_size > 65536:
        raise ValueError("Training export marker exceeds its publication limit")
    manifest = json.loads(marker.read_bytes())
    if manifest.get("schema_version") != 1 or any(
        manifest.get(key) != value for key, value in identity.items()
    ):
        raise ValueError("Training export belongs to another request or task")
    if set(manifest.get("files", {})) != {"dataset.jsonl", "provenance.jsonl"}:
        raise ValueError("Training export artifact catalog is incomplete")
    for filename, expected in manifest["files"].items():
        actual = snapshot(directory / filename, maximum)
        if any(actual[key] != expected[key] for key in ("bytes", "sha256")):
            raise ValueError("Published training export changed")
    return manifest
