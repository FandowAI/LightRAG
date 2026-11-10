"""Offline catalogs and workflow schema probes; no credentials or model calls."""

from copy import deepcopy
import sys

import pytest

from lightrag.batch_control.contracts import Configuration, Environment
from lightrag.batch_control.paths import ControlPaths
from lightrag.batch_control.service import BatchControl, Catalog


@pytest.fixture
def factory(tmp_path):
    paths = ControlPaths(tmp_path / "config", tmp_path / "runs")
    project = tmp_path / "workflow"
    project.mkdir()
    environment = Environment(
        str(project), sys.executable, "offline-code", "offline-lock"
    )
    catalog = Catalog(
        environments={"test": environment},
        credentials={},
        inputs={},
        profiles={
            "embed-v1": {"model_name": "synthetic-embedding", "dimensions": 8},
            "llm-v1": {"model_name": "synthetic-llm", "credential_id": None},
        },
    )
    tasks = {
        stage: {
            "model": "generator",
            "prompt": prompt,
            "parser": parser,
            "prompt_file": None,
        }
        for stage, prompt, parser in (
            ("scenario", "ScenarioPrompt", "scenarios_json.v1"),
            ("qa", "GeneratePrompt", "qa_json.v1"),
            ("classification", "QAClassificationPrompt", "classification_json.v1"),
            ("inspection", "InspectAndRevisePrompt", "inspection_revision_json.v1"),
        )
    }
    defaults = Configuration.model_validate(
        {
            "rag": {
                "embedding_profile": "embed-v1",
                "model_profiles": {"primary": "llm-v1"},
                "role_profiles": {"extract": "primary"},
            },
            "retrieval": {
                "ordinary": {"mode": "mix", "top_k": 40},
                "batch": {"chunk_top_k": 20},
            },
            "recipe": {
                "schema_version": 3,
                "input": {"paths": ["unused"]},
                "output": {"directory": "unused"},
                "runtime": {"batch_size": 1, "max_inflight_requests": 2},
                "models": {
                    "generator": {
                        "api_url": "https://offline.invalid/v1/chat/completions",
                        "model_name": "synthetic",
                        "key_name_of_api_key": "OFFLINE_TEST_KEY",
                    }
                },
                "workflow": {
                    "name": "difficulty_routed_qa",
                    "parameters": {
                        **tasks,
                        "questions_per_source": 3,
                    },
                },
            },
            "bridge": {"environment_id": "test"},
        }
    )
    calls = []

    def probe(settings, operation, recipe=None):
        calls.append(operation)
        if operation == "prompt_defaults":
            return {"prompts": {stage: stage + ": $payload" for stage in tasks}}
        if operation == "describe":
            return {"installed_commit": "offline-code"}
        return {"valid": True, "issues": []}

    def build(authorize=None, defaults_override=None):
        return BatchControl(
            paths,
            defaults_override or deepcopy(defaults),
            catalog,
            authorize or (lambda actor, action, resource: True),
            probe=probe,
        )

    return build, paths, defaults, catalog, calls
