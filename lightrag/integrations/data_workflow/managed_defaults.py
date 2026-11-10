"""Copy a managed batch's frozen recipe into its isolated business settings area.

Keep original revision resources immutable. Mutable recipe overrides and uploaded
Prompts survive RAG restarts through their workspace-specific configuration root.
"""

import hashlib
from pathlib import Path

import yaml

from .config import read_yaml
from .prompt_files import PromptFiles, publish_text


def prepare_defaults(settings, config_root: Path, namespace: str):
    recipe = read_yaml(settings.workflow_config)
    parameters = recipe["workflow"]["parameters"]
    library = PromptFiles(settings, namespace, config_root=config_root)
    for stage in ("scenario", "qa", "classification", "inspection"):
        task = parameters.get(stage)
        if isinstance(task, dict) and task.get("prompt_file"):
            source = settings.workflow_config.parent / Path(task["prompt_file"])
            published = library.save(stage, stage + ".md", source.read_text())
            task["prompt_file"] = published["path"]
    content = yaml.safe_dump(recipe, allow_unicode=True, sort_keys=False)
    digest = hashlib.sha256(content.encode()).hexdigest()
    target = publish_text(
        config_root / "recipes" / namespace, digest + ".yaml", content
    )
    return settings.model_copy(update={"workflow_config": target})
