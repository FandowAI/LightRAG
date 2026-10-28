"""Workspace Prompt documents; edits publish immutable files, never mutate a task."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from string import Template

from .config import BridgeSettings
from .recipe_schema import PROMPT_VARIABLES, STAGES, WORKFLOW, probe_schema

MAX_PROMPT_BYTES = 256 * 1024


def publish_text(directory: Path, name: str, content: str) -> Path:
    """Publish a complete UTF-8 document once; identical content may be reused."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    # A hard-link publishes only after the complete temporary file is durable.
    from tempfile import NamedTemporaryFile

    temporary = None
    try:
        with NamedTemporaryFile(
            "w", dir=directory, encoding="utf-8", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_text(encoding="utf-8") != content:
                raise ValueError("An immutable configuration file has changed")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def validate_prompt(stage: str, content: str) -> tuple[Template, dict]:
    if stage not in STAGES:
        raise ValueError("Unknown workflow stage")
    if not content.strip() or len(content.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("Prompt must be nonempty UTF-8 text, at most 256 KiB")
    template = Template(content)
    if not template.is_valid():
        raise ValueError("Invalid Prompt template syntax; use $$ for a literal $")
    unknown = set(template.get_identifiers()) - set(PROMPT_VARIABLES[stage])
    if unknown:
        raise ValueError("Unknown Prompt variables: " + ", ".join(sorted(unknown)))
    preview = {name: f"[{name}]" for name in PROMPT_VARIABLES[stage]}
    preview["text"] = "预览原文：采购下单须经过审批。"
    preview["payload"] = json.dumps(
        {key: value for key, value in preview.items() if key != "payload"},
        ensure_ascii=False,
    )
    return template, preview


class PromptFiles:
    def __init__(self, settings: BridgeSettings, namespace: str):
        self.base = settings.workflow_config.parent / "prompts"
        self.root = self.base / namespace
        self.defaults = self.base / "defaults"

    def read(self, stage: str, path: str) -> dict:
        if stage not in STAGES:
            raise ValueError("Unknown workflow stage")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.base.parent / candidate
        resolved = candidate.resolve()
        roots = (self.root.resolve(), self.defaults.resolve())
        if not any(resolved.is_relative_to(root) for root in roots):
            raise ValueError("Select a Prompt from this workspace's managed directory")
        if (
            candidate.is_symlink()
            or not resolved.is_file()
            or resolved.suffix.lower() not in {".md", ".txt"}
        ):
            raise ValueError("Prompt must be a managed regular file")
        if resolved.stat().st_size > MAX_PROMPT_BYTES:
            raise ValueError("Prompt exceeds 256 KiB")
        content = resolved.read_text(encoding="utf-8")
        template, preview = validate_prompt(stage, content)
        return {
            "stage": stage,
            "path": str(resolved),
            "filename": resolved.name,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
            "preview": template.substitute(preview),
            "variables": list(PROMPT_VARIABLES[stage]),
        }

    def list(self, stage: str) -> dict:
        if stage not in STAGES:
            raise ValueError("Unknown workflow stage")
        paths = [*self.defaults.glob(f"{stage}*.md"), *self.root.glob(f"{stage}-*.md")]
        return {
            "directory": str(self.root),
            "items": [
                {"path": str(path), "filename": path.name}
                for path in sorted(paths)
                if path.is_file() and not path.is_symlink()
            ],
        }

    def save(self, stage: str, filename: str, content: str) -> dict:
        if Path(filename).name != filename or Path(filename).suffix.lower() not in {
            ".md",
            ".txt",
        }:
            raise ValueError("Upload a .md or .txt file with a simple filename")
        validate_prompt(stage, content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = publish_text(self.root, f"{stage}-{digest}.md", content)
        return self.read(stage, str(path))


def materialize_builtin_prompts(settings: BridgeSettings, recipe: dict) -> dict:
    """Resolve selected built-ins now, so queued tasks do not read later source edits."""
    if recipe.get("workflow", {}).get("name") != WORKFLOW:
        return recipe
    parameters = recipe["workflow"]["parameters"]
    missing = [
        name
        for name in STAGES
        if name in parameters and not parameters[name].get("prompt_file")
    ]
    if not missing:
        return recipe
    defaults = probe_schema(settings, "prompt_defaults", recipe)
    if "prompts" not in defaults:
        raise ValueError("Cannot read installed built-in Prompts")
    for name in missing:
        content = defaults["prompts"][name]
        validate_prompt(name, content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = publish_text(
            settings.workflow_config.parent / "prompts/defaults",
            f"{name}-{digest}.md",
            content,
        )
        parameters[name]["prompt_file"] = str(path)
    return recipe
