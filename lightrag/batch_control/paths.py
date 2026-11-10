"""Explicit server-owned roots; recipe location never determines global storage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from .contracts import Name


@dataclass(frozen=True)
class ControlPaths:
    config_root: Path
    runtime_root: Path

    def __post_init__(self):
        for root in (self.config_root, self.runtime_root):
            if not root.is_absolute() or root != root.resolve():
                raise ValueError(
                    "Control roots must be absolute and must not traverse symlinks"
                )

    def under(self, root: Path, *parts: str) -> Path:
        for part in parts:
            TypeAdapter(Name).validate_python(part, strict=True)
        path = root.joinpath(*parts)
        if not path.resolve().is_relative_to(root):
            raise ValueError("Resource path escapes its configured root")
        return path

    def revision(self, batch: str, revision: str) -> Path:
        return self.under(self.config_root, "batches", batch, "revisions", revision)

    def workspace_settings(self, namespace: str) -> Path:
        return self.under(self.config_root, "workspaces", namespace)

    def prompts(self, namespace: str) -> Path:
        return self.under(self.config_root, "prompts", namespace)

    def task_config(self, namespace: str, task: str) -> Path:
        return self.under(self.config_root, "jobs", namespace, task)

    @property
    def registry(self) -> Path:
        return self.under(self.runtime_root, "batch-control", "control.sqlite3")
