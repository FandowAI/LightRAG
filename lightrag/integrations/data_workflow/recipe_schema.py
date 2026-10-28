"""Inspect editable types using the selected data-work-flow checkout, without a run."""

from __future__ import annotations

import json
import os
import subprocess

from .config import BridgeSettings

WORKFLOW = "difficulty_routed_qa"
STAGES = ("scenario", "qa", "classification", "inspection")
PARAMETER_FIELDS = (
    "questions_per_source",
    "deduplicate_qa",
    "scenario_instructions",
    "classification_instructions",
    "inspection_instructions",
    "seed_question_path",
)
RUNTIME_FIELDS = (
    "batch_size",
    "max_inflight_requests",
    "max_prompt_chars",
    "log_level",
)
PROMPT_VARIABLES = {
    "scenario": ("text", "count", "instructions", "seed", "payload"),
    "qa": ("text", "scenario", "payload"),
    "classification": (
        "text",
        "question",
        "answer",
        "instructions",
        "topic_catalog",
        "payload",
    ),
    "inspection": (
        "text",
        "question",
        "answer",
        "classification",
        "instructions",
        "payload",
    ),
}

SCHEMA_PROBE = r"""
import json, sys, subprocess
from typing import get_args
from pydantic import TypeAdapter, ValidationError
from data_studio.config.schema import StudioConfig, RuntimeConfig, ModelConfig, TaskConfig
from data_studio.config.difficulty_routed_qa import DifficultyRoutedQAParameters

body = json.load(sys.stdin)
try:
    if body['operation'] == 'describe':
        defaults = {}
        for model, names in ((DifficultyRoutedQAParameters, body['parameter_fields']),
                             (RuntimeConfig, body['runtime_fields'])):
            for name in names:
                field = model.model_fields[name]
                if not field.is_required():
                    defaults[name] = field.default
        result = subprocess.run(['git', 'rev-parse', '--short=12', 'HEAD'], capture_output=True, text=True, timeout=2)
        print(json.dumps({'schema_version': get_args(StudioConfig.model_fields['schema_version'].annotation)[0],
            'installed_commit': result.stdout.strip() if result.returncode == 0 else None,
            'defaults': defaults, 'model_schema': ModelConfig.model_json_schema(),
            'task_schema': TaskConfig.model_json_schema()}))
    elif body['operation'] == 'prompt_defaults':
        from data_studio.workflows.qa.difficulty_routed_qa import register_difficulty_routed_qa_builtins
        from data_studio.operators.catalog import prompt_instance
        register_difficulty_routed_qa_builtins()
        parameters = body['recipe']['workflow']['parameters']
        prompts = {}
        for name in body['stages']:
            task = TaskConfig.model_validate(parameters[name], context={'base_directory': body['base_directory']})
            prompt = prompt_instance(task.prompt, name, task.parser)
            prompts[name] = prompt.path.read_text(encoding='utf-8')
        print(json.dumps({'prompts': prompts}))
    else:
        config = StudioConfig.model_validate(body['recipe'])
        if config.workflow.name != 'difficulty_routed_qa':
            raise ValueError('Only difficulty_routed_qa is editable')
        parameters = config.workflow.parameters
        allowed = set(DifficultyRoutedQAParameters.model_fields)
        if set(parameters) - allowed:
            raise ValueError('Unsupported workflow parameters: ' + ', '.join(sorted(set(parameters) - allowed)))
        for name in body['parameter_fields']:
            field = DifficultyRoutedQAParameters.model_fields[name]
            TypeAdapter(field.rebuild_annotation()).validate_python(parameters[name], strict=True)
        from data_studio.workflows.qa.difficulty_routed_qa import register_difficulty_routed_qa_builtins
        from data_studio.operators.catalog import prompt_instance
        register_difficulty_routed_qa_builtins()
        for name in body['stages']:
            task = TaskConfig.model_validate(parameters[name], context={'base_directory': body['base_directory']})
            if task.model not in config.models:
                raise ValueError('Stage references an unknown model profile: ' + name)
            prompt_instance(task.prompt, name, task.parser, prompt_file=task.prompt_file)
        print(json.dumps({'valid': True, 'issues': []}))
except ValidationError as error:
    issues = ['.'.join(str(p) for p in item['loc']) + ': ' + item['msg']
              for item in error.errors(include_input=False, include_context=False, include_url=False)]
    print(json.dumps({'valid': False, 'issues': issues}))
except Exception as error:
    print(json.dumps({'valid': False, 'issues': [str(error)]}))
"""


def probe_schema(
    settings: BridgeSettings, operation: str, recipe: dict | None = None
) -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(settings.project_directory / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [str(settings.python), "-c", SCHEMA_PROBE],
            input=json.dumps(
                {
                    "operation": operation,
                    "recipe": recipe,
                    "parameter_fields": PARAMETER_FIELDS,
                    "runtime_fields": RUNTIME_FIELDS,
                    "stages": STAGES,
                    "base_directory": str(settings.workflow_config.parent),
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            cwd=settings.project_directory,
            env=environment,
        )
        if result.returncode != 0:
            raise ValueError("Cannot load the installed difficulty_routed_qa schema")
        response = json.loads(result.stdout.strip().splitlines()[-1])
        if operation == "describe" and "schema_version" not in response:
            raise ValueError("The installed workflow schema is incompatible")
        return response
    except (
        subprocess.TimeoutExpired,
        OSError,
        IndexError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(
            "Cannot inspect the configured data-work-flow interpreter"
        ) from exc
