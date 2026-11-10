"""Persist task admission, generation ownership and progress in the control registry."""

from __future__ import annotations

import json
import time

from .resources import digest
from .storage import Conflict, IntegrityError

TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}
EXECUTING = {"admitted", "running"}


class TaskStore:
    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def _read(db, batch, task):
        row = db.execute(
            "SELECT record FROM tasks WHERE batch=? AND id=?", (batch, task)
        ).fetchone()
        if row is None:
            raise KeyError(task)
        return json.loads(row[0])

    @staticmethod
    def _write(db, task, event):
        task["updated_at"] = time.time()
        db.execute(
            "UPDATE tasks SET record=? WHERE batch=? AND id=?",
            (json.dumps(task), task["data_batch_id"], task["task_id"]),
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS task_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT NOT NULL, task TEXT NOT NULL, record TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO task_events(batch,task,record) VALUES (?,?,?)",
            (
                task["data_batch_id"],
                task["task_id"],
                json.dumps(
                    {
                        "event": event,
                        "state": task["state"],
                        "phase": task.get("phase"),
                        "time": task["updated_at"],
                        "generation": task.get("generation"),
                        "actor": task.get("actor"),
                    }
                ),
            ),
        )

    def list(self, batch=None, *, states=None):
        with self.registry.transaction() as db:
            rows = (
                db.execute(
                    "SELECT record FROM tasks WHERE batch=? ORDER BY rowid", (batch,)
                )
                if batch
                else db.execute("SELECT record FROM tasks ORDER BY rowid")
            )
            return [
                task
                for row in rows
                if (task := json.loads(row[0])).get("payload", {}).get("version") == 1
                and (states is None or task["state"] in states)
            ]

    def get(self, batch, task):
        return self.registry.task(batch, task)

    def admit(self, batch, task_id, lease):
        with self.registry.transaction() as db:
            task = self._read(db, batch, task_id)
            if task["state"] != "queued":
                return task
            if self.registry.row(db, batch)["pending"]:
                raise Conflict("Task admission is closed for configuration application")
            live = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(live[0]) if live else {}
            if (
                runtime.get("state") != "ready"
                or runtime.get("generation") != lease.generation
                or runtime.get("lease_id") != lease.lease_id
            ):
                raise Conflict("Runtime changed before task admission")
            if task["components"]["rag"] != runtime["rag_digest"]:
                raise Conflict("Queued task requires a different RAG startup revision")
            active = db.execute("SELECT record FROM tasks WHERE batch=?", (batch,))
            if any(json.loads(row[0])["state"] in EXECUTING for row in active):
                raise Conflict("Another task owns this batch runtime")
            task.update(
                state="admitted",
                phase="waiting_for_worker",
                generation=lease.generation,
                runtime_lease_id=lease.lease_id,
                startup_digest=lease.startup_digest,
            )
            self._write(db, task, "admitted")
            return task

    def claim(self, batch, generation, startup_digest, pid, identity):
        with self.registry.transaction() as db:
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            if (
                runtime.get("state"),
                runtime.get("generation"),
                runtime.get("startup_digest"),
                runtime.get("pid"),
                runtime.get("process_identity"),
            ) != ("ready", generation, startup_digest, pid, identity):
                return None
            for row in db.execute(
                "SELECT record FROM tasks WHERE batch=? ORDER BY rowid", (batch,)
            ):
                task = json.loads(row[0])
                if task["state"] != "admitted" or task.get("generation") != generation:
                    continue
                if task.get("payload_digest") != digest(task.get("payload")):
                    raise IntegrityError("Admitted task payload changed")
                if (
                    task.get("runtime_lease_id") != runtime["lease_id"]
                    or task.get("startup_digest") != startup_digest
                ):
                    raise IntegrityError("Task does not own this runtime generation")
                task.update(state="running", phase="starting", started_at=time.time())
                self._write(db, task, "claimed")
                return task
            return None

    def update(self, batch, task_id, *, generation=None, event="progress", **values):
        with self.registry.transaction() as db:
            task = self._read(db, batch, task_id)
            if task["state"] in TERMINAL:
                return task
            if generation is not None and task.get("generation") != generation:
                raise Conflict("Task belongs to another runtime generation")
            task.update(values)
            if task["state"] in TERMINAL:
                task["finished_at"] = time.time()
            self._write(db, task, event)
            return task

    def cancel(self, batch, task):
        with self.registry.transaction() as db:
            record = self._read(db, batch, task)
            if record["state"] in TERMINAL:
                return record
            record["cancel_requested"] = True
            if record["state"] in {"queued", "admitted"}:
                record.update(
                    state="cancelled", phase="cancelled", finished_at=time.time()
                )
            self._write(db, record, "cancel_requested")
            return record

    def events(self, batch, task):
        with self.registry.transaction() as db:
            self._read(db, batch, task)
            if (
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='task_events'"
                ).fetchone()
                is None
            ):
                return []
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM task_events WHERE batch=? AND task=? ORDER BY sequence",
                    (batch, task),
                )
            ]


def public_task(task):
    return {
        "schema_version": 1,
        **{
            key: task.get(key)
            for key in (
                "task_id",
                "data_batch_id",
                "config_revision_id",
                "kind",
                "state",
                "phase",
                "generation",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "cancel_requested",
                "progress",
                "result",
                "error",
                "waiting_reason",
            )
        },
        "terminal": task["state"] in TERMINAL,
        "poll_after_ms": 0 if task["state"] in TERMINAL else 2000,
    }
