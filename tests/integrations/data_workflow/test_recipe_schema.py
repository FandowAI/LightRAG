"""Probe the selected interpreter without assuming retired workflow parameters."""

import pytest

from lightrag.integrations.data_workflow.config import load_settings
from lightrag.integrations.data_workflow.recipe_schema import probe_schema

pytestmark = pytest.mark.offline


@pytest.fixture
def installed_schema(bridge):
    settings = load_settings(bridge[1])
    package = settings.project_directory / "src/data_studio/config"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "schema.py").write_text("""
from typing import Literal
from pydantic import BaseModel

class WorkflowConfig(BaseModel):
    name: str
    parameters: dict

class StudioConfig(BaseModel):
    schema_version: Literal[3]
    workflow: WorkflowConfig
    models: dict = {}

class RuntimeConfig(BaseModel):
    batch_size: int = 32
    max_inflight_requests: int = 16
    max_prompt_chars: int | None = None
    log_level: str = "INFO"

class ModelConfig(BaseModel):
    model_name: str

class TaskConfig(BaseModel):
    model: str
    prompt: str
    parser: str
""")
    parameters = package / "difficulty_routed_qa.py"
    parameters.write_text("""
from pydantic import BaseModel

class DifficultyRoutedQAParameters(BaseModel):
    topic_catalog_file: str
    questions_per_source: int = 3
    scenario_instructions: str = "Generate scenarios"
    classification_instructions: str = "Classify QA"
    inspection_instructions: str = "Inspect QA"
    seed_question_path: list[str | int] | None = None
""")
    (package / "topics.py").write_text("""
from pydantic import BaseModel

class CategoryCatalog(BaseModel):
    categories: list[dict]
""")
    (package.parent / "__init__.py").write_text("""
from pathlib import Path
from types import SimpleNamespace
import json
from .config.difficulty_routed_qa import DifficultyRoutedQAParameters
from .config.topics import CategoryCatalog

def prepare_job(config, *, base_directory, environ):
    parameters = DifficultyRoutedQAParameters.model_validate(config.workflow.parameters)
    path = Path(base_directory) / parameters.topic_catalog_file
    catalog = CategoryCatalog.model_validate(json.loads(path.read_text()))
    return SimpleNamespace(parameters=SimpleNamespace(topic_catalog=catalog))
""")
    return settings, parameters


def test_describe_accepts_current_workflow_without_deduplication(installed_schema):
    settings, _ = installed_schema
    result = probe_schema(settings, "describe")
    assert result["schema_version"] == 3
    assert result["defaults"]["questions_per_source"] == 3
    assert result["defaults"]["max_inflight_requests"] == 16
    assert "deduplicate_qa" not in result["defaults"]
    assert "model_name" in result["model_schema"]["properties"]


def test_incompatible_probe_reports_the_actual_missing_field(installed_schema):
    settings, parameters = installed_schema
    parameters.write_text(
        parameters.read_text().replace(
            '    scenario_instructions: str = "Generate scenarios"\n', ""
        )
    )
    with pytest.raises(ValueError, match="incompatible.*scenario_instructions"):
        probe_schema(settings, "describe")


def test_validation_rejects_missing_required_catalog(installed_schema):
    settings, _ = installed_schema
    result = probe_schema(
        settings,
        "validate",
        {
            "schema_version": 3,
            "workflow": {"name": "difficulty_routed_qa", "parameters": {}},
        },
    )
    assert result["valid"] is False
    assert "topic_catalog_file" in "; ".join(result["issues"])


def test_validation_prepares_catalog_and_allows_optional_defaults(installed_schema):
    settings, _ = installed_schema
    catalog = settings.workflow_config.parent / "categories.json"
    catalog.write_text('{"categories": [{"id": "operations"}]}')
    recipe = {
        "schema_version": 3,
        "workflow": {
            "name": "difficulty_routed_qa",
            "parameters": {"topic_catalog_file": catalog.name},
        },
    }
    result = probe_schema(settings, "validate", recipe)
    assert result["valid"] is True
    assert result["validation_scope"] == "workflow_preparation_without_model_requests"
    catalog.unlink()
    result = probe_schema(settings, "validate", recipe)
    assert result["valid"] is False
