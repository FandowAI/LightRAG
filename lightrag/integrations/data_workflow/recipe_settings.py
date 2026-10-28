"""Workspace recipe overrides; validate with the installed workflow before saving."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

from .config import BridgeSettings, UniqueKeyLoader
from .storage import JobConflict
from .recipe_schema import (
    PARAMETER_FIELDS,
    RUNTIME_FIELDS,
    STAGES,
    WORKFLOW,
    probe_schema,
)


class StageValues(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    parser: str = Field(min_length=1)
    prompt_file: str | None = None


class RecipeValues(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    questions_per_source: int = Field(default=3, gt=0)
    deduplicate_qa: bool = False
    scenario_instructions: str = Field(
        default="生成有来源依据的企业工作问题场景。", min_length=1, max_length=16000
    )
    classification_instructions: str = Field(
        default="分别判断业务重要性和核实难度，并从给定目录选择一个主题。",
        min_length=1,
        max_length=16000,
    )
    inspection_instructions: str = Field(
        default="依据原文检查原始问答；有可靠依据时修正问题或答案，不能可靠修正时保留原文并说明原因。",
        min_length=1,
        max_length=16000,
    )
    seed_question_path: list[str | int] | None = None
    batch_size: int = Field(default=1, gt=0)
    max_inflight_requests: int = Field(default=2, gt=0)
    max_prompt_chars: int | None = Field(default=None, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    models: dict[str, dict[str, Any]] = Field(default_factory=dict)
    stages: dict[
        Literal["scenario", "qa", "classification", "inspection"], StageValues
    ] = Field(default_factory=dict)


def apply_values(recipe: dict, values: RecipeValues) -> dict:
    document = deepcopy(recipe)
    parameters = document.setdefault("workflow", {}).setdefault("parameters", {})
    for name in PARAMETER_FIELDS:
        parameters[name] = getattr(values, name)
    for name in RUNTIME_FIELDS:
        document.setdefault("runtime", {})[name] = getattr(values, name)
    document["models"] = deepcopy(values.models)
    for name, task in values.stages.items():
        parameters[name] = task.model_dump()
    return document


class RecipeSettingsStore:
    """Keep defaults in YAML and workspace changes in a separate versioned file."""

    def __init__(self, settings: BridgeSettings, root: Path):
        self.settings = settings
        self.root = settings.workflow_config.parent / "workspaces" / root.name
        self.path = self.root / "recipe-settings.json"

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "recipe-settings.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        raw = self.settings.workflow_config.read_bytes()
        overrides = self.path.read_bytes() if self.path.exists() else b""
        recipe = yaml.load(raw, Loader=UniqueKeyLoader)
        if not isinstance(recipe, dict):
            raise ValueError("Workflow recipe must be a YAML object")
        if overrides:
            saved = json.loads(overrides)
            version = saved.get("schema_version")
            if version not in {2, 3, 4}:
                raise ValueError("Unsupported recipe settings version")
            values = saved["values"]
            if recipe.get("workflow", {}).get("name") == WORKFLOW:
                if version < 4:
                    # Old workflow-only controls and defaults must not enter the new recipe.
                    values = {
                        name: value
                        for name, value in values.items()
                        if name in RUNTIME_FIELDS
                    }
                defaults = self._values(recipe)
                recipe = apply_values(
                    recipe, RecipeValues.model_validate({**defaults, **values})
                )
        return recipe, hashlib.sha256(raw + b"\0" + overrides).hexdigest()

    def effective(self) -> dict:
        with self.locked():
            return self._read()[0]

    def _values(self, recipe: dict, defaults: dict | None = None) -> dict:
        parameters = recipe.get("workflow", {}).get("parameters", {})
        runtime = recipe.get("runtime", {})
        defaults = {**RecipeValues().model_dump(), **(defaults or {})}
        values = {
            name: parameters.get(name, defaults[name]) for name in PARAMETER_FIELDS
        }
        values.update(
            {name: runtime.get(name, defaults[name]) for name in RUNTIME_FIELDS}
        )
        values["models"] = deepcopy(recipe.get("models", {}))
        values["stages"] = {
            name: parameters[name] for name in STAGES if name in parameters
        }
        return values

    def _view(self, recipe: dict, revision: str, schema: dict) -> dict:
        workflow_name = recipe.get("workflow", {}).get("name")
        editable = (
            recipe.get("schema_version") == schema["schema_version"] == 3
            and workflow_name == WORKFLOW
        )
        return {
            "schema_version": 2,
            "revision": revision,
            "editable": editable,
            "recipe_schema_version": recipe.get("schema_version"),
            "installed_schema_version": schema["schema_version"],
            "installed_commit": schema.get("installed_commit"),
            "workflow_name": workflow_name,
            "settings": RecipeValues.model_validate(
                self._values(recipe, schema.get("defaults"))
            ).model_dump(),
            "recipe_file": str(self.settings.workflow_config),
            "settings_file": str(self.path),
            "model_schema": schema.get("model_schema", {}),
            "task_schema": schema.get("task_schema", {}),
            "models": {
                name: value.get("model_name", "")
                for name, value in recipe.get("models", {}).items()
            },
        }

    def get(self) -> dict:
        schema = probe_schema(self.settings, "describe")
        with self.locked():
            return self._view(*self._read(), schema)

    def save(self, revision: str, values: RecipeValues) -> dict:
        schema = probe_schema(self.settings, "describe")
        with self.locked():
            recipe, current = self._read()
            if current != revision:
                raise JobConflict("Recipe settings changed; reload before saving")
            if not self._view(recipe, current, schema)["editable"]:
                raise ValueError(
                    "The installed workflow and recipe schema must both support difficulty_routed_qa version 3"
                )
            if set(values.stages) != set(STAGES):
                raise ValueError("All four workflow stages must be configured")
            from .prompt_files import PromptFiles

            prompts = PromptFiles(self.settings, self.root.name)
            for name, task in values.stages.items():
                if task.prompt_file:
                    prompts.read(name, task.prompt_file)
            candidate = apply_values(recipe, values)
            validation = probe_schema(self.settings, "validate", candidate)
            if not validation["valid"]:
                raise ValueError("; ".join(validation["issues"]))
            if self._read()[1] != revision:
                raise JobConflict(
                    "Recipe defaults changed during validation; reload before saving"
                )
            payload = {
                "schema_version": 4,
                "workflow_name": WORKFLOW,
                "values": values.model_dump(),
            }
            temporary = None
            try:
                with NamedTemporaryFile(
                    "w", dir=self.root, delete=False, encoding="utf-8"
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return self._view(*self._read(), schema)
