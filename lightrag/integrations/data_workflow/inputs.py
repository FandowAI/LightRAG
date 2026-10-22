"""Read configured JSON/JSONL snapshots; no user-supplied executable or URL."""

from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any, Iterator

from .config import BridgeSettings


def _reject_constant(value):
    raise ValueError(f"Nonfinite JSON value: {value}")


def _records(value: Any, path: list[str | int]) -> list:
    for key in path:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("input.records_path does not exist in the source") from exc
    return value if isinstance(value, list) else [value]


def read_results(settings: BridgeSettings) -> Iterator[tuple[Any, str | None]]:
    """Yield individual records or explicit parse failures, within total input limits."""
    total_bytes = count = 0

    def bounded(values):
        nonlocal count
        for item in values:
            count += 1
            if count > settings.limits.max_records:
                raise ValueError("Input exceeds limits.max_records")
            yield item, None

    files = []
    for pattern in settings.input.paths:
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise ValueError(f"Input path does not exist: {pattern}")
        for name in matches:
            path = Path(name)
            if path.is_dir():
                files.extend(
                    sorted(p for p in path.iterdir() if p.suffix in {".json", ".jsonl"})
                )
            else:
                files.append(path)
    files = list(dict.fromkeys(path.resolve() for path in files))
    if not files:
        raise ValueError("Configure input.paths in the bridge configuration")
    for path in files:
        if path.suffix not in {".json", ".jsonl"}:
            raise ValueError(f"Input must be JSON or JSONL: {path}")
        before = path.stat()
        # The actual read is bounded too, even when a producer grows the file.
        with path.open("rb") as stream:
            raw = stream.read(settings.limits.max_input_bytes - total_bytes + 1)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"Input changed while being read: {path}")
        total_bytes += len(raw)
        if total_bytes > settings.limits.max_input_bytes:
            raise ValueError("Input exceeds limits.max_input_bytes")
        units = raw.splitlines() if path.suffix == ".jsonl" else [raw]
        for unit in units:
            if not unit.strip():
                continue
            try:
                value = json.loads(unit, parse_constant=_reject_constant)
            except (ValueError, UnicodeError):
                count += 1
                if count > settings.limits.max_records:
                    raise ValueError("Input exceeds limits.max_records")
                yield None, "Invalid JSON or UTF-8 in source record"
                continue
            yield from bounded(_records(value, settings.input.records_path))
