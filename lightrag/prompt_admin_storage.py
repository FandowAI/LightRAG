"""SQLite persistence for the optional prompt administration overlay."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class StalePromptRevision(ValueError):
    """The database revision changed before a prompt write could commit."""


class PromptSqliteStore:
    def __init__(self, working_dir: str, workspace: str, legacy_path: Path):
        self.workspace = workspace or "default"
        self.path = Path(working_dir) / "prompt_admin" / "prompts.sqlite3"
        self.legacy_path = legacy_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def initialize(self, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS prompt_state (
                    workspace TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prompt_templates (
                    workspace TEXT NOT NULL,
                    prompt_key TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    PRIMARY KEY (workspace, prompt_key, variant)
                );
                CREATE TABLE IF NOT EXISTS prompt_active (
                    workspace TEXT NOT NULL,
                    prompt_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    PRIMARY KEY (workspace, prompt_key)
                );
                CREATE TABLE IF NOT EXISTS prompt_drafts (
                    workspace TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    base_json TEXT NOT NULL,
                    PRIMARY KEY (workspace, group_id)
                );
                CREATE TABLE IF NOT EXISTS prompt_history (
                    workspace TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    PRIMARY KEY (workspace, revision)
                );
                CREATE INDEX IF NOT EXISTS prompt_history_group_idx
                    ON prompt_history (workspace, group_id, revision DESC);
                """
            )
            existing = connection.execute(
                "SELECT revision FROM prompt_state WHERE workspace = ?", (self.workspace,)
            ).fetchone()
            if existing is None:
                state: dict[str, Any] = {
                    "revision": 0, "active": {}, "drafts": {}, "draft_bases": {}, "history": []
                }
                if self.legacy_path.exists():
                    loaded = json.loads(self.legacy_path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict) or not isinstance(loaded.get("active"), dict):
                        raise ValueError("Invalid legacy prompt editor profile")
                    state = {
                        "revision": int(loaded.get("revision", 0)),
                        "active": loaded["active"],
                        "drafts": loaded.get("drafts", {}),
                        "draft_bases": loaded.get("draft_bases", {}),
                        "history": loaded.get("history", []),
                    }
                self._insert_initial_state(connection, state)
            # These three variants are snapshots of the shipped/configured templates.
            # Refreshing them at startup lets upstream template updates take effect.
            for variant, fields in templates.items():
                connection.executemany(
                    """INSERT INTO prompt_templates (workspace, prompt_key, variant, value_json)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (workspace, prompt_key, variant)
                       DO UPDATE SET value_json = excluded.value_json""",
                    (
                        (self.workspace, key, variant, self._encode(value))
                        for key, value in fields.items()
                    ),
                )
        return self.load()

    def _insert_initial_state(self, connection: sqlite3.Connection, state: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO prompt_state (workspace, revision) VALUES (?, ?)",
            (self.workspace, state["revision"]),
        )
        connection.executemany(
            "INSERT INTO prompt_active (workspace, prompt_key, value_json) VALUES (?, ?, ?)",
            (
                (self.workspace, key, self._encode(value))
                for key, value in state["active"].items()
            ),
        )
        connection.executemany(
            """INSERT INTO prompt_drafts (workspace, group_id, fields_json, base_json)
               VALUES (?, ?, ?, ?)""",
            (
                (self.workspace, group, self._encode(fields), self._encode(state["draft_bases"].get(group, {})))
                for group, fields in state["drafts"].items()
            ),
        )
        connection.executemany(
            """INSERT INTO prompt_history
               (workspace, revision, at, action, group_id, actor, fields_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self._history_row(entry) for entry in state["history"]),
        )

    def _history_row(self, entry: dict[str, Any]) -> tuple[Any, ...]:
        return (
            self.workspace, entry["revision"], entry["at"], entry["action"],
            entry["group"], entry["actor"], self._encode(entry["fields"]),
        )

    def load(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT revision FROM prompt_state WHERE workspace = ?", (self.workspace,)
            ).fetchone()
            if row is None:
                raise ValueError("Prompt database has no workspace state")
            active = {
                item["prompt_key"]: json.loads(item["value_json"])
                for item in connection.execute(
                    "SELECT prompt_key, value_json FROM prompt_active WHERE workspace = ?", (self.workspace,)
                )
            }
            drafts: dict[str, Any] = {}
            bases: dict[str, Any] = {}
            for item in connection.execute(
                "SELECT group_id, fields_json, base_json FROM prompt_drafts WHERE workspace = ?",
                (self.workspace,),
            ):
                drafts[item["group_id"]] = json.loads(item["fields_json"])
                bases[item["group_id"]] = json.loads(item["base_json"])
            history = [
                {
                    "revision": item["revision"], "at": item["at"],
                    "action": item["action"], "group": item["group_id"],
                    "actor": item["actor"], "fields": json.loads(item["fields_json"]),
                }
                for item in connection.execute(
                    """SELECT revision, at, action, group_id, actor, fields_json
                       FROM prompt_history WHERE workspace = ? ORDER BY revision""",
                    (self.workspace,),
                )
            ]
            return {
                "revision": row["revision"], "active": active,
                "drafts": drafts, "draft_bases": bases, "history": history,
            }

    def write(self, previous: dict[str, Any], state: dict[str, Any]) -> None:
        old_history = previous["history"]
        if state["history"][:len(old_history)] != old_history:
            raise ValueError("Prompt history must remain append-only")
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE prompt_state SET revision = ?
                   WHERE workspace = ? AND revision = ?""",
                (state["revision"], self.workspace, previous["revision"]),
            )
            if cursor.rowcount != 1:
                raise StalePromptRevision("Prompt profile changed; reload before editing")
            for key in previous["active"].keys() - state["active"].keys():
                connection.execute(
                    "DELETE FROM prompt_active WHERE workspace = ? AND prompt_key = ?",
                    (self.workspace, key),
                )
            for key, value in state["active"].items():
                if key not in previous["active"] or previous["active"][key] != value:
                    connection.execute(
                        """INSERT INTO prompt_active (workspace, prompt_key, value_json)
                           VALUES (?, ?, ?)
                           ON CONFLICT (workspace, prompt_key)
                           DO UPDATE SET value_json = excluded.value_json""",
                        (self.workspace, key, self._encode(value)),
                    )
            for group in previous["drafts"].keys() - state["drafts"].keys():
                connection.execute(
                    "DELETE FROM prompt_drafts WHERE workspace = ? AND group_id = ?",
                    (self.workspace, group),
                )
            for group, fields in state["drafts"].items():
                base = state["draft_bases"].get(group, {})
                if fields != previous["drafts"].get(group) or base != previous["draft_bases"].get(group):
                    connection.execute(
                        """INSERT INTO prompt_drafts (workspace, group_id, fields_json, base_json)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT (workspace, group_id) DO UPDATE SET
                             fields_json = excluded.fields_json,
                             base_json = excluded.base_json""",
                        (self.workspace, group, self._encode(fields), self._encode(base)),
                    )
            connection.executemany(
                """INSERT INTO prompt_history
                   (workspace, revision, at, action, group_id, actor, fields_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self._history_row(entry) for entry in state["history"][len(old_history):]),
            )
