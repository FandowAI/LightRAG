"""Cross-process job ownership and durable receipts, isolated by workspace."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


ACTIVE = ("preparing", "starting", "running")
TERMINAL = ("completed", "completed_with_errors", "failed", "interrupted")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobConflict(ValueError):
    pass


class JobStore:
    """Serialize reservations so separate API workers cannot launch the same job."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs "
                "(job_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
                "status TEXT NOT NULL, record TEXT NOT NULL)"
            )
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _write(conn, record):
        conn.execute(
            "UPDATE jobs SET status=?, record=? WHERE job_id=?",
            (
                record["status"],
                json.dumps(record, ensure_ascii=False),
                record["job_id"],
            ),
        )

    def reserve(self, record: dict, maximum: int) -> tuple[dict, bool]:
        with self.connection() as conn:
            previous = conn.execute(
                "SELECT record FROM jobs WHERE request_id=?", (record["request_id"],)
            ).fetchone()
            if previous:
                return json.loads(previous[0]), False
            active = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?,?,?)", ACTIVE
            ).fetchone()[0]
            if active >= maximum:
                raise JobConflict("Active job limit reached; wait for the current job")
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)",
                (
                    record["job_id"],
                    record["request_id"],
                    record["status"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            return record, True

    def get(self, job_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT record FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            return json.loads(row[0])

    def list(self, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        with self.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            rows = conn.execute(
                "SELECT record FROM jobs ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [json.loads(row[0]) for row in rows], count

    def active(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT record FROM jobs WHERE status IN (?,?,?)", ACTIVE
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def update(
        self, job_id: str, *, expected: tuple[str, ...] | None = None, **changes
    ) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT record FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            record = json.loads(row[0])
            if expected is not None and record["status"] not in expected:
                raise JobConflict(f"Job is already {record['status']}")
            record.update(changes, updated_at=timestamp())
            self._write(conn, record)
            return record

    def claim_start(self, job_id: str, maximum: int) -> tuple[dict, bool]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT record FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            record = json.loads(row[0])
            if record["status"] in ACTIVE:
                return record, False
            if record["status"] != "ready":
                raise JobConflict("Only a prepared, never-executed job can be started")
            active = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?,?,?)", ACTIVE
            ).fetchone()[0]
            if active >= maximum:
                raise JobConflict("Active job limit reached")
            record.update(
                status="starting",
                phase="starting",
                worker_pid=None,
                worker_identity=None,
                updated_at=timestamp(),
            )
            self._write(conn, record)
            return record, True
