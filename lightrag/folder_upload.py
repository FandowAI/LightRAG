"""Map relative folder paths to safe basenames without creating subdirectories.

The private SQLite map is provenance only. A failed upload may leave a harmless
map row; it cannot enqueue work or authorize a duplicate/identity decision.
"""

from contextlib import closing
import hashlib
from pathlib import Path, PurePosixPath
import re
import sqlite3
import unicodedata


def folder_source_name(relative_path: str, filename: str) -> tuple[str, str]:
    normalized = relative_path.replace("\\", "/")
    parts = normalized.split("/")
    if (
        len(normalized) > 2048
        or len(parts) < 2
        or any(
            p in {"", ".", ".."}
            or ":" in p
            or any(
                unicodedata.category(c) in {"Cc", "Cs"} or c in "\ufffe\uffff"
                for c in p
            )
            for p in parts
        )
        or parts[-1] != filename
    ):
        raise ValueError("Invalid relative folder path or filename mismatch")
    path = PurePosixPath(normalized)
    suffix = path.suffix
    stem = (
        re.sub(r"[^\w.\[\]-]+", "_", path.stem, flags=re.UNICODE).strip(".")
        or "document"
    )
    # Stay below the 255-byte filename limit even for multi-byte names.
    stem = stem.encode("utf-8")[:120].decode("utf-8", errors="ignore")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{digest}--{stem}{suffix}", normalized


def save_source_path(working_dir, workspace, basename, relative_path):
    path = Path(working_dir) / "folder_sources.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS sources (workspace TEXT, basename TEXT, relative_path TEXT, PRIMARY KEY(workspace, basename))"
        )
        existing = db.execute(
            "SELECT relative_path FROM sources WHERE workspace=? AND basename=?",
            (workspace, basename),
        ).fetchone()
        if existing and existing[0] != relative_path:
            raise ValueError("Folder source digest collision")
        db.execute(
            "INSERT OR IGNORE INTO sources VALUES (?,?,?)",
            (workspace, basename, relative_path),
        )


def source_paths(working_dir, workspace):
    path = Path(working_dir) / "folder_sources.sqlite3"
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path, timeout=15)) as db:
        return dict(
            db.execute(
                "SELECT basename,relative_path FROM sources WHERE workspace=?",
                (workspace,),
            )
        )
