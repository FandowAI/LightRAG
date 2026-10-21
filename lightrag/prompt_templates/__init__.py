"""Localized prompt templates kept apart from upstream prompt modules."""

from __future__ import annotations

from pathlib import Path
from string import Formatter
from typing import Any


_ZH_DIR = Path(__file__).parent / "zh"


def load_zh_templates(project_defaults: dict[str, Any]) -> dict[str, Any]:
    """Load Chinese templates while checking the upstream format contract."""

    result: dict[str, Any] = {}
    for key, original in project_defaults.items():
        if key == "user_prompt_prefix":
            result[key] = ""
            continue
        path = _ZH_DIR / f"{key}.txt"
        translated = path.read_text(encoding="utf-8")
        if isinstance(original, list):
            if len(original) != 1:
                raise ValueError(f"Chinese prompt {key} needs one file per example")
            originals = original
            translations = [translated]
        else:
            originals = [original]
            translations = [translated]

        for source, target in zip(originals, translations, strict=True):
            if not source.endswith("\n"):
                target = target.removesuffix("\n")
            source_lines = source.splitlines()
            target_lines = target.splitlines()
            if len(source_lines) != len(target_lines) or [
                index for index, line in enumerate(source_lines) if not line.strip()
            ] != [index for index, line in enumerate(target_lines) if not line.strip()]:
                raise ValueError(f"Chinese prompt {key} changed the line or blank-line structure")
            source_fields = sorted(name for _, name, _, _ in Formatter().parse(source) if name)
            target_fields = sorted(name for _, name, _, _ in Formatter().parse(target) if name)
            if source_fields != target_fields:
                raise ValueError(f"Chinese prompt {key} changed format placeholders")
            if source.endswith("\n") != target.endswith("\n"):
                raise ValueError(f"Chinese prompt {key} changed the final newline")
            if isinstance(original, list):
                result[key] = [target]
            else:
                result[key] = target
    return result
