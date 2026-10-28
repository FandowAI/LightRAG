"""Prompt publication and task snapshots do not expose arbitrary files or mutable text."""

from pathlib import Path

import pytest

from lightrag.integrations.data_workflow.config import (
    load_settings,
    write_job_config,
    read_yaml,
)
from lightrag.integrations.data_workflow.prompt_files import PromptFiles
from lightrag.integrations.data_workflow.prompt_files import materialize_builtin_prompts
from lightrag.integrations.data_workflow.recipe_schema import PROMPT_VARIABLES

pytestmark = pytest.mark.offline


def library(bridge):
    _, configuration, _, _ = bridge
    return PromptFiles(load_settings(configuration), "test-workspace")


def test_upload_preview_edit_and_reuse_are_immutable(bridge):
    prompts = library(bridge)
    first = prompts.save("scenario", "场景.txt", "Context: $text\n$payload\nLiteral $$")
    assert "预览原文" in first["preview"]
    assert first["preview"].endswith("Literal $")
    assert first["variables"] == list(PROMPT_VARIABLES["scenario"])
    assert (
        prompts.save("scenario", "again.md", first["content"])["path"] == first["path"]
    )
    edited = prompts.save("scenario", "场景.md", "Edited: $text\n$payload")
    assert edited["path"] != first["path"]
    assert Path(first["path"]).read_text() == first["content"]
    assert len(prompts.list("scenario")["items"]) == 2
    assert Path(edited["path"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "content", ["", " ", "$unknown", "${", "x" * (256 * 1024 + 1), "汉" * 100000]
)
def test_invalid_prompt_does_not_publish_files(bridge, content):
    prompts = library(bridge)
    with pytest.raises(ValueError):
        prompts.save("qa", "qa.md", content)
    assert not prompts.root.exists()


def test_paths_symlinks_and_other_workspaces_are_not_readable(bridge, tmp_path):
    prompts = library(bridge)
    outside = tmp_path / "credentials.env"
    outside.write_text("PRIVATE_KEY=not-a-prompt")
    other = PromptFiles(prompts_settings := load_settings(bridge[1]), "other-workspace")
    document = other.save("qa", "qa.md", "$text")
    for path in (
        str(outside),
        str(prompts.root / "../.." / outside.name),
        document["path"],
    ):
        with pytest.raises(ValueError):
            prompts.read("qa", path)
    prompts.root.mkdir(parents=True)
    link = prompts.root / "qa-link.md"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        prompts.read("qa", str(link))
    with pytest.raises(ValueError):
        prompts.save("qa", "../../recipe.yaml", "$text")
    assert prompts_settings.workflow_config == bridge[3]


def test_job_freezes_prompt_and_keeps_config_bytes_under_config_root(bridge, tmp_path):
    _, _, _, recipe = bridge
    prompts = library(bridge)
    document = prompts.save("scenario", "scenario.md", "Original $payload")
    content = read_yaml(recipe)
    content["workflow"] = {
        "name": "difficulty_routed_qa",
        "parameters": {"scenario": {"prompt_file": document["path"]}},
    }
    job = tmp_path / "runs/workspace/job-one"
    job.mkdir(parents=True)
    snapshot = write_job_config(recipe, job, recipe=content)
    frozen = Path(snapshot["workflow"]["parameters"]["scenario"]["prompt_file"])
    assert frozen.is_relative_to(recipe.parent / "jobs")
    assert (job / "job.yaml").resolve().is_relative_to(recipe.parent / "jobs")
    prompts.save("scenario", "scenario.md", "Edited $payload")
    assert frozen.read_text() == "Original $payload"
    assert read_yaml(job / "job.yaml") == snapshot


def test_builtin_prompts_are_copied_without_preparing_workflow_or_topic_files(
    bridge, monkeypatch
):
    settings = load_settings(bridge[1])
    stages = {
        stage: {
            "model": "generator",
            "prompt": "RegisteredPrompt",
            "parser": "test.v1",
            "prompt_file": None,
        }
        for stage in PROMPT_VARIABLES
    }
    recipe = {"workflow": {"name": "difficulty_routed_qa", "parameters": stages}}
    calls = []

    def probe(settings, operation, document):
        calls.append(operation)
        return {"prompts": {stage: f"{stage}: $payload" for stage in PROMPT_VARIABLES}}

    monkeypatch.setattr(
        "lightrag.integrations.data_workflow.prompt_files.probe_schema", probe
    )
    result = materialize_builtin_prompts(settings, recipe)
    assert calls == ["prompt_defaults"]
    assert all(
        Path(task["prompt_file"]).read_text().endswith("$payload")
        for task in result["workflow"]["parameters"].values()
    )
    assert "topic_catalog_file" not in result["workflow"]["parameters"]
    materialize_builtin_prompts(settings, result)
    assert calls == ["prompt_defaults"]
