"""Server-side prompt overrides for the optional WebUI prompt editor.

The editor owns a small, versioned SQLite database. Upstream prompt modules remain
unchanged on disk; this module installs separate Chinese defaults at startup
and applies validated published overrides. The deployment is limited to one worker:
module globals cannot be synchronized across independent worker processes.
"""

from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any

from lightrag.parser.routing import resolve_parser_directives
from lightrag.prompt import PROMPTS
from lightrag.prompt_multimodal import MULTIMODAL_PROMPTS
from lightrag.parser.docx.smart_heading import title_block
from lightrag.prompt_admin_cache import set_prompt_admin_cache_digest
from lightrag.prompt_admin_storage import PromptSqliteStore, StalePromptRevision
from lightrag.prompt_templates import load_zh_templates


GROUPS: dict[str, tuple[str, ...]] = {
    "image_analysis": ("image_analysis",),
    "table_analysis": ("table_analysis",),
    "equation_analysis": ("equation_analysis",),
    "docx_title": ("docx_title_system", "docx_title_user"),
    "extraction_json": (
        "entity_extraction_json_system_prompt",
        "entity_extraction_json_user_prompt",
        "default_entity_types_guidance",
        "entity_extraction_json_examples",
        "entity_extraction_section_context",
    ),
    "extraction_text": (
        "entity_extraction_system_prompt",
        "entity_extraction_user_prompt",
        "default_entity_types_guidance",
        "entity_extraction_examples",
        "entity_extraction_section_context",
    ),
    "gleaning_json": (
        "entity_extraction_json_system_prompt",
        "default_entity_types_guidance",
        "entity_extraction_json_examples",
        "entity_extraction_json_user_prompt",
        "entity_extraction_section_context",
        "entity_continue_extraction_json_user_prompt",
    ),
    "gleaning_text": (
        "entity_extraction_system_prompt",
        "default_entity_types_guidance",
        "entity_extraction_examples",
        "entity_extraction_user_prompt",
        "entity_extraction_section_context",
        "entity_continue_extraction_user_prompt",
    ),
    "description_merge": ("summarize_entity_descriptions",),
    "keywords": ("keywords_extraction", "keywords_extraction_examples"),
    "graph_answer": ("rag_response", "kg_query_context", "user_prompt_prefix"),
    "naive_answer": (
        "naive_rag_response",
        "naive_query_context",
        "user_prompt_prefix",
    ),
}

_MULTIMODAL = frozenset(MULTIMODAL_PROMPTS)
_TITLE = frozenset({"docx_title_system", "docx_title_user"})
_PLAIN = frozenset({"user_prompt_prefix", "default_entity_types_guidance"})
_MAX_FIELD_CHARS = 100_000
_BUILTIN_PROMPTS = deepcopy(PROMPTS)
_BUILTIN_MULTIMODAL = deepcopy(MULTIMODAL_PROMPTS)
_BUILTIN_TITLE_SYSTEM = title_block._SYSTEM_PROMPT
_BUILTIN_TITLE_USER = title_block._USER_TEMPLATE


class PromptEditorConflict(ValueError):
    """The editor submitted a stale revision."""


class PromptAdmin:
    def __init__(self, rag: Any, working_dir: str, workspace: str, workers: int):
        self.rag = rag
        self.write_enabled = workers == 1
        self._lock = threading.RLock()
        suffix = hashlib.sha256((workspace or "default").encode()).hexdigest()[:12]
        self.legacy_path = Path(working_dir) / "prompt_admin" / f"{suffix}.json"
        self._title_block = title_block
        self.project_defaults: dict[str, Any] = {
            **{key: value for key, value in _BUILTIN_PROMPTS.items() if key in self.keys},
            **{key: value for key, value in _BUILTIN_MULTIMODAL.items() if key in self.keys},
            "docx_title_system": _BUILTIN_TITLE_SYSTEM,
            "docx_title_user": _BUILTIN_TITLE_USER,
            "user_prompt_prefix": "",
        }
        self.zh_defaults: dict[str, Any] = load_zh_templates(self.project_defaults)
        self.defaults: dict[str, Any] = deepcopy(self.zh_defaults)
        self.defaults["user_prompt_prefix"] = rag.user_prompt_prefix
        resolved_profile = getattr(rag, "_entity_extraction_prompt_profile", None)
        addon_params = getattr(rag, "addon_params", None) or {}
        external_profile = bool(addon_params.get("entity_type_prompt_file"))
        if resolved_profile is not None:
            for key, profile_key in (
                ("default_entity_types_guidance", "entity_types_guidance"),
                ("entity_extraction_examples", "entity_extraction_examples"),
                ("entity_extraction_json_examples", "entity_extraction_json_examples"),
            ):
                original = self.project_defaults[key]
                builtin_profile_value = (
                    [item.rstrip() for item in original]
                    if isinstance(original, list)
                    else original.rstrip()
                )
                explicitly_configured = external_profile or (
                    key == "default_entity_types_guidance" and "entity_types_guidance" in addon_params
                )
                if profile_key in resolved_profile and (
                    explicitly_configured or resolved_profile[profile_key] != builtin_profile_value
                ):
                    self.defaults[key] = deepcopy(resolved_profile[profile_key])
        self.store = PromptSqliteStore(working_dir, workspace, self.legacy_path)
        self.path = self.store.path
        self._state = self.store.initialize({
            "en": self.project_defaults,
            "zh": self.zh_defaults,
            "configured": self.defaults,
        })
        for key, value in self._state["active"].items():
            self._validate_field(key, value)
        if not isinstance(self._state["drafts"], dict) or not isinstance(self._state["draft_bases"], dict):
            raise ValueError("Invalid prompt editor drafts")
        if not isinstance(self._state["history"], list):
            raise ValueError("Invalid prompt editor history")
        # Older profiles stored only the continue prompt in a gleaning draft.
        # Fill newly exposed inherited fields from the current active version.
        for group, fields in self._state["drafts"].items():
            if group not in GROUPS or not isinstance(fields, dict) or not set(fields).issubset(GROUPS[group]):
                raise ValueError(f"Invalid prompt editor draft: {group}")
            base = self._state["draft_bases"].get(group)
            if base is None:
                base = {key: self._state["active"].get(key, self.defaults[key]) for key in GROUPS[group]}
            if not isinstance(base, dict) or not set(base).issubset(GROUPS[group]):
                raise ValueError(f"Invalid prompt editor draft base: {group}")
            self._state["draft_bases"][group] = {
                key: base.get(key, self._state["active"].get(key, self.defaults[key])) for key in GROUPS[group]
            }
            self._state["drafts"][group] = {
                key: fields.get(key, self._state["active"].get(key, self.defaults[key])) for key in GROUPS[group]
            }
            self._validate_group(group, self._state["drafts"][group])
        self._apply_active()

    @property
    def keys(self) -> set[str]:
        return {key for fields in GROUPS.values() for key in fields}

    def _validate_field(self, key: str, value: Any) -> None:
        if key not in self.defaults:
            raise ValueError(f"Unknown prompt field: {key}")
        original = self.defaults[key]
        if isinstance(original, list):
            if not isinstance(value, list) or not value or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"{key} must be a nonempty list of strings")
            if any(len(item) > _MAX_FIELD_CHARS for item in value):
                raise ValueError(f"{key} contains an oversized example")
            if key == "entity_extraction_examples":
                for item in value:
                    try:
                        item.format(
                            tuple_delimiter="<|#|>",
                            completion_delimiter="<|COMPLETE|>",
                            language="Chinese",
                            entity_types_guidance="Types",
                        )
                    except (KeyError, IndexError, ValueError) as exc:
                        raise ValueError(f"Invalid placeholders in {key}: {exc}") from exc
            return
        if not isinstance(value, str) or len(value) > _MAX_FIELD_CHARS:
            raise ValueError(f"{key} must be a string of at most {_MAX_FIELD_CHARS} characters")
        if key != "user_prompt_prefix" and not value.strip():
            raise ValueError(f"{key} cannot be empty")
        if key in _PLAIN or key == "docx_title_system":
            return
        original_fields = {name for _, name, _, _ in Formatter().parse(original) if name}
        try:
            actual_fields = {name for _, name, _, _ in Formatter().parse(value) if name}
        except ValueError as exc:
            raise ValueError(f"Invalid format braces in {key}: {exc}") from exc
        if actual_fields != original_fields:
            raise ValueError(
                f"{key} must preserve these placeholders: {', '.join(sorted(original_fields))}"
            )
        value.format(**{name: "sample" for name in original_fields})

    def _validate_group(self, group: str, fields: dict[str, Any]) -> None:
        if group not in GROUPS:
            raise ValueError(f"Unknown prompt group: {group}")
        if set(fields) != set(GROUPS[group]):
            raise ValueError("A prompt group must provide every field in its card")
        for key, value in fields.items():
            self._validate_field(key, value)

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            self.store.write(self._state, state)
        except StalePromptRevision as exc:
            raise PromptEditorConflict(str(exc)) from exc

    def _record_history(
        self, state: dict[str, Any], group: str, action: str, fields: dict[str, Any], actor: str
    ) -> None:
        state["history"] = [
            *self._state["history"],
            {
                "revision": state["revision"],
                "at": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "group": group,
                "actor": actor[:128] or "unknown",
                "fields": deepcopy(fields),
            },
        ]

    def history(self, group: str) -> list[dict[str, Any]]:
        if group not in GROUPS:
            raise ValueError(f"Unknown prompt group: {group}")
        with self._lock:
            return [deepcopy(entry) for entry in reversed(self._state["history"]) if entry.get("group") == group]

    def _apply_active(self) -> None:
        active = self._state["active"]
        for key in self.keys:
            value = active.get(key, self.defaults[key])
            if key in _MULTIMODAL:
                MULTIMODAL_PROMPTS[key] = value
            elif key == "docx_title_system":
                self._title_block._SYSTEM_PROMPT = value
            elif key == "docx_title_user":
                self._title_block._USER_TEMPLATE = value
            elif key == "user_prompt_prefix":
                self.rag.user_prompt_prefix = value
            else:
                PROMPTS[key] = value
        # The core caches its resolved extraction guidance and examples.
        self.rag._refresh_addon_params_cache()
        # An optional ENTITY_TYPE_PROMPT_FILE or addon_params profile takes
        # precedence when the cache is built. A published WebUI override is
        # explicitly the last layer, so the editor always shows effective text.
        profile = getattr(self.rag, "_entity_extraction_prompt_profile", None)
        if profile is not None:
            for key, profile_key in (
                ("default_entity_types_guidance", "entity_types_guidance"),
                ("entity_extraction_examples", "entity_extraction_examples"),
                ("entity_extraction_json_examples", "entity_extraction_json_examples"),
            ):
                if key in active:
                    profile[profile_key] = deepcopy(active[key])
        effective = {key: active.get(key, default) for key, default in self.defaults.items()}
        set_prompt_admin_cache_digest(
            hashlib.sha256(json.dumps(effective, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        )

    def _status(self, group: str) -> dict[str, str]:
        md = resolve_parser_directives("example.md", require_external_endpoint=False)
        docx = resolve_parser_directives("example.docx", require_external_endpoint=False)
        json_mode = bool(self.rag.entity_extraction_use_json)
        if group in {"table_analysis", "equation_analysis", "image_analysis"}:
            option = {"table_analysis": "t", "equation_analysis": "e", "image_analysis": "i"}[group]
            if md.engine != "native" or option not in md.process_options:
                return {"state": "inactive", "reason": f"Markdown route does not enable native {option} analysis."}
            if group == "image_analysis" and (
                not self.rag.vlm_process_enable or not self.rag.role_llm_funcs.get("vlm")
            ):
                return {"state": "inactive", "reason": "Image analysis also requires VLM_PROCESS_ENABLE and a VLM role."}
            return {"state": "conditional", "reason": "Used when a Markdown document contains a recognized " + option + " item."}
        if group == "docx_title":
            if docx.engine != "native" or not docx.engine_params.get("smart_heading"):
                return {"state": "inactive", "reason": "Native DOCX smart heading is not enabled; Markdown documents never use this prompt."}
            return {"state": "conditional", "reason": "Used only for qualifying DOCX title windows; Markdown never uses this prompt."}
        if group in {"extraction_json", "gleaning_json", "extraction_text", "gleaning_text"}:
            wanted_json = group.endswith("json")
            if wanted_json != json_mode:
                return {"state": "inactive", "reason": "The active entity extraction output format is " + ("JSON." if json_mode else "delimiter text.")}
            if group.startswith("gleaning"):
                if self.rag.entity_extract_max_gleaning <= 0:
                    return {"state": "inactive", "reason": "MAX_GLEANING is zero."}
                return {"state": "conditional", "reason": "Used after initial extraction unless the token budget skips gleaning."}
            return {"state": "active", "reason": "Used for each extracted document chunk, including Markdown chunks."}
        if group == "description_merge":
            return {"state": "conditional", "reason": "Used when entity or relation descriptions need LLM merging."}
        if group == "keywords":
            return {"state": "conditional", "reason": "Used for graph retrieval unless keywords are supplied or a cache hit occurs."}
        if group == "graph_answer":
            return {"state": "conditional", "reason": "Used for local, global, hybrid or mix final answers; context-only skips the answer model."}
        return {"state": "conditional", "reason": "Used for Naive final answers; context-only skips the answer model."}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = self._state["active"]
            drafts = self._state["drafts"]
            return {
                "revision": self._state["revision"],
                "write_enabled": self.write_enabled,
                "groups": [
                    {
                        "id": group,
                        "fields": [
                            {
                                "key": key,
                                "project_default": self.project_defaults[key],
                                "zh_default": self.zh_defaults[key],
                                "default": self.defaults[key],
                                "active": active.get(key, self.defaults[key]),
                                "draft": drafts.get(group, {}).get(key, active.get(key, self.defaults[key])),
                                "customized": key in active,
                            }
                            for key in keys
                        ],
                        "has_draft": group in drafts,
                        **self._status(group),
                    }
                    for group, keys in GROUPS.items()
                ],
            }

    def save_draft(self, group: str, fields: dict[str, Any], revision: int, actor: str = "unknown") -> dict[str, Any]:
        with self._lock:
            self._check_revision(revision)
            self._validate_group(group, fields)
            next_state = {
                **self._state,
                "revision": revision + 1,
                "drafts": {**self._state["drafts"], group: fields},
                "draft_bases": {
                    **self._state["draft_bases"],
                    group: {
                        key: self._state["active"].get(key, self.defaults[key])
                        for key in GROUPS[group]
                    },
                },
            }
            self._record_history(next_state, group, "draft", fields, actor)
            self._write_state(next_state)
            self._state = next_state
            return self.snapshot()

    def publish(self, group: str, revision: int, actor: str = "unknown") -> dict[str, Any]:
        with self._lock:
            self._check_revision(revision)
            fields = self._state["drafts"].get(group)
            if fields is None:
                raise ValueError("Save a draft before publishing")
            self._validate_group(group, fields)
            next_active = dict(self._state["active"])
            base = self._state["draft_bases"].get(group, {})
            for key, value in fields.items():
                base_value = base.get(key, self.defaults[key])
                if value == base_value:
                    continue
                if next_active.get(key, self.defaults[key]) != base_value:
                    raise PromptEditorConflict(f"Shared prompt {key} changed after the draft was saved; save the draft again")
                if value == self.defaults[key]:
                    next_active.pop(key, None)
                else:
                    next_active[key] = value
            next_drafts = dict(self._state["drafts"])
            del next_drafts[group]
            next_bases = dict(self._state["draft_bases"])
            next_bases.pop(group, None)
            next_state = {"revision": revision + 1, "active": next_active, "drafts": next_drafts, "draft_bases": next_bases}
            self._record_history(
                next_state,
                group,
                "publish",
                {key: next_active.get(key, self.defaults[key]) for key in GROUPS[group]},
                actor,
            )
            self._write_state(next_state)
            self._state = next_state
            self._apply_active()
            return self.snapshot()

    def reset(
        self, group: str, revision: int, actor: str = "unknown", language: str = "zh"
    ) -> dict[str, Any]:
        with self._lock:
            self._check_revision(revision)
            if group not in GROUPS:
                raise ValueError(f"Unknown prompt group: {group}")
            if language not in {"zh", "en"}:
                raise ValueError(f"Unknown prompt language: {language}")
            before_reset = {key: self._state["active"].get(key, self.defaults[key]) for key in GROUPS[group]}
            active = {key: value for key, value in self._state["active"].items() if key not in GROUPS[group]}
            if language == "en":
                for key in GROUPS[group]:
                    # The global user prefix is a user setting, not a translated template.
                    value = self.defaults[key] if key == "user_prompt_prefix" else self.project_defaults[key]
                    self._validate_field(key, value)
                    if value != self.defaults[key]:
                        active[key] = deepcopy(value)
            drafts = {key: value for key, value in self._state["drafts"].items() if key != group}
            draft_bases = {key: value for key, value in self._state["draft_bases"].items() if key != group}
            next_state = {"revision": revision + 1, "active": active, "drafts": drafts, "draft_bases": draft_bases}
            self._record_history(next_state, group, "reset_english" if language == "en" else "reset", before_reset, actor)
            self._write_state(next_state)
            self._state = next_state
            self._apply_active()
            return self.snapshot()

    def _check_revision(self, revision: int) -> None:
        if not self.write_enabled:
            raise PermissionError("Prompt editing requires a single server worker")
        if revision != self._state["revision"]:
            raise PromptEditorConflict("Prompt profile changed; reload before editing")
