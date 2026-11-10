"""Single SQLite authority for batch identity, configuration CAS and queued envelopes."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .contracts import BatchRecord


class Conflict(ValueError):
    pass


class IntegrityError(ValueError):
    pass


class Registry:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise IntegrityError("Unsupported control registry version")
            for statement in (
                "CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, workspace TEXT UNIQUE NOT NULL, namespace TEXT UNIQUE NOT NULL, name TEXT NOT NULL, active TEXT NOT NULL, desired TEXT, pending TEXT, index_state TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'registered')",
                "CREATE TABLE IF NOT EXISTS revisions (batch TEXT NOT NULL, id TEXT NOT NULL, directory TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, components TEXT NOT NULL, PRIMARY KEY(batch,id))",
                "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, batch TEXT NOT NULL, revision TEXT NOT NULL, status TEXT NOT NULL, requires_restart INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS requests (owner TEXT NOT NULL, kind TEXT NOT NULL, batch TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(owner,kind,batch,key))",
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, batch TEXT NOT NULL, revision TEXT NOT NULL, record TEXT NOT NULL)",
            ):
                db.execute(statement)
            db.execute("PRAGMA user_version=1")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def prior(db, owner, kind, batch, key, fingerprint):
        row = db.execute(
            "SELECT * FROM requests WHERE owner=? AND kind=? AND batch=? AND key=?",
            (owner, kind, batch, key),
        ).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise Conflict("Idempotency key belongs to a different request")
            return json.loads(row["result"])
        return None

    def previous(self, owner, kind, batch, key, fingerprint):
        with self.transaction() as db:
            return self.prior(db, owner, kind, batch, key, fingerprint)

    @staticmethod
    def remember(db, owner, kind, batch, key, fingerprint, result):
        db.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?,?)",
            (owner, kind, batch, key, fingerprint, json.dumps(result)),
        )

    @staticmethod
    def row(db, batch):
        row = db.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
        if row is None:
            raise KeyError(batch)
        return BatchRecord.model_validate(dict(row)).model_dump()

    def get(self, batch):
        with self.transaction() as db:
            return self.row(db, batch)

    def revision(self, batch, revision):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM revisions WHERE batch=? AND id=?", (batch, revision)
            ).fetchone()
            if row is None:
                raise KeyError((batch, revision))
            return {**dict(row), "components": json.loads(row["components"])}

    @staticmethod
    def add_revision(db, batch, revision):
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?)",
            (
                batch,
                revision["id"],
                revision["directory"],
                revision["digest"],
                json.dumps(revision["components"]),
            ),
        )

    def create(self, owner, key, fingerprint, batch, revision):
        with self.transaction() as db:
            previous = self.prior(db, owner, "create", "", key, fingerprint)
            if previous:
                return previous
            db.execute(
                "INSERT INTO batches(id,workspace,namespace,name,active,index_state,lifecycle) VALUES (?,?,?,?,?,'unprovisioned','registered')",
                (
                    batch["id"],
                    batch["workspace"],
                    batch["namespace"],
                    batch["name"],
                    revision["id"],
                ),
            )
            self.add_revision(db, batch["id"], revision)
            result = self.row(db, batch["id"])
            self.remember(db, owner, "create", "", key, fingerprint, result)
            return result

    def publish(
        self, owner, key, fingerprint, batch, expected, revision, operation, restart
    ):
        with self.transaction() as db:
            previous = self.prior(db, owner, "publish", batch, key, fingerprint)
            if previous:
                return previous
            current = self.row(db, batch)
            if current["active"] != expected or current["pending"]:
                raise Conflict(
                    "Configuration version changed or an apply operation is pending"
                )
            if revision["id"] == expected:
                result = {
                    "batch": batch,
                    "revision": expected,
                    "operation": None,
                    "status": "unchanged",
                    "requires_restart": False,
                }
            else:
                self.add_revision(db, batch, revision)
                status = "pending_restart" if restart else "applied"
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?,?,?)",
                    (operation, batch, revision["id"], status, int(restart)),
                )
                if restart:
                    db.execute(
                        "UPDATE batches SET desired=?,pending=? WHERE id=?",
                        (revision["id"], operation, batch),
                    )
                else:
                    db.execute(
                        "UPDATE batches SET active=?,desired=NULL WHERE id=?",
                        (revision["id"], batch),
                    )
                result = {
                    "batch": batch,
                    "revision": revision["id"],
                    "operation": operation,
                    "status": status,
                    "requires_restart": restart,
                }
            self.remember(db, owner, "publish", batch, key, fingerprint, result)
            return result

    def enqueue(self, owner, key, fingerprint, batch, expected, task):
        with self.transaction() as db:
            previous = self.prior(db, owner, "task", batch, key, fingerprint)
            if previous:
                return previous
            current = self.row(db, batch)
            if current["active"] != expected or current["pending"]:
                raise Conflict("Configuration changed or admission is closed for apply")
            # Registration freezes identity; no execution slot is acquired here.
            task = {
                **task,
                "data_batch_id": batch,
                "config_revision_id": current["active"],
                "state": "queued",
            }
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?)",
                (task["task_id"], batch, current["active"], json.dumps(task)),
            )
            self.remember(db, owner, "task", batch, key, fingerprint, task)
            return task

    def referenced_directories(self):
        with self.transaction() as db:
            return {row[0] for row in db.execute("SELECT directory FROM revisions")}

    def operation(self, batch, operation):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE batch=? AND id=?", (batch, operation)
            ).fetchone()
            if row is None:
                raise KeyError(operation)
            return {
                "id": row["id"],
                "data_batch_id": batch,
                "config_revision_id": row["revision"],
                "status": row["status"],
                "requires_restart": bool(row["requires_restart"]),
            }

    def task(self, batch, task):
        with self.transaction() as db:
            row = db.execute(
                "SELECT record FROM tasks WHERE batch=? AND id=?", (batch, task)
            ).fetchone()
            if row is None:
                raise KeyError(task)
            return json.loads(row[0])
