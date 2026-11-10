"""Nested recipe directories never relocate global configuration resources."""

import sys
from pathlib import Path

import pytest

from lightrag.batch_control.paths import ControlPaths
from lightrag.integrations.data_workflow.config import BridgeSettings, write_job_config
from lightrag.integrations.data_workflow.prompt_files import PromptFiles
from lightrag.integrations.data_workflow.recipe_settings import RecipeSettingsStore

pytestmark = pytest.mark.offline


def test_explicit_roots_for_legacy_bridge_helpers(tmp_path):
    config = tmp_path / "config"
    template = config / "batches/batch-a/revisions/rev-a/job.yaml"
    template.parent.mkdir(parents=True)
    settings = BridgeSettings(
        project_directory=tmp_path,
        python=Path(sys.executable),
        workflow_config=template,
        jobs_directory=tmp_path / "runs",
    )
    prompts = PromptFiles(settings, "namespace-a", config_root=config)
    saved = prompts.save("scenario", "scenario.md", "$payload")
    assert Path(saved["path"]).is_relative_to(config / "prompts/namespace-a")
    store = RecipeSettingsStore(settings, tmp_path / "namespace-a", config_root=config)
    assert store.path == config / "workspaces/namespace-a/recipe-settings.json"
    job = tmp_path / "runs/namespace-a/distill-a"
    job.mkdir(parents=True)
    recipe = {
        "schema_version": 3,
        "workflow": {
            "name": "difficulty_routed_qa",
            "parameters": {"scenario": {"prompt_file": saved["path"]}},
        },
    }
    snapshot = write_job_config(template, job, recipe=recipe, config_root=config)
    assert (job / "job.yaml").resolve().is_relative_to(config / "jobs")
    assert Path(
        snapshot["workflow"]["parameters"]["scenario"]["prompt_file"]
    ).is_relative_to(config / "jobs")
    assert not (template.parent / "prompts").exists()
    assert not (template.parent / "jobs").exists()


@pytest.mark.parametrize("part", ["../escape", "..", "/etc/passwd", "a/b", ""])
def test_path_components_cannot_escape(tmp_path, part):
    paths = ControlPaths(tmp_path / "config", tmp_path / "runs")
    with pytest.raises(ValueError):
        paths.revision(part, "rev-a")


def test_symlink_root_and_parent_escape_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        ControlPaths(link, tmp_path / "runs")
    root = tmp_path / "config"
    root.mkdir()
    (root / "batches").symlink_to(outside)
    paths = ControlPaths(root, tmp_path / "runs")
    with pytest.raises(ValueError):
        paths.revision("batch-a", "rev-a")
