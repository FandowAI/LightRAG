"""Operator-owned bridge settings; requests cannot select programs or credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "data_workflow.yaml"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InputSettings(StrictModel):
    paths: list[str] = Field(default_factory=list)
    records_path: list[str | int] = Field(default_factory=list)


class Limits(StrictModel):
    max_input_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    max_records: int = Field(default=10_000, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_active_jobs: int = Field(default=1, gt=0)


class BridgeSettings(StrictModel):
    version: Literal[1] = 1
    project_directory: Path
    python: Path
    workflow_config: Path
    credentials_file: Path | None = None
    jobs_directory: Path
    input: InputSettings = Field(default_factory=InputSettings)
    limits: Limits = Field(default_factory=Limits)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate configuration keys rather than silently losing settings."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping
)


def read_yaml(path: Path) -> dict:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a YAML object: {path}")
    return value


def configuration_path() -> Path:
    return Path(
        os.getenv("LIGHTRAG_DATA_WORKFLOW_CONFIG", str(DEFAULT_CONFIG))
    ).resolve()


def load_settings(path: Path) -> BridgeSettings:
    document = read_yaml(path)

    def absolute(value, *, follow_symlinks=True):
        candidate = Path(value).expanduser()
        target = path.parent / candidate
        # Resolving a venv's python symlink loses that virtual environment.
        return str(
            target.resolve() if follow_symlinks else Path(os.path.abspath(target))
        )

    for key in (
        "project_directory",
        "python",
        "workflow_config",
        "jobs_directory",
        "credentials_file",
    ):
        if document.get(key) is not None:
            document[key] = absolute(document[key], follow_symlinks=key != "python")
    inputs = dict(document.get("input") or {})
    inputs["paths"] = [absolute(value) for value in inputs.get("paths", [])]
    document["input"] = inputs
    return BridgeSettings.model_validate(document)


def write_job_config(template: Path, directory: Path) -> dict:
    """Snapshot the recipe, preserving the original base for Prompt resources."""
    document = read_yaml(template)
    if document.get("schema_version") != 3:
        raise ValueError("The data-work-flow recipe must use schema_version: 3")

    def rebase(value):
        if isinstance(value, dict):
            return {
                key: str((template.parent / Path(item).expanduser()).resolve())
                if key == "prompt_file" and item is not None
                else rebase(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rebase(item) for item in value]
        return value

    document = rebase(document)
    document["input"] = {"paths": [str(directory / "input.jsonl")]}
    document["output"] = {"directory": str(directory / "outputs")}
    fields = document.setdefault("workflow", {}).setdefault("parameters", {})
    # The bridge owns the input envelope; do not inherit a different field mapping.
    if document["workflow"].get("name") == "source_review":
        fields.update(text_path=["text"], meta_path=["meta"])
    else:
        fields.setdefault("fields", {}).update(text_path=["text"], meta_path=["meta"])
    (directory / "job.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return document
