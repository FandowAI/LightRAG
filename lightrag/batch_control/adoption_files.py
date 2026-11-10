"""Keep complete, verified copies of a stopped workspace; never rewrite its rows."""

import os
from pathlib import Path
import shutil
import sqlite3

from .adoption_snapshot import file_sha256
from .adoption_resources import resource_sha256
from .runtime_process import atomic_json
from .storage import IntegrityError
from .writer_claim import read_json, sync_directory


def copy_file(source, target):
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    before = source.stat()
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream, target.open("xb") as output:
        os.chmod(target, 0o600)
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
        ):
            raise IntegrityError("Source changed before backup")
        shutil.copyfileobj(stream, output, 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
        after = os.fstat(stream.fileno())
    if (opened.st_mtime_ns, opened.st_size) != (
        after.st_mtime_ns,
        after.st_size,
    ) or file_sha256(source) != file_sha256(target):
        raise IntegrityError("Source changed during backup")
    return {"bytes": target.stat().st_size, "sha256": file_sha256(target)}


def copy_tree(source, target):
    source, target = Path(source), Path(target)
    result = {}
    if not source.exists():
        return result
    if source.is_symlink() or target.resolve().is_relative_to(source.resolve()):
        raise IntegrityError("Backup must be outside the original workspace")
    for directory, dirs, files in os.walk(source, followlinks=False):
        base = Path(directory)
        if any((base / name).is_symlink() for name in [*dirs, *files]):
            raise IntegrityError("Symlinks are not supported in adoption backups")
        (target / base.relative_to(source)).mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        for name in files:
            relative = (base / name).relative_to(source)
            if name in {".batch-writer.lock", ".batch-owner.json"}:
                continue
            result[str(relative)] = copy_file(base / name, target / relative)
    return result


def backup(
    source, destination, snapshot_directory, resources, *, retrieval_directory=None
):
    source_root = Path(source.working_directory) / source.workspace
    destination = Path(destination)
    marker = destination / "manifest.json"
    if marker.exists():
        return verify_backup(destination)
    # Partial copies are diagnostic; a later step writes a fresh attempt.
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = {}
    for name, origin in (
        ("storage", source_root),
        ("inputs", Path(source.input_directory) / source.workspace),
        ("remote", Path(snapshot_directory)),
    ):
        for relative, value in copy_tree(origin, destination / name).items():
            files[name + "/" + relative] = value
    if retrieval_directory is not None:
        retrieval = Path(retrieval_directory)
        if not retrieval.is_absolute() or retrieval != retrieval.resolve():
            raise IntegrityError("Retrieval history directory was redirected")
        if not retrieval.is_relative_to(source_root):
            for relative, value in copy_tree(
                retrieval, destination / "retrieval"
            ).items():
                files["retrieval/" + relative] = value
    for relative in ("entity_merge_settings.sqlite3", "prompt_admin/prompts.sqlite3"):
        path = Path(source.working_directory) / relative
        if not path.exists():
            continue
        if path != path.resolve():
            raise IntegrityError("Shared settings database was redirected")
        target = destination / "shared-settings" / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as original,
            sqlite3.connect(target) as copied,
        ):
            original.backup(copied)
            if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise IntegrityError("Shared settings backup is invalid")
        os.chmod(target, 0o600)
        files[str(target.relative_to(destination))] = {
            "bytes": target.stat().st_size,
            "sha256": file_sha256(target),
        }
    pinned = {}
    for name, path in resources.items():
        target = destination / "resources" / (name + Path(path).suffix)
        if Path(path).is_dir():
            for relative, info in copy_tree(path, target).items():
                files[str((target / relative).relative_to(destination))] = info
        else:
            info = copy_file(path, target)
            files[str(target.relative_to(destination))] = info
        if resource_sha256(path) != resource_sha256(target):
            raise IntegrityError("Resource tree changed during backup")
        pinned[name] = {
            "path": str(target),
            "sha256": resource_sha256(target),
            "kind": "directory" if target.is_dir() else "file",
        }
    result = {
        "schema_version": 1,
        "workspace": source.workspace,
        "files": files,
        "resources": pinned,
    }
    atomic_json(marker, result)
    for directory in [p for p in destination.rglob("*") if p.is_dir()] + [destination]:
        sync_directory(directory)
    sync_directory(destination.parent)
    return verify_backup(destination)


def verify_backup(directory):
    directory = Path(directory)
    result = read_json(directory / "manifest.json")
    for relative, record in result["files"].items():
        path = directory / relative
        if (
            not path.resolve().is_relative_to(directory.resolve())
            or path.is_symlink()
            or path.stat().st_size != record["bytes"]
            or file_sha256(path) != record["sha256"]
        ):
            raise IntegrityError("Adoption backup changed")
    for resource in result["resources"].values():
        path = Path(resource["path"])
        if (
            not path.resolve().is_relative_to(directory.resolve())
            or resource_sha256(path) != resource["sha256"]
        ):
            raise IntegrityError("Adoption resource backup changed")
    return result
