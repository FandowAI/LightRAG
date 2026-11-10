"""Configuration ownership, copying, publication and queue registration contracts."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from .helpers import change
from lightrag.batch_control.contracts import AdmissionDecision, Configuration
from lightrag.batch_control.fields import describe_fields, import_overrides
from lightrag.batch_control.storage import Conflict, IntegrityError

pytestmark = pytest.mark.offline


def new(factory, name="Test", request="create"):
    service = factory[0]()
    return service, service.create("user", name, request)


def edit_questions(document, count):
    value = document.model_dump()
    value["recipe"]["workflow"]["parameters"]["questions_per_source"] = count
    return Configuration.model_validate(value)


def test_initialization_has_no_side_effects_and_batch_registration_is_not_runtime(
    factory,
):
    service = factory[0]()
    assert not factory[1].registry.exists()
    batch = service.create("user", "Same display name", "first")
    assert batch["index_state"] == "unprovisioned"
    assert batch["desired"] is None
    assert not (factory[1].runtime_root / "data-workflow").exists()
    owner, record, document, prompts, manifest = service.read("user", batch["id"])
    assert document.recipe["workflow"]["parameters"]["questions_per_source"] == 3
    assert "deduplicate_qa" not in document.recipe["workflow"]["parameters"]
    assert "topic_catalog_file" not in document.recipe["workflow"]["parameters"]
    assert manifest["validation_scope"] == "workflow_preparation_without_model_requests"
    assert set(prompts) == {"scenario", "qa", "classification", "inspection"}
    assert all(
        Path(task["prompt_file"]).is_relative_to(Path(record["directory"]))
        for task in document.recipe["workflow"]["parameters"].values()
        if isinstance(task, dict) and "prompt_file" in task
    )
    assert owner["workspace"].startswith("batch_")


def test_create_is_idempotent_and_same_name_allocates_new_identity(factory):
    service, a = new(factory)
    assert service.create("user", "Test", "create") == a
    b = service.create("user", "Test", "another")
    assert (
        a["id"] != b["id"]
        and a["workspace"] != b["workspace"]
        and a["namespace"] != b["namespace"]
    )
    with pytest.raises(Conflict):
        service.create("user", "Changed", "create")


def test_save_idempotency_cas_noop_and_restart_classification(factory):
    service, batch = new(factory)
    _, _, document, _, _ = service.read("user", batch["id"])
    unchanged = service.save("user", batch["id"], batch["active"], document, "noop")
    assert unchanged["status"] == "unchanged" and unchanged["operation"] is None
    saved = service.save(
        "user", batch["id"], batch["active"], edit_questions(document, 5), "save"
    )
    assert saved["status"] == "applied" and not saved["requires_restart"]
    assert (
        service.save(
            "user", batch["id"], batch["active"], edit_questions(document, 5), "save"
        )
        == saved
    )
    with pytest.raises(Conflict):
        service.save("user", batch["id"], batch["active"], document, "stale")
    _, _, current, _, _ = service.read("user", batch["id"])
    result = service.save(
        "user",
        batch["id"],
        saved["revision"],
        change(current, "rag", chunk_size=1400),
        "restart",
    )
    owner = service.registry.get(batch["id"])
    assert (
        result["status"] == "pending_restart" and owner["active"] == saved["revision"]
    )
    assert (
        owner["desired"] == result["revision"]
        and owner["pending"] == result["operation"]
    )
    with pytest.raises(Conflict):
        service.save("user", batch["id"], saved["revision"], current, "while-pending")


def test_copy_keeps_retrieval_and_owns_new_prompt_bytes(factory):
    service, a = new(factory)
    _, _, document, _, _ = service.read("user", a["id"])
    source = edit_questions(document, 7)
    source = change(source, "retrieval", ordinary={"top_k": 999})
    published = service.save("user", a["id"], a["active"], source, "customize")
    b = service.create(
        "user",
        "B",
        "b",
        copies={"recipe": {"batch": a["id"], "revision": published["revision"]}},
    )
    _, record, target, copied, _ = service.read("user", b["id"])
    assert target.retrieval == factory[2].retrieval
    assert target.recipe["workflow"]["parameters"]["questions_per_source"] == 7
    assert target.recipe["workflow"]["parameters"]["scenario"][
        "prompt_file"
    ].startswith(record["directory"])
    path = Path(source.recipe["workflow"]["parameters"]["scenario"]["prompt_file"])
    path.write_text("manual tampering")
    assert service.read("user", b["id"])[3] == copied
    with pytest.raises(IntegrityError):
        service.read("user", a["id"], a["active"])


@pytest.mark.parametrize(
    "group", ["retrieval", "workspace", "collection", "topic_catalog_file"]
)
def test_unreusable_groups_are_rejected(factory, group):
    service, a = new(factory)
    with pytest.raises(ValueError):
        service.create(
            "user",
            "B",
            "bad-" + group,
            copies={group: {"batch": a["id"], "revision": a["active"]}},
        )


def test_preview_does_not_publish_and_rechecks_source_permission(factory):
    service, a = new(factory)
    b = service.create("user", "B", "b")
    before = list(factory[1].config_root.rglob("manifest.json"))
    preview = service.preview(
        "user",
        b["id"],
        b["active"],
        copies={"recipe": {"batch": a["id"], "revision": a["active"]}},
    )
    assert preview["differences"] == []
    assert list(factory[1].config_root.rglob("manifest.json")) == before
    denied = factory[0](authorize=lambda actor, action, resource: resource != a["id"])
    with pytest.raises(PermissionError):
        denied.save(
            "user",
            b["id"],
            b["active"],
            Configuration.model_validate(preview["draft"]),
            "denied",
            copies=preview["sources"],
        )


def test_private_prompt_paths_cannot_be_used_in_another_batch(factory):
    service, a = new(factory)
    b = service.create("user", "B", "b")
    _, _, source, _, _ = service.read("user", a["id"])
    _, _, target, _, _ = service.read("user", b["id"])
    data = target.model_dump()
    data["recipe"]["workflow"]["parameters"]["scenario"]["prompt_file"] = source.recipe[
        "workflow"
    ]["parameters"]["scenario"]["prompt_file"]
    with pytest.raises(PermissionError):
        service.save(
            "user", b["id"], b["active"], Configuration.model_validate(data), "escape"
        )


def test_queue_freezes_revision_and_idempotent_retry_survives_config_change(factory):
    service, a = new(factory)
    source = {
        "type": "retrieval",
        "data_batch_id": a["id"],
        "retrieval_job_id": "retrieval-test",
    }
    task = service.register_task(
        "user", a["id"], a["active"], "task-key", "distillation", source
    )
    _, _, config, _, _ = service.read("user", a["id"])
    changed = service.save(
        "user", a["id"], a["active"], edit_questions(config, 8), "save"
    )
    assert changed["revision"] != task["config_revision_id"]
    assert task["state"] == "queued" and len(task["prompt_digests"]) == 4
    assert (
        service.register_task(
            "user", a["id"], a["active"], "task-key", "distillation", source
        )
        == task
    )
    with pytest.raises(Conflict):
        service.register_task(
            "user", a["id"], a["active"], "new-task", "distillation", source
        )
    with pytest.raises(ValueError):
        service.register_task(
            "user",
            a["id"],
            changed["revision"],
            "foreign",
            "distillation",
            {**source, "data_batch_id": "batch-foreign"},
        )


def test_two_simultaneous_apply_requests_have_one_winner_and_safe_orphan(factory):
    service, a = new(factory)
    _, _, document, _, _ = service.read("user", a["id"])

    def save(number):
        try:
            return service.save(
                "user",
                a["id"],
                a["active"],
                edit_questions(document, number),
                "save-" + str(number),
            )["revision"]
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [5, 6]))
    assert sum(value is not None for value in results) == 1
    assert service.registry.get(a["id"])["active"] in results
    # An unregistered complete directory is never used to infer active state.
    assert all(Path(path).is_dir() for path in service.list_orphans("user"))


def test_file_publish_before_registry_failure_does_not_activate_configuration(factory):
    service, a = new(factory)
    _, _, document, _, _ = service.read("user", a["id"])
    with patch.object(
        service.registry,
        "publish",
        side_effect=RuntimeError("injected registration failure"),
    ):
        with pytest.raises(RuntimeError):
            service.save(
                "user", a["id"], a["active"], edit_questions(document, 5), "fail"
            )
    assert service.registry.get(a["id"])["active"] == a["active"]
    assert len(service.list_orphans("user")) == 1


@pytest.mark.parametrize("version", [2, 3])
def test_old_override_formats_are_rejected(factory, version):
    with pytest.raises(ValueError, match="Only current v4 settings"):
        import_overrides(
            factory[2].recipe,
            {
                "schema_version": version,
                "values": {"batch_size": 4},
            },
        )


def test_v4_override_values_are_preserved(factory):
    imported = import_overrides(
        factory[2].recipe,
        {
            "schema_version": 4,
            "workflow_name": "difficulty_routed_qa",
            "values": {"questions_per_source": 8},
        },
    )
    assert imported["workflow"]["parameters"]["questions_per_source"] == 8


@pytest.mark.parametrize("enabled", [False, True])
def test_current_overrides_reject_removed_deduplication(factory, enabled):
    with pytest.raises(ValueError, match="Incompatible v4 settings"):
        import_overrides(
            factory[2].recipe,
            {
                "schema_version": 4,
                "values": {"deduplicate_qa": enabled},
            },
        )


def test_artifact_tampering_fails_before_using_a_changed_recipe(factory):
    service, a = new(factory)
    _, record, _, _, _ = service.read("user", a["id"])
    (Path(record["directory"]) / "rag.yaml").write_text("chunk_size: 1")
    with pytest.raises(IntegrityError):
        service.read("user", a["id"])


def test_fields_and_admission_lease_invariants():
    fields = describe_fields()
    assert (
        next(field for field in fields if field["key"] == "retrieval")["reusable"]
        is False
    )
    assert any(field["key"] == "recipe.inspection_instructions" for field in fields)
    with pytest.raises(ValueError):
        AdmissionDecision(state="granted")
    with pytest.raises(ValueError):
        AdmissionDecision(state="queued", lease_id="lease-1")


def test_rejected_edit_does_not_publish_a_revision(factory):
    service, a = new(factory)
    _, _, document, _, _ = service.read("user", a["id"])
    data = document.model_dump()
    data["recipe"]["workflow"]["parameters"]["expert_parallelism"] = 2
    count = len(list(factory[1].config_root.rglob("manifest.json")))
    with pytest.raises(ValueError):
        service.save(
            "user", a["id"], a["active"], Configuration.model_validate(data), "invalid"
        )
    assert len(list(factory[1].config_root.rglob("manifest.json"))) == count


def test_simultaneous_create_clicks_are_idempotent(factory):
    service = factory[0]()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: service.create("user", "One batch", "one-click"), range(4)
            )
        )
    assert len({row["id"] for row in results}) == 1
    assert service.registry.get(results[0]["id"])["lifecycle"] == "registered"


def test_save_records_parent_actor_and_user_edits(factory):
    service, a = new(factory)
    _, _, config, _, _ = service.read("user", a["id"])
    saved = service.save(
        "user", a["id"], a["active"], edit_questions(config, 6), "edited"
    )
    manifest = service.read("user", a["id"], saved["revision"])[4]
    assert manifest["sources"]["actor"] == "user"
    assert manifest["sources"]["parent_revision"] == a["active"]
    assert any(
        row["field"].endswith("questions_per_source")
        for row in manifest["sources"]["user_edits"]
    )


def test_source_references_cannot_silently_drift(factory):
    service, a = new(factory)
    service.catalog.profiles["embed-v1"]["dimensions"] = 16
    with pytest.raises(Conflict):
        service.create(
            "user",
            "B",
            "b",
            copies={"rag": {"batch": a["id"], "revision": a["active"]}},
        )


def test_environment_commit_mismatch_fails_before_publication(factory):
    service = factory[0]()
    original = service.probe
    service.probe = lambda settings, operation, recipe=None: (
        {"installed_commit": "wrong-commit"}
        if operation == "describe"
        else original(settings, operation, recipe)
    )
    with pytest.raises(Conflict):
        service.create("user", "Bad environment", "bad-env")
    assert not list(factory[1].config_root.rglob("manifest.json"))


def test_deny_all_has_no_filesystem_side_effects(factory):
    service = factory[0](authorize=lambda *args: False)
    with pytest.raises(PermissionError):
        service.create("user", "Denied", "denied")
    assert not factory[1].config_root.exists()
    assert not factory[1].runtime_root.exists()


@pytest.mark.parametrize(
    "source",
    [
        {"data_batch_id": "batch-a", "type": "directory", "input_ids": "abc"},
        {"data_batch_id": "batch-a", "type": "directory", "input_ids": []},
        {"data_batch_id": "batch-a", "type": "retrieval"},
        {
            "data_batch_id": "batch-a",
            "type": "retrieval",
            "retrieval_job_id": "job-a",
            "input_ids": ["extra"],
        },
    ],
)
def test_task_source_contract_rejects_ambiguous_sources(source):
    from lightrag.batch_control.contracts import TaskSource

    with pytest.raises(ValueError):
        TaskSource.model_validate(source)


@pytest.mark.parametrize(
    "value",
    [
        {"api_key": "do-not-store"},
        {"api_url": "https://user:password@example.invalid/v1"},
        {"api_url": "https://example.invalid/v1?api_key=do-not-store"},
    ],
)
def test_configuration_rejects_inline_credentials(value):
    from lightrag.batch_control.service import no_plaintext_secrets

    with pytest.raises(ValueError):
        no_plaintext_secrets(value)


@pytest.mark.parametrize(
    "kind,provider,tokens", [("model_request", None, 1), ("ingest", None, 1)]
)
def test_admission_budget_contract_rejects_ambiguous_budget(kind, provider, tokens):
    from lightrag.batch_control.contracts import AdmissionRequest

    with pytest.raises(ValueError):
        AdmissionRequest(
            task_id="task-a",
            data_batch_id="batch-a",
            config_revision_id="rev-a",
            kind=kind,
            provider_group=provider,
            estimated_tokens=tokens,
        )


def test_granted_model_budget_contract_has_explicit_group():
    from lightrag.batch_control.contracts import AdmissionRequest

    request = AdmissionRequest(
        task_id="task-a",
        data_batch_id="batch-a",
        config_revision_id="rev-a",
        kind="model_request",
        provider_group="provider-account",
        estimated_tokens=100,
    )
    assert request.units == 1


def test_apply_and_queue_records_remain_readable_and_batch_scoped(factory):
    service, a = new(factory)
    b = service.create("user", "B", "b")
    _, _, config, _, _ = service.read("user", a["id"])
    task = service.register_task(
        "user",
        a["id"],
        a["active"],
        "queue",
        "distillation",
        {
            "type": "retrieval",
            "data_batch_id": a["id"],
            "retrieval_job_id": "retrieval-a",
        },
    )
    pending = service.save(
        "user", a["id"], a["active"], change(config, "rag", chunk_size=1400), "pending"
    )
    assert (
        service.operation("user", a["id"], pending["operation"])["status"]
        == "pending_restart"
    )
    assert service.task("user", a["id"], task["task_id"]) == task
    with pytest.raises(KeyError):
        service.operation("user", b["id"], pending["operation"])
    with pytest.raises(KeyError):
        service.task("user", b["id"], task["task_id"])


def test_separate_recipe_and_bridge_sources_keep_target_retrieval(factory):
    service, a = new(factory)
    b = service.create("user", "B", "b")
    c = service.create("user", "C", "c")
    _, _, a_doc, _, _ = service.read("user", a["id"])
    _, _, b_doc, _, _ = service.read("user", b["id"])
    a_saved = service.save(
        "user", a["id"], a["active"], edit_questions(a_doc, 9), "a-settings"
    )
    b_saved = service.save(
        "user",
        b["id"],
        b["active"],
        change(b_doc, "bridge", max_active_jobs=3),
        "b-settings",
    )
    preview = service.preview(
        "user",
        c["id"],
        c["active"],
        copies={
            "recipe": {"batch": a["id"], "revision": a_saved["revision"]},
            "bridge": {"batch": b["id"], "revision": b_saved["revision"]},
        },
    )
    saved = service.save(
        "user",
        c["id"],
        c["active"],
        Configuration.model_validate(preview["draft"]),
        "copy-both",
        copies=preview["sources"],
    )
    _, _, document, _, manifest = service.read("user", c["id"], saved["revision"])
    assert document.bridge.max_active_jobs == 3
    assert document.recipe["workflow"]["parameters"]["questions_per_source"] == 9
    assert document.retrieval == factory[2].retrieval
    assert set(manifest["sources"]["copied_groups"]) == {"recipe", "bridge"}


def test_recipe_only_copy_does_not_require_use_of_source_private_credentials(
    factory, tmp_path
):
    service, a = new(factory)
    b = service.create("user", "B", "b")
    credential = tmp_path / "private.env"
    credential.write_text("PRIVATE_VALUE=not-read-by-copy")
    service.catalog.credentials["source-private"] = credential
    _, _, document, _, _ = service.read("user", a["id"])
    saved = service.save(
        "user",
        a["id"],
        a["active"],
        change(document, "bridge", credential_id="source-private"),
        "private-bridge",
    )
    service.authorize = lambda actor, action, resource: action != "use_credential"
    preview = service.preview(
        "user",
        b["id"],
        b["active"],
        copies={"recipe": {"batch": a["id"], "revision": saved["revision"]}},
    )
    assert preview["draft"]["bridge"]["credential_id"] is None
    with pytest.raises(PermissionError):
        service.preview(
            "user",
            b["id"],
            b["active"],
            copies={"bridge": {"batch": a["id"], "revision": saved["revision"]}},
        )


def test_model_field_metadata_is_forwarded_from_selected_checkout_schema():
    selected = {
        "model_schema": {
            "type": "object",
            "properties": {
                "max_workers": {"type": "integer", "exclusiveMinimum": 0, "default": 10}
            },
        }
    }
    metadata = next(
        row for row in describe_fields(selected) if row["key"] == "recipe.models"
    )
    assert metadata["schema"]["additionalProperties"] == selected["model_schema"]
