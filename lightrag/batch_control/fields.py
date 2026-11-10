"""One group policy for preview, copy and apply classification."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass

from .contracts import BridgeConfig, Configuration, RagConfig
from ..integrations.data_workflow.recipe_settings import RecipeValues, apply_values
from ..integrations.data_workflow.recipe_schema import (
    PARAMETER_FIELDS,
    RUNTIME_FIELDS,
    STAGES,
    WORKFLOW,
)


@dataclass(frozen=True)
class FieldRule:
    key: str
    label: str
    group: str
    editable: bool
    reusable: bool
    effect: str
    unit: str | None = None


LABELS = {
    "parse_engine": "解析方式",
    "chunk_size": "分块大小",
    "chunk_overlap_size": "重叠长度",
    "chunking_strategy": "分块策略",
    "max_parallel_insert": "文档并行数",
    "max_parallel_parse_native": "原生解析并行数",
    "model_profiles": "模型档案引用",
    "role_profiles": "模型角色绑定",
    "role_concurrency": "角色请求并发",
    "embedding_profile": "向量模型档案引用",
    "environment_id": "执行环境引用",
    "credential_id": "凭据引用",
    "input_ids": "输入来源引用",
    "records_path": "来源封装字段路径",
    "max_input_bytes": "输入字节上限",
    "max_records": "输入记录上限",
    "max_active_jobs": "批次蒸馏任务上限",
    "timeout_seconds": "执行超时",
    "questions_per_source": "每条来源最多题数",
    "scenario_instructions": "场景要求",
    "classification_instructions": "分类要求",
    "inspection_instructions": "单次检查要求",
    "seed_question_path": "种子问题路径",
    "batch_size": "来源批次大小",
    "max_inflight_requests": "作业内总请求并发",
    "max_prompt_chars": "提示词字符上限",
    "log_level": "日志级别",
    "models": "模型档案",
    "stages": "四阶段绑定",
}
UNITS = {
    "chunk_size": "tokens",
    "chunk_overlap_size": "tokens",
    "timeout_seconds": "seconds",
    "max_input_bytes": "bytes",
    "max_prompt_chars": "characters",
}


def describe_fields(workflow_schema: dict | None = None) -> list[dict]:
    rules = []
    for model, group, effect in (
        (RagConfig, "rag", "restart"),
        (BridgeConfig, "bridge", "new_task"),
    ):
        schema = model.model_json_schema()
        for key, field in schema["properties"].items():
            rules.append(
                {
                    **asdict(
                        FieldRule(
                            f"{group}.{key}",
                            LABELS[key],
                            group,
                            True,
                            True,
                            "index_compatibility"
                            if key == "embedding_profile"
                            else effect,
                            UNITS.get(key),
                        )
                    ),
                    "schema": deepcopy(field),
                }
            )
    schema = RecipeValues.model_json_schema()
    if workflow_schema and workflow_schema.get("model_schema"):
        schema["properties"]["models"] = {
            "type": "object",
            "additionalProperties": deepcopy(workflow_schema["model_schema"]),
        }
    for key in RecipeValues.model_fields:
        rules.append(
            {
                **asdict(
                    FieldRule(
                        f"recipe.{key}",
                        LABELS[key],
                        "recipe",
                        True,
                        True,
                        "new_task",
                        UNITS.get(key),
                    )
                ),
                "schema": deepcopy(schema["properties"][key]),
                "definitions": schema.get("$defs", {}),
            }
        )
    rules.append(
        asdict(
            FieldRule(
                "retrieval",
                "检索（仅当前批次）",
                "retrieval",
                True,
                False,
                "new_request",
            )
        )
    )
    return rules


def import_overrides(recipe: dict, saved: dict | None) -> dict:
    result = deepcopy(recipe)
    if saved is None:
        return result
    version = saved.get("schema_version")
    if version != 4:
        raise ValueError("Only current v4 settings are supported")
    values = saved.get("values")
    if not isinstance(values, dict):
        raise ValueError("Settings values must be an object")
    allowed = set(RecipeValues.model_fields)
    if set(values) - allowed or saved.get("workflow_name") != WORKFLOW:
        raise ValueError("Incompatible v4 settings")
    params, runtime = result["workflow"]["parameters"], result.get("runtime", {})
    defaults = RecipeValues().model_dump()
    base = {key: params.get(key, defaults[key]) for key in PARAMETER_FIELDS}
    base.update({key: runtime.get(key, defaults[key]) for key in RUNTIME_FIELDS})
    base["models"] = result.get("models", {})
    base["stages"] = {key: params[key] for key in STAGES if key in params}
    base.update({key: value for key, value in values.items() if key in allowed})
    return apply_values(result, RecipeValues.model_validate(base))


def validate_recipe_shape(document: dict) -> None:
    if (
        document.get("schema_version") != 3
        or document.get("workflow", {}).get("name") != WORKFLOW
    ):
        raise ValueError("Only difficulty_routed_qa schema 3 can be edited")
    params = document["workflow"]["parameters"]
    allowed = set(PARAMETER_FIELDS) | set(STAGES) | {"fields", "topic_catalog_file"}
    if set(params) - allowed:
        raise ValueError("Unsupported workflow parameters")
    values = {key: params[key] for key in PARAMETER_FIELDS if key in params}
    values.update(
        {
            key: document.get("runtime", {})[key]
            for key in RUNTIME_FIELDS
            if key in document.get("runtime", {})
        }
    )
    values.update(
        models=document.get("models", {}),
        stages={key: params[key] for key in STAGES if key in params},
    )
    typed = RecipeValues.model_validate(values)
    if set(typed.stages) != set(STAGES):
        raise ValueError("All four stages must be present")
    for stage in typed.stages.values():
        if stage.model not in typed.models:
            raise ValueError("Stage references an unknown model profile")
    # Full editable model/protocol validation is delegated to the selected checkout.


def copy_groups(
    target: Configuration, source: Configuration, groups: list[str]
) -> Configuration:
    if (
        not groups
        or len(groups) != len(set(groups))
        or set(groups) - {"rag", "recipe", "bridge"}
    ):
        raise ValueError(
            "Only rag, recipe and bridge are reusable; retrieval is never copied"
        )
    document = target.model_dump()
    origin = source.model_dump()
    for group in groups:
        document[group] = deepcopy(origin[group])
    # Unopened administrator fields belong to the target, including an absent value.
    if "recipe" in groups:
        src = document["recipe"]["workflow"]["parameters"]
        dst = target.recipe["workflow"]["parameters"]
        src.pop("topic_catalog_file", None)
        if "topic_catalog_file" in dst:
            src["topic_catalog_file"] = dst["topic_catalog_file"]
        src["fields"] = deepcopy(
            dst.get("fields", {"text_path": ["text"], "meta_path": ["meta"]})
        )
    return Configuration.model_validate(document)


def differences(before: dict, after: dict, prefix: str = "") -> list[dict]:
    result = []
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key), after.get(key)
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(a, dict) and isinstance(b, dict):
            result.extend(differences(a, b, path))
        elif a != b or (key in before) != (key in after):
            result.append({"field": path, "before": a, "after": b})
    return result
