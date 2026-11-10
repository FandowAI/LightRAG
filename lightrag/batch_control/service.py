"""Offline configuration lifecycle; execution and HTTP mounting belong to later phases."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import time
from pathlib import Path
from typing import Callable, Literal

from pydantic import TypeAdapter

from .contracts import (
    Configuration,
    Name,
    TaskSource,
    identifier,
)
from .fields import copy_groups, differences, import_overrides, validate_recipe_shape
from .paths import ControlPaths
from .resources import VersionFiles, digest
from .storage import Conflict, Registry
from .references import Catalog, no_plaintext_secrets
from ..integrations.data_workflow.config import BridgeSettings
from ..integrations.data_workflow.recipe_schema import STAGES, WORKFLOW, probe_schema


class BatchControl:
    """Callers must supply authorization; this class never starts a RAG or workflow."""

    def __init__(
        self,
        paths: ControlPaths,
        defaults: Configuration,
        catalog: Catalog,
        authorize: Callable[[str, str, str], bool],
        *,
        probe: Callable = probe_schema,
    ):
        self.paths, self.catalog, self.authorize, self.probe = (
            paths,
            catalog,
            authorize,
            probe,
        )
        self.defaults = Configuration.model_validate(deepcopy(defaults.model_dump()))
        self.registry, self.files = Registry(paths.registry), VersionFiles(paths)
        from .provider_quota.core import configuration

        self.provider_limits = configuration({})
        from .training_export.service import ExportLimits

        self.training_export_limits = ExportLimits()

    def allow(self, actor, action, resource):
        TypeAdapter(Name).validate_python(actor, strict=True)
        if not self.authorize(actor, action, resource):
            raise PermissionError("Configuration access is not authorized")

    def read(self, actor: str, batch: str, revision: str | None = None):
        self.allow(actor, "read", batch)
        from .bindings import BindingStore

        for name, profile in BindingStore(self.registry).profiles().items():
            if name in self.catalog.profiles and self.catalog.profiles[name] != profile:
                raise Conflict(
                    "An adopted profile differs from its immutable registry record"
                )
            self.catalog.profiles[name] = profile
        owner = self.registry.get(batch)
        record = self.registry.revision(batch, revision or owner["active"])
        document, prompts, manifest = self.files.read(owner, record)
        return owner, record, document, prompts, manifest

    def _normalize(self, document: Configuration) -> Configuration:
        data = deepcopy(document.model_dump())
        recipe = data["recipe"]
        validate_recipe_shape(recipe)
        recipe = import_overrides(
            recipe, {"schema_version": 4, "workflow_name": WORKFLOW, "values": {}}
        )
        recipe["input"] = {"paths": ["<job-input>"]}
        recipe["output"] = {"directory": "<job-output>"}
        recipe["workflow"]["parameters"].setdefault("fields", {}).update(
            text_path=["text"], meta_path=["meta"]
        )
        parameters = recipe["workflow"]["parameters"]
        if parameters.get("topic_catalog_file"):
            parameters["topic_catalog_file"] = str(
                self.paths.config_root
                / Path(parameters["topic_catalog_file"]).expanduser()
            )
        data["recipe"] = recipe
        no_plaintext_secrets(data)
        return Configuration.model_validate(data)

    def _prepare(self, actor, owner, document, inherited):
        document = self._normalize(document)
        execution = self.catalog.execution(
            document, lambda action, reference: self.allow(actor, action, reference)
        )
        bridge = self.catalog.bridge(self.paths, owner, document)
        settings = BridgeSettings.model_validate(
            {
                **bridge,
                "workflow_config": self.paths.config_root / "data_workflow_job.yaml",
            }
        )
        described = self.probe(settings, "describe")
        installed = described.get("installed_commit")
        if not installed or not execution["environment"]["code_revision"].startswith(
            installed
        ):
            raise Conflict(
                "Selected checkout does not match its registered code revision"
            )
        cache = {}

        def builtin(stage):
            if not cache:
                result = self.probe(settings, "prompt_defaults", document.recipe)
                if "prompts" not in result:
                    raise ValueError("Cannot resolve selected built-in Prompts")
                cache.update(result["prompts"])
            return cache[stage]

        recipe = deepcopy(document.recipe)
        matched = {}
        for stage, (path, content) in inherited.items():
            if recipe["workflow"]["parameters"][stage].get("prompt_file") == path:
                matched[stage] = content
        prompts = self.files.capture(recipe, owner, matched, builtin)
        validation = self.probe(settings, "validate", recipe)
        if not validation.get("valid"):
            raise ValueError(
                "; ".join(validation.get("issues", ["Invalid workflow settings"]))
            )
        return document, prompts, execution, bridge

    @staticmethod
    def _inherit(document, prompts):
        return {
            stage: (
                document.recipe["workflow"]["parameters"][stage].get("prompt_file"),
                prompts[stage],
            )
            for stage in STAGES
        }

    def _copies(self, actor, target, copies):
        result, inherited, sources = target, {}, {}
        for group, reference in (copies or {}).items():
            if group not in {"rag", "recipe", "bridge"}:
                raise ValueError("Retrieval and resource identities cannot be copied")
            if set(reference) != {"batch", "revision"}:
                raise ValueError(
                    "Copy source must identify one batch and immutable revision"
                )
            _, _, source, prompts, manifest = self.read(
                actor, reference["batch"], reference["revision"]
            )
            # A versioned reference must still resolve to the same administrator snapshot.
            if (
                group == "rag"
                and self.catalog.profile_snapshot(source.rag)
                != manifest["execution"]["rag_profiles"]
            ):
                raise Conflict(
                    "Source model/profile reference changed; register a new reference"
                )
            if (
                group == "bridge"
                and self.catalog.environment_snapshot(source.bridge.environment_id)
                != manifest["execution"]["environment"]
            ):
                raise Conflict(
                    "Source environment reference changed; register a new reference"
                )
            result = copy_groups(result, source, [group])
            sources[group] = deepcopy(reference)
            if group == "recipe":
                inherited = self._inherit(source, prompts)
        return result, inherited, sources

    def create(
        self,
        actor: str,
        name: str,
        request_id: str,
        *,
        copies: dict | None = None,
        overrides: dict | None = None,
    ) -> dict:
        self.allow(actor, "create", "batches")
        TypeAdapter(Name).validate_python(request_id, strict=True)
        if not name.strip() or len(name) > 256:
            raise ValueError(
                "A nonblank batch name of at most 256 characters is required"
            )
        fingerprint = digest({"name": name, "copies": copies, "overrides": overrides})
        previous = self.registry.previous(actor, "create", "", request_id, fingerprint)
        if previous:
            self.allow(actor, "read", previous["id"])
            return previous
        batch_id = identifier("batch")
        workspace = "batch_" + batch_id.removeprefix("batch-")
        owner = {
            "id": batch_id,
            "workspace": workspace,
            "namespace": hashlib.sha256(workspace.encode()).hexdigest()[:16],
            "name": name,
        }
        document = Configuration.model_validate(deepcopy(self.defaults.model_dump()))
        if overrides is not None:
            document = Configuration.model_validate(
                {
                    **document.model_dump(),
                    "recipe": import_overrides(document.recipe, overrides),
                }
            )
        document, inherited, sources = self._copies(actor, document, copies)
        # The new batch always keeps deployment defaults for retrieval.
        document = Configuration.model_validate(
            {**document.model_dump(), "retrieval": deepcopy(self.defaults.retrieval)}
        )
        document, prompts, execution, bridge = self._prepare(
            actor, owner, document, inherited
        )
        revision = self.files.publish(
            owner,
            identifier("rev"),
            document,
            prompts,
            execution,
            bridge,
            {
                "actor": actor,
                "copied_groups": sources,
                "legacy_settings_schema": overrides.get("schema_version")
                if overrides
                else None,
            },
        )
        return self.registry.create(actor, request_id, fingerprint, owner, revision)

    def preview(self, actor: str, batch: str, expected: str, *, copies: dict) -> dict:
        self.allow(actor, "edit", batch)
        owner, _, before, original, _ = self.read(actor, batch)
        if owner["active"] != expected or owner["pending"]:
            raise Conflict("Configuration changed or application is pending")
        after, inherited, sources = self._copies(actor, before, copies)
        after, prompts, execution, _ = self._prepare(
            actor, owner, after, {**self._inherit(before, original), **inherited}
        )
        a, b = before.model_dump(), after.model_dump()
        for stage in STAGES:
            a["recipe"]["workflow"]["parameters"][stage]["prompt_file"] = (
                hashlib.sha256(original[stage].encode()).hexdigest()
            )
            b["recipe"]["workflow"]["parameters"][stage]["prompt_file"] = (
                hashlib.sha256(prompts[stage].encode()).hexdigest()
            )
        current = self.registry.revision(batch, expected)
        return {
            "expected_revision": expected,
            "draft": after.model_dump(),
            "prompts": prompts,
            "sources": sources,
            "differences": differences(a, b),
            "requires_restart": current["components"]["rag"]
            != self.files.components(after, prompts, execution)["rag"],
        }

    def save(
        self,
        actor: str,
        batch: str,
        expected: str,
        draft: Configuration,
        request_id: str,
        *,
        copies: dict | None = None,
        prompt_contents: dict[str, str] | None = None,
    ) -> dict:
        self.allow(actor, "edit", batch)
        TypeAdapter(Name).validate_python(request_id, strict=True)
        fingerprint = digest(
            {
                "expected": expected,
                "draft": draft.model_dump(),
                "copies": copies,
                **(
                    {"prompt_contents": prompt_contents}
                    if prompt_contents is not None
                    else {}
                ),
            }
        )
        previous = self.registry.previous(
            actor, "publish", batch, request_id, fingerprint
        )
        if previous:
            return previous
        owner, current, before, original, previous_manifest = self.read(actor, batch)
        if owner["active"] != expected or owner["pending"]:
            raise Conflict("Configuration changed or application is pending")
        copied_document, copied, sources = self._copies(actor, before, copies)
        a, b = (
            before.recipe["workflow"]["parameters"],
            draft.recipe["workflow"]["parameters"],
        )
        if a.get("topic_catalog_file") != b.get("topic_catalog_file") or a.get(
            "fields"
        ) != b.get("fields"):
            raise ValueError("Administrator-owned workflow parameters are read-only")
        draft, inherited = self._inline_prompts(
            draft, {**self._inherit(before, original), **copied}, prompt_contents
        )
        document, prompts, execution, bridge = self._prepare(
            actor, owner, draft, inherited
        )
        original_execution = self.catalog.snapshot(before)
        if original_execution != previous_manifest["execution"]:
            raise Conflict(
                "Registered execution references changed; register new reference IDs"
            )
        components = self.files.components(document, prompts, execution)
        if components == current["components"]:
            revision = current
        else:
            if document.rag.embedding_profile != before.rag.embedding_profile and owner[
                "index_state"
            ] not in {"empty", "unprovisioned"}:
                raise Conflict(
                    "Embedding changes require an empty batch or an explicit rebuild"
                )
            revision = self.files.publish(
                owner,
                identifier("rev"),
                document,
                prompts,
                execution,
                bridge,
                {
                    "actor": actor,
                    "parent_revision": expected,
                    **{
                        key: previous_manifest["sources"][key]
                        for key in ("index_binding_id", "index_binding_digest")
                        if key in previous_manifest["sources"]
                    },
                    "copied_groups": sources,
                    "user_edits": differences(
                        copied_document.model_dump(), draft.model_dump()
                    ),
                },
            )
        return self.registry.publish(
            actor,
            request_id,
            fingerprint,
            batch,
            expected,
            revision,
            identifier("op"),
            components["rag"] != current["components"]["rag"],
        )

    @staticmethod
    def _inline_prompts(draft, inherited, contents):
        data = draft.model_dump()
        inherited = dict(inherited)
        for stage, content in (contents or {}).items():
            if stage not in STAGES or not isinstance(content, str):
                raise ValueError(
                    "Inline Prompts require a supported stage and UTF-8 text"
                )
            from ..integrations.data_workflow.prompt_files import validate_prompt

            validate_prompt(stage, content)
            data["recipe"]["workflow"]["parameters"][stage]["prompt_file"] = None
            inherited[stage] = (None, content)
        return Configuration.model_validate(data), inherited

    def preview_draft(
        self, actor, batch, expected, draft, *, copies=None, prompt_contents=None
    ):
        self.allow(actor, "edit", batch)
        owner, current, before, original, _ = self.read(actor, batch)
        if owner["active"] != expected or owner["pending"]:
            raise Conflict("Configuration changed or application is pending")
        _, copied, sources = self._copies(actor, before, copies)
        a, b = (
            before.recipe["workflow"]["parameters"],
            draft.recipe["workflow"]["parameters"],
        )
        if a.get("topic_catalog_file") != b.get("topic_catalog_file") or a.get(
            "fields"
        ) != b.get("fields"):
            raise ValueError("Administrator-owned workflow parameters are read-only")
        draft, inherited = self._inline_prompts(
            draft, {**self._inherit(before, original), **copied}, prompt_contents
        )
        after, prompts, execution, _ = self._prepare(actor, owner, draft, inherited)
        old, new = before.model_dump(), after.model_dump()
        for stage in STAGES:
            old["recipe"]["workflow"]["parameters"][stage]["prompt_file"] = (
                hashlib.sha256(original[stage].encode()).hexdigest()
            )
            new["recipe"]["workflow"]["parameters"][stage]["prompt_file"] = (
                hashlib.sha256(prompts[stage].encode()).hexdigest()
            )
        return {
            "expected_revision": expected,
            "draft": after.model_dump(),
            "prompts": prompts,
            "sources": sources,
            "differences": differences(old, new),
            "requires_restart": current["components"]["rag"]
            != self.files.components(after, prompts, execution)["rag"],
        }

    def register_task(
        self,
        actor: str,
        batch: str,
        expected: str,
        request_id: str,
        kind: str,
        source: dict,
        *,
        payload: dict | None = None,
        request_fingerprint: str | None = None,
    ) -> dict:
        self.allow(actor, "submit", batch)
        TypeAdapter(
            Literal["ingest", "retrieval", "distillation", "chain", "query", "centers"]
        ).validate_python(kind, strict=True)
        TypeAdapter(Name).validate_python(request_id, strict=True)
        source = TaskSource.model_validate(source).model_dump(exclude_none=True)
        if source.get("data_batch_id") != batch or source.get("type") not in {
            "retrieval",
            "directory",
            "index",
        }:
            raise ValueError(
                "Task source must declare its target batch and supported source type"
            )
        if set(source) - {"data_batch_id", "type", "retrieval_job_id", "input_ids"}:
            raise ValueError("Task source does not accept arbitrary file paths")
        if source["type"] == "retrieval":
            TypeAdapter(Name).validate_python(
                source.get("retrieval_job_id"), strict=True
            )
            self.allow(
                actor, "read_retrieval", batch + ":" + source["retrieval_job_id"]
            )
        elif source["type"] == "directory":
            for reference in source.get("input_ids", []):
                TypeAdapter(Name).validate_python(reference, strict=True)
                self.allow(actor, "use_input", reference)
                if reference not in self.catalog.inputs:
                    raise ValueError("Unknown input reference")
        fingerprint = request_fingerprint or digest(
            {
                "expected": expected,
                "kind": kind,
                "source": source,
                **({"payload": payload} if payload is not None else {}),
            }
        )
        previous = self.registry.previous(actor, "task", batch, request_id, fingerprint)
        if previous:
            return previous
        _, record, config, _, manifest = self.read(actor, batch, expected)
        if source["type"] == "directory" and set(source["input_ids"]) - set(
            config.bridge.input_ids
        ):
            raise ValueError(
                "Task directory sources must belong to the selected configuration"
            )
        envelope = {
            "task_id": identifier("task"),
            "data_batch_id": batch,
            "config_revision_id": expected,
            "kind": kind,
            "actor": actor,
            "created_at": time.time(),
            "phase": "queued",
            "cancel_requested": False,
        }
        if payload is not None:
            envelope.update(payload=deepcopy(payload), payload_digest=digest(payload))
        envelope.update(
            source=deepcopy(source),
            manifest_digest=record["digest"],
            components=manifest["components"],
            execution=manifest["execution"],
            prompt_digests={
                name: value
                for name, value in manifest["artifacts"].items()
                if name.startswith("resources/prompts/")
            },
        )
        return self.registry.enqueue(
            actor, request_id, fingerprint, batch, expected, envelope
        )

    def list_orphans(self, actor: str) -> list[str]:
        self.allow(actor, "audit", "batches")
        return self.files.orphans(self.registry.referenced_directories())

    def operation(self, actor: str, batch: str, operation: str) -> dict:
        self.allow(actor, "read", batch)
        return self.registry.operation(batch, operation)

    def task(self, actor: str, batch: str, task: str) -> dict:
        self.allow(actor, "read", batch)
        return self.registry.task(batch, task)
