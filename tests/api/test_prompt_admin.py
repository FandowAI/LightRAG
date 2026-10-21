"""Prompt editor persistence and runtime status without external services."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import hashlib
import json
import sqlite3
from string import Formatter

import pytest

from lightrag.prompt import PROMPTS, get_default_entity_extraction_prompt_profile
from lightrag.prompt_multimodal import MULTIMODAL_PROMPTS
from lightrag.prompt_admin import GROUPS, PromptAdmin, PromptEditorConflict
from lightrag.prompt_admin_cache import prompt_admin_cache_digest, set_prompt_admin_cache_digest
from lightrag.parser.docx.smart_heading import title_block


@dataclass
class RagStub:
    entity_extraction_use_json: bool = True
    entity_extract_max_gleaning: int = 1
    vlm_process_enable: bool = False
    user_prompt_prefix: str = ""
    role_llm_funcs: dict = field(default_factory=lambda: {"vlm": None})
    refreshes: int = 0

    def _refresh_addon_params_cache(self) -> None:
        self.refreshes += 1


@pytest.fixture(autouse=True)
def restore_prompt_globals():
    original_prompts = deepcopy(PROMPTS)
    original_multimodal = deepcopy(MULTIMODAL_PROMPTS)
    original_title = (title_block._SYSTEM_PROMPT, title_block._USER_TEMPLATE)
    original_digest = prompt_admin_cache_digest()
    yield
    PROMPTS.clear()
    PROMPTS.update(original_prompts)
    MULTIMODAL_PROMPTS.clear()
    MULTIMODAL_PROMPTS.update(original_multimodal)
    title_block._SYSTEM_PROMPT, title_block._USER_TEMPLATE = original_title
    set_prompt_admin_cache_digest(original_digest)


def test_publish_updates_runtime_persists_and_changes_status(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTRAG_PARSER", "*:native-teP,*:legacy-R")
    original = PROMPTS["entity_extraction_json_user_prompt"]
    rag = RagStub()
    try:
        manager = PromptAdmin(rag, str(tmp_path), "workspace", 1)
        snapshot = manager.snapshot()
        groups = {item["id"]: item for item in snapshot["groups"]}
        user_field = next(item for item in groups["extraction_json"]["fields"] if item["key"] == "entity_extraction_json_user_prompt")
        assert user_field["project_default"] == original
        assert user_field["default"] == manager.zh_defaults["entity_extraction_json_user_prompt"]
        assert groups["extraction_json"]["state"] == "active"
        assert groups["extraction_text"]["state"] == "inactive"
        assert groups["table_analysis"]["state"] == "conditional"
        assert groups["image_analysis"]["state"] == "inactive"

        fields = {item["key"]: item["draft"] for item in groups["extraction_json"]["fields"]}
        fields["entity_extraction_json_user_prompt"] = original + "\nAnswer in Chinese."
        with pytest.raises(ValueError, match="placeholders"):
            manager.save_draft("extraction_json", {**fields, "entity_extraction_json_user_prompt": "bad"}, 0)

        manager.save_draft("extraction_json", fields, 0)
        with pytest.raises(PromptEditorConflict):
            manager.publish("extraction_json", 0)
        manager.publish("extraction_json", 1)
        assert PROMPTS["entity_extraction_json_user_prompt"].endswith("Answer in Chinese.")
        assert prompt_admin_cache_digest()
        assert rag.refreshes >= 2

        second = PromptAdmin(RagStub(), str(tmp_path), "workspace", 1)
        assert second.snapshot()["revision"] == 2
        second.rag.entity_extraction_use_json = False
        second_groups = {item["id"]: item for item in second.snapshot()["groups"]}
        assert second_groups["extraction_json"]["state"] == "inactive"
        assert second_groups["extraction_text"]["state"] == "active"
        second.reset("extraction_json", 2)
        assert PROMPTS["entity_extraction_json_user_prompt"] == manager.zh_defaults["entity_extraction_json_user_prompt"]
    finally:
        PROMPTS["entity_extraction_json_user_prompt"] = original


def test_published_examples_override_an_external_profile(tmp_path):
    class RagWithExternalProfile(RagStub):
        def _refresh_addon_params_cache(self) -> None:
            self._entity_extraction_prompt_profile = {
                "entity_types_guidance": "External guidance",
                "entity_extraction_examples": ["External text example"],
                "entity_extraction_json_examples": ["External JSON example"],
            }

    rag = RagWithExternalProfile()
    rag._refresh_addon_params_cache()
    manager = PromptAdmin(rag, str(tmp_path), "external-profile", 1)
    try:
        group = next(item for item in manager.snapshot()["groups"] if item["id"] == "extraction_json")
        assert next(item["default"] for item in group["fields"] if item["key"] == "default_entity_types_guidance") == "External guidance"
        fields = {item["key"]: item["draft"] for item in group["fields"]}
        fields["default_entity_types_guidance"] = "WebUI guidance"
        fields["entity_extraction_json_examples"] = ["WebUI JSON example"]
        manager.save_draft("extraction_json", fields, 0)
        manager.publish("extraction_json", 1)
        assert rag._entity_extraction_prompt_profile["entity_types_guidance"] == "WebUI guidance"
        assert rag._entity_extraction_prompt_profile["entity_extraction_json_examples"] == [
            "WebUI JSON example"
        ]
        assert rag._entity_extraction_prompt_profile["entity_extraction_examples"] == [
            "External text example"
        ]
        with pytest.raises(ValueError, match="Invalid placeholders"):
            manager.save_draft(
                "extraction_text",
                {
                    item["key"]: ["{missing}"] if item["key"] == "entity_extraction_examples" else item["draft"]
                    for item in next(
                        group for group in manager.snapshot()["groups"] if group["id"] == "extraction_text"
                    )["fields"]
                },
                2,
            )
    finally:
        manager.reset("extraction_json", manager.snapshot()["revision"])
        assert rag._entity_extraction_prompt_profile["entity_types_guidance"] == "External guidance"


def test_gleaning_exposes_shared_system_and_keeps_version_history(tmp_path):
    original = PROMPTS["entity_extraction_json_system_prompt"]
    manager = PromptAdmin(RagStub(), str(tmp_path), "gleaning", 1)
    try:
        group = next(item for item in manager.snapshot()["groups"] if item["id"] == "gleaning_json")
        keys = {item["key"] for item in group["fields"]}
        assert "entity_extraction_json_system_prompt" in keys
        assert "entity_extraction_json_user_prompt" in keys
        fields = {item["key"]: item["draft"] for item in group["fields"]}
        fields["entity_extraction_json_system_prompt"] = original + "\nUse the revised glossary."
        manager.save_draft("gleaning_json", fields, 0, "alice")
        assert PROMPTS["entity_extraction_json_system_prompt"] == manager.zh_defaults["entity_extraction_json_system_prompt"]
        manager.publish("gleaning_json", 1, "alice")
        assert PROMPTS["entity_extraction_json_system_prompt"].endswith("Use the revised glossary.")
        assert [entry["action"] for entry in manager.history("gleaning_json")] == ["publish", "draft"]
        assert manager.history("gleaning_json")[0]["actor"] == "alice"
        reloaded = PromptAdmin(RagStub(), str(tmp_path), "gleaning", 1)
        assert len(reloaded.history("gleaning_json")) == 2
        reloaded.reset("gleaning_json", 2, "alice")
        assert reloaded.history("gleaning_json")[0]["fields"]["entity_extraction_json_system_prompt"].endswith("Use the revised glossary.")
    finally:
        PROMPTS["entity_extraction_json_system_prompt"] = original


def test_older_continue_only_draft_is_migrated(tmp_path):
    continue_key = "entity_continue_extraction_json_user_prompt"
    old_draft = PROMPTS[continue_key] + "\nOne more pass."
    legacy_path = tmp_path / "prompt_admin" / f"{hashlib.sha256(b'old-draft').hexdigest()[:12]}.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps({
        "revision": 1,
        "active": {},
        "drafts": {"gleaning_json": {continue_key: old_draft}},
    }), encoding="utf-8")
    migrated = PromptAdmin(RagStub(), str(tmp_path), "old-draft", 1)
    group = next(item for item in migrated.snapshot()["groups"] if item["id"] == "gleaning_json")
    assert len(group["fields"]) == 6
    assert next(item["draft"] for item in group["fields"] if item["key"] == continue_key) == old_draft
    assert migrated.path.name == "prompts.sqlite3"
    assert legacy_path.exists()


def test_sqlite_stores_all_templates_and_isolates_workspaces(tmp_path):
    first = PromptAdmin(RagStub(), str(tmp_path), "first", 1)
    second = PromptAdmin(RagStub(), str(tmp_path), "second", 1)
    assert first.path == second.path
    assert first.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(first.path) as connection:
        rows = connection.execute(
            "SELECT variant, COUNT(*) FROM prompt_templates WHERE workspace = ? GROUP BY variant",
            ("first",),
        ).fetchall()
        assert sorted(rows) == [("configured", 23), ("en", 23), ("zh", 23)]
        assert connection.execute("SELECT COUNT(*) FROM prompt_templates").fetchone()[0] == 138

    group = next(item for item in first.snapshot()["groups"] if item["id"] == "keywords")
    fields = {item["key"]: item["draft"] for item in group["fields"]}
    first.save_draft("keywords", fields, 0, "alice")
    assert second.snapshot()["revision"] == 0
    with sqlite3.connect(first.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM prompt_drafts WHERE workspace = ?", ("first",)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM prompt_history WHERE workspace = ?", ("first",)
        ).fetchone()[0] == 1


def test_sqlite_rejects_stale_writes_between_managers(tmp_path):
    first = PromptAdmin(RagStub(), str(tmp_path), "shared", 1)
    stale = PromptAdmin(RagStub(), str(tmp_path), "shared", 1)
    group = next(item for item in first.snapshot()["groups"] if item["id"] == "keywords")
    fields = {item["key"]: item["draft"] for item in group["fields"]}
    first.save_draft("keywords", fields, 0)
    with pytest.raises(PromptEditorConflict, match="reload"):
        stale.save_draft("keywords", fields, 0)
    assert PromptAdmin(RagStub(), str(tmp_path), "shared", 1).snapshot()["revision"] == 1


def test_legacy_json_import_preserves_published_draft_and_history(tmp_path):
    workspace = "legacy-full"
    legacy_path = tmp_path / "prompt_admin" / f"{hashlib.sha256(workspace.encode()).hexdigest()[:12]}.json"
    legacy_path.parent.mkdir(parents=True)
    key = "keywords_extraction"
    published = PROMPTS[key] + "\nUse Chinese terms."
    draft = published + "\nKeep citations."
    group = "keywords"
    # A full draft is required by the prompt editor; get the second field from the shipped source.
    examples = deepcopy(PROMPTS["keywords_extraction_examples"])
    fields = {key: draft, "keywords_extraction_examples": examples}
    bases = {key: published, "keywords_extraction_examples": examples}
    history = [{
        "revision": 1, "at": "2026-10-21T00:00:00+00:00", "action": "publish",
        "group": group, "actor": "alice", "fields": bases,
    }]
    legacy = {
        "revision": 2, "active": {key: published},
        "drafts": {group: fields}, "draft_bases": {group: bases}, "history": history,
    }
    legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    manager = PromptAdmin(RagStub(), str(tmp_path), workspace, 1)
    assert manager.snapshot()["revision"] == 2
    current = next(item for item in manager.snapshot()["groups"] if item["id"] == group)
    assert current["has_draft"]
    assert next(item for item in current["fields"] if item["key"] == key)["active"] == published
    assert next(item for item in current["fields"] if item["key"] == key)["draft"] == draft
    assert manager.history(group) == history
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy

    manager.publish(group, 2)
    assert PromptAdmin(RagStub(), str(tmp_path), workspace, 1).snapshot()["revision"] == 3
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy


def test_gleaning_draft_does_not_revert_newer_shared_system(tmp_path):
    original_system = PROMPTS["entity_extraction_json_system_prompt"]
    original_continue = PROMPTS["entity_continue_extraction_json_user_prompt"]
    manager = PromptAdmin(RagStub(), str(tmp_path), "shared-fields", 1)
    try:
        groups = {item["id"]: item for item in manager.snapshot()["groups"]}
        gleaning = {item["key"]: item["draft"] for item in groups["gleaning_json"]["fields"]}
        gleaning["entity_continue_extraction_json_user_prompt"] = original_continue + "\nFind omitted links."
        manager.save_draft("gleaning_json", gleaning, 0)

        extraction = {item["key"]: item["draft"] for item in groups["extraction_json"]["fields"]}
        extraction["entity_extraction_json_system_prompt"] = original_system + "\nPrefer Chinese labels."
        manager.save_draft("extraction_json", extraction, 1)
        manager.publish("extraction_json", 2)
        manager.publish("gleaning_json", 3)

        assert PROMPTS["entity_extraction_json_system_prompt"].endswith("Prefer Chinese labels.")
        assert PROMPTS["entity_continue_extraction_json_user_prompt"].endswith("Find omitted links.")
    finally:
        revision = manager.snapshot()["revision"]
        manager.reset("gleaning_json", revision)


def test_chinese_defaults_preserve_prompt_structure_and_runtime_protocol(tmp_path):
    manager = PromptAdmin(RagStub(), str(tmp_path), "chinese-defaults", 1)
    assert prompt_admin_cache_digest()
    assert manager.defaults.keys() == manager.project_defaults.keys()
    assert len(manager.defaults) == 23
    for key, value in manager.zh_defaults.items():
        if key == "user_prompt_prefix":
            assert value == ""
            continue
        parts = value if isinstance(value, list) else [value]
        assert all(any("\u4e00" <= char <= "\u9fff" for char in part) or key.endswith("_examples") for part in parts)
    assert '"entities"' in manager.zh_defaults["entity_extraction_json_examples"][0]
    assert '"relationships"' in manager.zh_defaults["entity_extraction_json_examples"][0]
    assert "{tuple_delimiter}" in manager.zh_defaults["entity_extraction_system_prompt"]
    assert "### References" in manager.zh_defaults["rag_response"]
    assert PROMPTS["entity_extraction_json_system_prompt"] == manager.zh_defaults["entity_extraction_json_system_prompt"]
    assert MULTIMODAL_PROMPTS["image_analysis"] == manager.zh_defaults["image_analysis"]
    assert title_block._USER_TEMPLATE == manager.zh_defaults["docx_title_user"]
    for key, template in manager.zh_defaults.items():
        if key in {"user_prompt_prefix", "default_entity_types_guidance"}:
            continue
        if key in {"entity_extraction_json_examples", "keywords_extraction_examples"}:
            json.loads(template[0])
            continue
        parts = template if isinstance(template, list) else [template]
        for part in parts:
            fields = {name for _, name, _, _ in Formatter().parse(part) if name}
            part.format(**{name: "sample" for name in fields})


def test_english_original_can_be_published_and_reset_to_chinese(tmp_path):
    manager = PromptAdmin(RagStub(), str(tmp_path), "language-choice", 1)
    original = manager.project_defaults["keywords_extraction"]
    group = next(item for item in manager.snapshot()["groups"] if item["id"] == "keywords")
    english_fields = {item["key"]: item["project_default"] for item in group["fields"]}

    manager.save_draft("keywords", english_fields, 0)
    manager.publish("keywords", 1)
    assert PROMPTS["keywords_extraction"] == original

    manager.reset("keywords", 2)
    assert PROMPTS["keywords_extraction"] == manager.zh_defaults["keywords_extraction"]


def test_restore_english_applies_immediately_and_chinese_restores_configured_default(tmp_path):
    manager = PromptAdmin(RagStub(), str(tmp_path), "language-reset", 1)
    original = manager.project_defaults["keywords_extraction"]
    chinese = manager.zh_defaults["keywords_extraction"]

    english_snapshot = manager.reset("keywords", 0, "alice", "en")
    assert english_snapshot["revision"] == 1
    assert PROMPTS["keywords_extraction"] == original
    assert manager.history("keywords")[0]["action"] == "reset_english"
    assert manager.history("keywords")[0]["fields"]["keywords_extraction"] == chinese

    reloaded = PromptAdmin(RagStub(), str(tmp_path), "language-reset", 1)
    assert PROMPTS["keywords_extraction"] == original
    reloaded.reset("keywords", 1, "alice", "zh")
    assert PROMPTS["keywords_extraction"] == chinese
    assert reloaded.history("keywords")[0]["action"] == "reset"


@pytest.mark.parametrize("group", GROUPS)
def test_each_card_can_restore_english_and_chinese(tmp_path, group):
    manager = PromptAdmin(RagStub(), str(tmp_path), f"reset-{group}", 1)
    english = manager.reset(group, 0, language="en")
    fields = {item["key"]: item for item in next(item for item in english["groups"] if item["id"] == group)["fields"]}
    for key in GROUPS[group]:
        expected = manager.defaults[key] if key == "user_prompt_prefix" else manager.project_defaults[key]
        assert fields[key]["active"] == expected

    chinese = manager.reset(group, 1, language="zh")
    fields = {item["key"]: item for item in next(item for item in chinese["groups"] if item["id"] == group)["fields"]}
    for key in GROUPS[group]:
        assert fields[key]["active"] == manager.defaults[key]


def test_builtin_extraction_profile_becomes_chinese_at_startup(tmp_path):
    class RagWithBuiltinProfile(RagStub):
        def __init__(self):
            super().__init__()
            self._entity_extraction_prompt_profile = get_default_entity_extraction_prompt_profile()

        def _refresh_addon_params_cache(self) -> None:
            self._entity_extraction_prompt_profile = get_default_entity_extraction_prompt_profile()

    rag = RagWithBuiltinProfile()
    manager = PromptAdmin(rag, str(tmp_path), "builtin-profile", 1)
    assert manager.defaults["default_entity_types_guidance"] == manager.zh_defaults["default_entity_types_guidance"]
    assert rag._entity_extraction_prompt_profile["entity_types_guidance"] == manager.zh_defaults["default_entity_types_guidance"].rstrip()
    assert rag._entity_extraction_prompt_profile["entity_extraction_json_examples"] == [
        item.rstrip() for item in manager.zh_defaults["entity_extraction_json_examples"]
    ]


def test_explicit_external_profile_is_preserved_even_when_text_matches_builtin(tmp_path):
    rag = RagStub()
    rag.addon_params = {"entity_type_prompt_file": "configured.yml"}
    rag._entity_extraction_prompt_profile = get_default_entity_extraction_prompt_profile()
    manager = PromptAdmin(rag, str(tmp_path), "explicit-profile", 1)
    assert manager.defaults["default_entity_types_guidance"] == manager.project_defaults["default_entity_types_guidance"].rstrip()
    assert manager.defaults["entity_extraction_json_examples"] == [
        item.rstrip() for item in manager.project_defaults["entity_extraction_json_examples"]
    ]

