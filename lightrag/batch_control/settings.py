"""Batch-owned settings metadata and validated drafts, independent of legacy overrides."""

from .applications import ApplicationStore
from .contracts import Configuration
from .fields import describe_fields
from .runtime_config import compile_startup
from .runtime_store import RuntimeStore
from .resources import digest
from .storage import Conflict
from ..integrations.data_workflow.config import load_settings
from ..integrations.data_workflow.recipe_settings import RecipeValues
from ..integrations.data_workflow.recipe_schema import (
    PARAMETER_FIELDS,
    RUNTIME_FIELDS,
    STAGES,
)


def recipe_values(recipe):
    parameters = recipe["workflow"]["parameters"]
    return RecipeValues.model_validate(
        {
            **{key: parameters[key] for key in PARAMETER_FIELDS if key in parameters},
            **{
                key: recipe.get("runtime", {})[key]
                for key in RUNTIME_FIELDS
                if key in recipe.get("runtime", {})
            },
            "models": recipe["models"],
            "stages": {stage: parameters[stage] for stage in STAGES},
        }
    ).model_dump()


class SettingsService:
    def __init__(self, control, policy, *, actor):
        self.control, self.policy, self.actor = control, policy, actor

    def get(self, batch):
        control, actor = self.control, self.actor
        owner, record, active, active_prompts, _ = control.read(actor, batch)
        revision = owner["desired"] or owner["active"]
        _, selected_record, document, prompts, _ = control.read(actor, batch, revision)
        bridge = load_settings(
            control.paths.revision(batch, revision) / "data_workflow.yaml"
        )
        schema = control.probe(bridge, "describe")
        catalog = control.catalog

        def options(action, values):
            return [key for key in values if control.authorize(actor, action, key)]

        allowed_profiles = options("use_profile", catalog.profiles)
        from .index_rebuild.authority import is_rebuild
        from .index_rebuild.service import IndexRebuilds

        with control.registry.transaction() as db:
            rebuilding = owner["pending"] and is_rebuild(db, batch, owner["pending"])
        rebuild_history = IndexRebuilds(control, self.policy, actor=actor).list(
            batch, 1
        )["items"]
        return {
            "schema_version": 1,
            "batch": owner,
            "revision": revision,
            "configuration": document.model_dump(),
            "active_configuration": active.model_dump(),
            "prompts": prompts,
            "active_prompts": active_prompts,
            "recipe_values": recipe_values(document.recipe),
            "fields": describe_fields(schema),
            "model_schema": schema.get("model_schema", {}),
            "task_schema": schema.get("task_schema", {}),
            "options": {
                "profiles": [
                    {
                        "id": ref,
                        "model_name": catalog.profiles[ref].get("model_name", ref),
                        "dimensions": catalog.profiles[ref].get("dimensions"),
                    }
                    for ref in allowed_profiles
                ],
                "environments": options("use_environment", catalog.environments),
                "credentials": options("use_credential", catalog.credentials),
                "inputs": options("use_input", catalog.inputs),
                "parse_engines": ["native", "legacy", "mineru", "docling"],
                "chunking_strategies": [
                    "fixed_token",
                    "recursive_character",
                    "semantic_vector",
                    "paragraph_semantic",
                ],
            },
            "permissions": {
                action: control.authorize(actor, action, batch)
                for action in (
                    "edit",
                    "apply_config",
                    "rebuild_index",
                    "run_runtime",
                    "stop_runtime",
                    "audit",
                )
            },
            "embedding_editable": owner["index_state"] == "unprovisioned"
            and RuntimeStore(control.registry).get(batch) is None,
            "index_rebuild": rebuild_history[0] if rebuild_history else None,
            "application": None
            if rebuilding
            else ApplicationStore(control.registry).get(batch, owner["pending"])
            if owner["pending"]
            else ApplicationStore(control.registry).latest(batch),
            "manifest_digest": selected_record["digest"],
        }

    def preview(self, batch, expected, draft, *, copies=None, prompt_contents=None):
        result = self.control.preview_draft(
            self.actor,
            batch,
            expected,
            draft,
            copies=copies,
            prompt_contents=prompt_contents,
        )
        after = Configuration.model_validate(result["draft"])
        owner, _, active, _, active_manifest = self.control.read(self.actor, batch)
        if after.rag.embedding_profile != active.rag.embedding_profile and (
            owner["index_state"] != "unprovisioned"
            or RuntimeStore(self.control.registry).get(batch) is not None
        ):
            raise Conflict("Embedding changes require a new unprovisioned batch")
        execution = self.control.catalog.snapshot(after)
        manifest = {
            "execution": execution,
            "sources": active_manifest["sources"],
            "components": self.control.files.components(
                after, result["prompts"], execution
            ),
        }
        compile_startup(
            self.control,
            self.actor,
            self.control.registry.get(batch),
            expected,
            after,
            manifest,
            self.policy,
        )
        return result

    def save(
        self, batch, expected, draft, request_id, *, copies=None, prompt_contents=None
    ):
        self.control.allow(self.actor, "edit", batch)
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
        if self.control.registry.previous(
            self.actor, "publish", batch, request_id, fingerprint
        ):
            return self.control.save(
                self.actor,
                batch,
                expected,
                draft,
                request_id,
                copies=copies,
                prompt_contents=prompt_contents,
            )
        self.preview(
            batch, expected, draft, copies=copies, prompt_contents=prompt_contents
        )
        return self.control.save(
            self.actor,
            batch,
            expected,
            draft,
            request_id,
            copies=copies,
            prompt_contents=prompt_contents,
        )
