"""Read server documents only within operator-configured import roots."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryFile

from fastapi import HTTPException


ROOTS_ENV = "LIGHTRAG_SERVER_IMPORT_ROOTS"
FILES_ENV = "LIGHTRAG_SERVER_IMPORT_MAX_FILES"
ENTRIES_ENV = "LIGHTRAG_SERVER_IMPORT_MAX_ENTRIES"
MAX_ENTRIES = 20_000
MAX_FILES = 5_000


def configured_limit(name: str, default: int, ceiling: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if not 1 <= value <= ceiling:
            raise ValueError
        return value
    except ValueError as exc:
        raise HTTPException(
            503, f"{name} must be an integer from 1 to {ceiling}"
        ) from exc


def limit_error(kind: str, limit: int, observed: int) -> HTTPException:
    variable = FILES_ENV if kind == "files" else ENTRIES_ENV
    return HTTPException(
        422,
        {
            "code": f"server_import_{kind}_limit",
            "message": f"Directory exceeds the {limit} {kind} preview limit; filter file types, disable recursion or choose a subdirectory",
            "limit": limit,
            "observed": observed,
            "configuration_variable": variable,
        },
    )


def version(info: os.stat_result) -> str:
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return hashlib.sha256(repr(identity).encode()).hexdigest()


class ServerFiles:
    """Never follow user-selected symlinks or modify source documents.

    The roots are an explicit JSON array in LIGHTRAG_SERVER_IMPORT_ROOTS.
    Directory descriptors pin each traversal against parent-symlink swaps.
    """

    def __init__(self):
        self.max_files = configured_limit(FILES_ENV, MAX_FILES, 100_000)
        self.max_entries = configured_limit(ENTRIES_ENV, MAX_ENTRIES, 1_000_000)
        try:
            values = json.loads(os.getenv(ROOTS_ENV, "[]"))
            if not isinstance(values, list) or any(
                not isinstance(value, str)
                or not Path(value).is_absolute()
                or "\x00" in value
                for value in values
            ):
                raise ValueError
            self.roots = list(
                dict.fromkeys(Path(os.path.abspath(value)) for value in values)
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(
                503, f"{ROOTS_ENV} must be a JSON array of absolute directories"
            ) from exc

    def locate(self, value: str) -> tuple[Path, Path]:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise HTTPException(
                400, "Use an absolute server path without parent traversal"
            )
        for root in sorted(self.roots, key=lambda item: len(item.parts), reverse=True):
            if path.is_relative_to(root):
                return root, path.relative_to(root)
        raise HTTPException(
            403, "Directory is outside the configured server import roots"
        )

    @contextmanager
    def directory(self, value: str):
        root, relative = self.locate(value)
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open("/", flags)
            for part in (*root.parts[1:], *relative.parts):
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        except FileNotFoundError as exc:
            raise HTTPException(404, "Server directory no longer exists") from exc
        except (PermissionError, NotADirectoryError) as exc:
            raise HTTPException(
                403, "Server directory is inaccessible or contains a symbolic link"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def browse(self, value: str) -> dict:
        root, relative = self.locate(value)
        directories = []
        with self.directory(value) as descriptor, os.scandir(descriptor) as entries:
            for count, entry in enumerate(entries, 1):
                if count > self.max_entries:
                    raise limit_error("entries", self.max_entries, count)
                if (
                    not entry.name.startswith(".")
                    and entry.name != "__parsed__"
                    and entry.is_dir(follow_symlinks=False)
                ):
                    directories.append(
                        {"name": entry.name, "path": str(Path(value) / entry.name)}
                    )
                if len(directories) > 1000:
                    raise HTTPException(
                        422, "Too many subdirectories; enter a more specific path"
                    )
        return {
            "path": str(root / relative),
            "parent": str((root / relative).parent) if relative.parts else None,
            "directories": sorted(directories, key=lambda item: item["name"]),
        }

    def preview(
        self,
        value: str,
        recursive: bool,
        supported,
        maximum: int | None,
        extensions: set[str] | None = None,
    ) -> dict:
        self.locate(value)
        stack = [(Path(value), 0)]
        files, excluded = [], []
        seen = skipped = 0
        while stack:
            current, depth = stack.pop()
            if depth > 64:
                raise HTTPException(422, "Directory nesting exceeds 64 levels")
            with (
                self.directory(str(current)) as descriptor,
                os.scandir(descriptor) as entries,
            ):
                for entry in entries:
                    seen += 1
                    if seen > self.max_entries:
                        raise limit_error("entries", self.max_entries, seen)
                    path = current / entry.name
                    reason = None
                    if entry.name.startswith(".") or entry.name == "__parsed__":
                        reason = "hidden_or_archive"
                    elif entry.is_symlink():
                        reason = "symbolic_link"
                    elif entry.is_dir(follow_symlinks=False):
                        if recursive:
                            stack.append((path, depth + 1))
                        continue
                    else:
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            reason = "not_regular_file"
                        elif (
                            extensions is not None
                            and path.suffix.lower() not in extensions
                        ):
                            reason = "extension_filtered"
                        else:
                            try:
                                usable = supported(entry.name)
                            except ValueError:
                                usable = False
                            if not usable:
                                reason = "unsupported_type"
                            elif maximum and maximum > 0 and info.st_size > maximum:
                                reason = "too_large"
                            else:
                                files.append(
                                    {
                                        "path": str(path),
                                        "relative_path": str(path.relative_to(value)),
                                        "size": info.st_size,
                                        "version": version(info),
                                    }
                                )
                                if len(files) > self.max_files:
                                    raise limit_error(
                                        "files", self.max_files, len(files)
                                    )
                    if reason:
                        skipped += 1
                        if len(excluded) < 80:
                            excluded.append({"path": str(path), "reason": reason})
        return {
            "path": value,
            "files": sorted(files, key=lambda item: item["relative_path"]),
            "skipped": skipped,
            "excluded": excluded,
        }

    def snapshot(self, value: str, expected: str, maximum: int | None):
        """Return a disk snapshot only if the previewed regular file is unchanged."""
        root, relative = self.locate(value)
        if not relative.parts:
            raise HTTPException(400, "Choose a file inside an import root")
        snapshot = TemporaryFile()
        try:
            with self.directory(str(Path(value).parent)) as parent:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                try:
                    descriptor = os.open(Path(value).name, flags, dir_fd=parent)
                except FileNotFoundError as exc:
                    raise HTTPException(404, "Source file no longer exists") from exc
                except OSError as exc:
                    raise HTTPException(
                        403, "Source file is inaccessible or unsafe"
                    ) from exc
                with os.fdopen(descriptor, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise HTTPException(400, "Only regular files can be imported")
                    if version(before) != expected:
                        raise HTTPException(
                            409, "Source file changed; preview the directory again"
                        )
                    if maximum and maximum > 0 and before.st_size > maximum:
                        raise HTTPException(413, "Source file exceeds MAX_UPLOAD_SIZE")
                    total = 0
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if maximum and maximum > 0 and total > maximum:
                            raise HTTPException(
                                413, "Source file exceeds MAX_UPLOAD_SIZE"
                            )
                        snapshot.write(chunk)
                    if version(os.fstat(source.fileno())) != expected:
                        raise HTTPException(
                            409, "Source file changed while being copied; preview again"
                        )
            snapshot.seek(0)
            prefix = "server-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
            return snapshot, total, f"{prefix}/{relative.as_posix()}"
        except BaseException:
            snapshot.close()
            raise
