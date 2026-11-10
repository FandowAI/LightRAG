"""Editable overrides stay isolated from administrator-owned workflow settings."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from lightrag.integrations.data_workflow.config import read_yaml
from lightrag.integrations.data_workflow.recipe_settings import RecipeValues
from lightrag.integrations.data_workflow.service import DataWorkflowService
from lightrag.integrations.data_workflow.storage import JobConflict
from lightrag.integrations.data_workflow.worker import run

pytestmark = pytest.mark.offline


@pytest.fixture
def classified(bridge, monkeypatch):
    service, config, _, recipe = bridge
    catalog = recipe.parent / "categories.yaml"
    catalog.write_text("categories:\n  - id: operations\n")
    document = read_yaml(recipe)
    document["workflow"] = {
        "name": "difficulty_routed_qa",
        "parameters": {
            "questions_per_source": 1,
            "scenario_instructions": "Synthetic scenario",
            "classification_instructions": "Classify importance and difficulty",
            "topic_catalog_file": str(catalog),
            **{
                stage: {"model": "generator", "prompt": prompt, "parser": parser}
                for stage, prompt, parser in (
                    ("scenario", "ScenarioPrompt", "scenarios_json.v1"),
                    ("qa", "GeneratePrompt", "qa_json.v1"),
                    (
                        "classification",
                        "QAClassificationPrompt",
                        "classification_json.v1",
                    ),
                    (
                        "inspection",
                        "InspectAndRevisePrompt",
                        "inspection_revision_json.v1",
                    ),
                )
            },
        },
    }
    for stage, task in document["workflow"]["parameters"].items():
        if isinstance(task, dict) and "model" in task:
            path = recipe.parent / "prompts/defaults" / f"{stage}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("$payload")
            task["prompt_file"] = str(path)
    recipe.write_text(yaml.safe_dump(document))

    def probe(settings, operation, recipe=None):
        if operation == "describe":
            return {"schema_version": 3, "defaults": {}}
        return {"valid": True, "issues": []}

    probe_mock = Mock(side_effect=probe)
    monkeypatch.setattr(
        "lightrag.integrations.data_workflow.recipe_settings.probe_schema", probe_mock
    )
    return service, config, recipe, probe_mock


def values(snapshot):
    return RecipeValues.model_validate(
        {**snapshot["settings"], "questions_per_source": 3}
    )


def test_save_keeps_uneditable_recipe_values_and_does_not_generate_resources(
    classified,
):
    service, _, recipe, probe = classified
    original = recipe.read_bytes()
    snapshot = service.recipe_settings()
    assert snapshot["editable"]
    assert set(snapshot["settings"]) == set(RecipeValues.model_fields)
    assert not {"topic_catalog_file", "example_topics", "quality"} & snapshot.keys()
    saved = service.save_recipe_settings(snapshot["revision"], values(snapshot))
    assert saved["settings"]["questions_per_source"] == 3
    settings_file = Path(saved["settings_file"])
    assert settings_file.is_file() and settings_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(settings_file.read_text()) == {
        "schema_version": 4,
        "workflow_name": "difficulty_routed_qa",
        "values": saved["settings"],
    }
    assert list(settings_file.parent.glob("*.yaml")) == []
    assert recipe.read_bytes() == original
    candidate = next(
        call.args[2] for call in probe.call_args_list if call.args[1] == "validate"
    )
    original_params = read_yaml(recipe)["workflow"]["parameters"]
    assert (
        candidate["workflow"]["parameters"]["topic_catalog_file"]
        == original_params["topic_catalog_file"]
    )


def test_missing_catalog_validation_prevents_saving_settings(classified):
    service, _, recipe, probe = classified
    document = read_yaml(recipe)
    del document["workflow"]["parameters"]["topic_catalog_file"]
    recipe.write_text(yaml.safe_dump(document))
    snapshot = service.recipe_settings()
    probe.side_effect = lambda settings, operation, recipe=None: (
        {"schema_version": 3, "defaults": {}}
        if operation == "describe"
        else {"valid": False, "issues": ["topic_catalog_file: Field required"]}
    )
    with pytest.raises(ValueError, match="topic_catalog_file"):
        service.save_recipe_settings(snapshot["revision"], values(snapshot))
    assert not Path(snapshot["settings_file"]).exists()


def test_revision_conflicts_and_failed_validation_do_not_overwrite(classified):
    service, _, _, probe = classified
    before = service.recipe_settings()
    saved = service.save_recipe_settings(before["revision"], values(before))
    with pytest.raises(JobConflict):
        service.save_recipe_settings(before["revision"], values(before))
    probe.side_effect = lambda settings, operation, recipe=None: (
        {"schema_version": 3, "defaults": {}}
        if operation == "describe"
        else {"valid": False, "issues": ["Invalid workflow parameters"]}
    )
    with pytest.raises(ValueError, match="Invalid workflow parameters"):
        service.save_recipe_settings(saved["revision"], values(saved))
    assert service.recipe_settings()["revision"] == saved["revision"]


def test_settings_are_workspace_scoped_and_existing_jobs_keep_snapshots(
    classified, monkeypatch
):
    service, config, _, _ = classified
    monkeypatch.setattr(
        service,
        "_spawn",
        lambda store, record, operation: run(store, record["job_id"], operation),
    )
    snapshot = service.recipe_settings()
    saved = service.save_recipe_settings(snapshot["revision"], values(snapshot))
    job = service.submit("before-change", start=False)
    assert job["status"] == "ready"
    old_snapshot = Path(job["artifacts"]["config"]).read_bytes()
    changed = values(saved).model_copy(update={"questions_per_source": 5})
    service.save_recipe_settings(saved["revision"], changed)
    assert Path(job["artifacts"]["config"]).read_bytes() == old_snapshot
    new_job = service.submit("after-change", start=False)
    assert (
        read_yaml(Path(new_job["artifacts"]["config"]))["workflow"]["parameters"][
            "questions_per_source"
        ]
        == 5
    )
    other = DataWorkflowService("another-workspace", config)
    assert other.recipe_settings()["settings"]["questions_per_source"] == 1


def test_explicit_retry_keeps_failure_and_is_idempotent(classified, monkeypatch):
    service, _, _, _ = classified

    def fail(*args):
        raise RuntimeError("Fixture launch failed")

    monkeypatch.setattr(service, "_spawn", fail)
    failed = service.submit("failed-launch")
    assert failed["status"] == "failed"
    snapshot = service.recipe_settings()
    service.save_recipe_settings(snapshot["revision"], values(snapshot))
    monkeypatch.setattr(
        service,
        "_spawn",
        lambda store, record, operation: run(store, record["job_id"], operation),
    )
    retried = service.retry(failed["job_id"], "one-click", start=False)
    assert retried["status"] == "ready" and retried["job_id"] != failed["job_id"]
    assert (
        service.retry(failed["job_id"], "one-click", start=False)["job_id"]
        == retried["job_id"]
    )
    assert service.get(failed["job_id"])["status"] == "failed"
    with pytest.raises(JobConflict):
        service.retry(retried["job_id"], "invalid-retry")


@pytest.mark.parametrize("version", [2, 3])
def test_old_settings_formats_are_rejected(classified, version):
    service, _, _, _ = classified
    snapshot = service.recipe_settings()
    path = Path(snapshot["settings_file"])
    path.write_text(
        json.dumps(
            {
                "schema_version": version,
                "workflow_name": "difficulty_routed_qa",
                "values": snapshot["settings"],
            }
        )
    )
    with pytest.raises(ValueError, match="Unsupported recipe settings version"):
        service.recipe_settings()


@pytest.mark.parametrize("enabled", [False, True])
def test_removed_deduplication_field_is_rejected(enabled):
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        RecipeValues.model_validate({"deduplicate_qa": enabled})


def test_current_workspace_settings_reject_removed_fields(classified):
    service, _, _, _ = classified
    snapshot = service.recipe_settings()
    path = Path(snapshot["settings_file"])
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "workflow_name": "difficulty_routed_qa",
                "values": {**snapshot["settings"], "deduplicate_qa": False},
            }
        )
    )
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        service.recipe_settings()


def test_managed_recipe_defaults_remain_editable_and_survive_revision_paths(
    classified, tmp_path
):
    service, _, recipe, _ = classified
    before = recipe.read_bytes()
    service.config_root = tmp_path / "batch-business"
    first = service.recipe_settings()
    assert first["editable"]
    assert all(
        Path(v["prompt_file"]).is_relative_to(service.config_root)
        for v in first["settings"]["stages"].values()
    )
    saved = service.save_recipe_settings(first["revision"], values(first))
    assert saved["settings"]["questions_per_source"] == 3
    stage = saved["settings"]["stages"]["scenario"]["prompt_file"]
    assert service.read_prompt("scenario", stage)["content"] == "$payload"
    assert recipe.read_bytes() == before
    assert service.recipe_settings()["settings"]["questions_per_source"] == 3
