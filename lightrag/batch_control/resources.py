"""Authorized Prompt capture and immutable configuration directories with file hashes."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

import yaml

from .contracts import Configuration
from .paths import ControlPaths
from .storage import IntegrityError
from ..integrations.data_workflow.config import read_yaml
from ..integrations.data_workflow.prompt_files import MAX_PROMPT_BYTES, validate_prompt
from ..integrations.data_workflow.recipe_schema import STAGES


def canonical(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class VersionFiles:
    def __init__(self, paths: ControlPaths):
        self.paths = paths

    def capture(
        self, recipe: dict, batch: dict, inherited: dict[str, str] | None, builtin
    ) -> dict[str, str]:
        result = {}
        for stage in STAGES:
            task = recipe["workflow"]["parameters"][stage]
            if inherited is not None and stage in inherited:
                content = inherited[stage]
            elif task.get("prompt_file"):
                path = Path(task["prompt_file"])
                if not path.is_absolute():
                    raise ValueError(
                        "Draft Prompt paths must be managed absolute references"
                    )
                allowed = (
                    self.paths.prompts(batch["namespace"]),
                    self.paths.prompts("defaults"),
                )
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not any(
                        path.resolve().is_relative_to(root.resolve())
                        for root in allowed
                    )
                ):
                    raise PermissionError("Prompt is not owned by the target batch")
                if path.stat().st_size > MAX_PROMPT_BYTES:
                    raise ValueError("Prompt exceeds 256 KiB")
                content = path.read_text(encoding="utf-8")
            else:
                content = builtin(stage)
            validate_prompt(stage, content)
            result[stage] = content
        return result

    @staticmethod
    def components(document: Configuration, prompts: dict, execution: dict) -> dict:
        recipe = deepcopy(document.recipe)
        parameters = recipe["workflow"]["parameters"]
        if parameters.get("topic_catalog_file"):
            parameters["topic_catalog_file"] = (
                "sha256:"
                + hashlib.sha256(
                    Path(parameters["topic_catalog_file"]).read_bytes()
                ).hexdigest()
            )
        for stage in STAGES:
            recipe["workflow"]["parameters"][stage]["prompt_file"] = (
                "sha256:" + hashlib.sha256(prompts[stage].encode()).hexdigest()
            )
        return {
            "rag": digest(
                {
                    "configuration": document.rag.model_dump(),
                    "profiles": execution["rag_profiles"],
                }
            ),
            "retrieval": digest(document.retrieval),
            "recipe": digest(
                {"configuration": recipe, "environment": execution["environment"]}
            ),
            "bridge": digest(document.bridge.model_dump()),
        }

    def publish(
        self,
        batch: dict,
        revision: str,
        document: Configuration,
        prompts: dict,
        execution: dict,
        bridge: dict,
        sources: dict,
    ) -> dict:
        from .bindings import BindingStore
        from .storage import Registry

        binding = (
            BindingStore(Registry(self.paths.registry)).get(batch["id"])
            if self.paths.registry.is_file()
            else None
        )
        sources = dict(sources)
        if binding:
            sources["storage_binding_digest"] = binding["digest"]
        final = self.paths.revision(batch["id"], revision)
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=final.parent))
        try:
            doc = document.model_dump()
            payloads = {}
            parameters = doc["recipe"]["workflow"]["parameters"]
            if parameters.get("topic_catalog_file"):
                topic_path = (
                    self.paths.config_root
                    / Path(parameters["topic_catalog_file"]).expanduser()
                )
                content = topic_path.read_bytes()
                name = (
                    "resources/topics/categories-"
                    + hashlib.sha256(content).hexdigest()
                    + ".yaml"
                )
                payloads[name] = content
                parameters["topic_catalog_file"] = str(final / name)
            for stage, content in prompts.items():
                name = f"resources/prompts/{stage}-{hashlib.sha256(content.encode()).hexdigest()}.md"
                payloads[name] = content.encode("utf-8")
                doc["recipe"]["workflow"]["parameters"][stage]["prompt_file"] = str(
                    final / name
                )
            for name, value in (
                ("rag.yaml", doc["rag"]),
                ("retrieval.yaml", doc["retrieval"]),
                ("data_workflow_job.yaml", doc["recipe"]),
                (
                    "data_workflow.yaml",
                    {
                        **bridge,
                        "workflow_config": str(final / "data_workflow_job.yaml"),
                    },
                ),
            ):
                payloads[name] = yaml.safe_dump(
                    value, sort_keys=False, allow_unicode=True
                ).encode("utf-8")
            artifacts = {
                name: hashlib.sha256(data).hexdigest()
                for name, data in payloads.items()
            }
            manifest = {
                "schema_version": 1,
                "data_batch_id": batch["id"],
                "config_revision_id": revision,
                "workflow": doc["recipe"]["workflow"]["name"],
                "job_schema_version": 3,
                "bridge_references": doc["bridge"],
                "execution": execution,
                "sources": sources,
                "components": self.components(document, prompts, execution),
                "artifacts": artifacts,
                "validation_scope": "workflow_preparation_without_model_requests",
            }
            payloads["manifest.json"] = canonical(manifest)
            for name, data in payloads.items():
                path = temporary / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as handle:
                    os.chmod(path, 0o600)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            for path in sorted(
                {temporary, *(p.parent for p in temporary.rglob("*") if p.is_file())},
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.rename(temporary, final)
            fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return {
                "id": revision,
                "directory": str(final),
                "digest": hashlib.sha256(payloads["manifest.json"]).hexdigest(),
                "components": manifest["components"],
            }
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def read(self, batch: dict, record: dict) -> tuple[Configuration, dict, dict]:
        directory = self.paths.revision(batch["id"], record["id"])
        if directory != Path(record["directory"]):
            raise IntegrityError(
                "Registered configuration directory does not match its owner"
            )
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            raise IntegrityError("Manifest must be a regular file")
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["digest"]:
            raise IntegrityError("Configuration manifest changed after registration")
        manifest = json.loads(raw)
        if (
            manifest["data_batch_id"] != batch["id"]
            or manifest["config_revision_id"] != record["id"]
            or manifest["components"] != record["components"]
        ):
            raise IntegrityError("Manifest identity or component digests disagree")
        for name, expected in manifest["artifacts"].items():
            path = directory / name
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(directory.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise IntegrityError("Published configuration resource changed")
        recipe = read_yaml(directory / "data_workflow_job.yaml")
        document = Configuration.model_validate(
            {
                "rag": read_yaml(directory / "rag.yaml"),
                "retrieval": read_yaml(directory / "retrieval.yaml"),
                "recipe": recipe,
                "bridge": manifest["bridge_references"],
            }
        )
        prompts = {}
        for stage in STAGES:
            path = Path(recipe["workflow"]["parameters"][stage]["prompt_file"])
            if str(path.relative_to(directory)) not in manifest["artifacts"]:
                raise IntegrityError("Prompt is not part of this revision")
            prompts[stage] = path.read_text(encoding="utf-8")
        return document, prompts, manifest

    def orphans(self, referenced: set[str]) -> list[str]:
        root = self.paths.config_root / "batches"
        return sorted(
            str(p)
            for p in root.glob("*/revisions/*")
            if p.is_dir() and not p.name.startswith(".") and str(p) not in referenced
        )
