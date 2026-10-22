"""Project query/data results to evidence-only text without rendering a Prompt."""

from __future__ import annotations

import json
from typing import Any


def _items(data: dict, key: str) -> list[dict]:
    values = data.get(key, [])
    if not isinstance(values, list) or any(
        not isinstance(item, dict) for item in values
    ):
        raise ValueError(f"data.{key} must be an array of objects")
    return values


def _text(item: dict, *keys: str, default: str | None = None) -> str:
    for key in keys:
        if key in item:
            value = item[key]
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            return value
    if default is not None:
        return default
    raise ValueError(f"Missing field: {keys[0]}")


def _name(item: dict, *keys: str) -> str:
    value = _text(item, *keys)
    if not value.strip():
        raise ValueError(f"{keys[0]} must not be blank")
    return value


def convert_result(result: Any) -> dict[str, str]:
    """Match the query context's field projection, preserving text and source order.

    JSONL has one text-only record per retrieval result. Reference IDs, filenames,
    metadata and answer-generation instructions never enter the delivered envelope.
    """
    if not isinstance(result, dict) or result.get("status") != "success":
        raise ValueError("Expected a successful /query/data result")
    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError("Query result data must be an object")
    entities = [
        {
            "entity": _name(item, "entity_name", "entity"),
            "type": _text(item, "entity_type", "type", default="UNKNOWN"),
            "description": _text(item, "description", default=""),
        }
        for item in _items(data, "entities")
    ]
    relationships = [
        {
            "entity1": _name(item, "src_id", "entity1"),
            "entity2": _name(item, "tgt_id", "entity2"),
            "description": _text(item, "description", default=""),
        }
        for item in _items(data, "relationships")
    ]
    chunks = [_name(item, "content") for item in _items(data, "chunks")]
    if not chunks:
        raise ValueError("At least one original text chunk is required")
    context = {
        "entities": entities,
        "relationships": relationships,
        "text_chunks": chunks,
    }
    return {"text": json.dumps(context, ensure_ascii=False, allow_nan=False)}
