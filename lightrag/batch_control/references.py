"""Server-owned execution references and credential-free public snapshots."""

from __future__ import annotations
from copy import deepcopy
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, parse_qsl

from .contracts import Configuration, Environment
from .paths import ControlPaths


def no_plaintext_secrets(value):
    if isinstance(value, dict):
        if any(
            str(key).lower()
            in {
                "api_key",
                "password",
                "secret",
                "token",
                "authorization",
                "access_token",
            }
            for key in value
        ):
            raise ValueError(
                "Configuration must use credential references, not secret values"
            )
        for key, item in value.items():
            if (
                key in {"api_url", "base_url", "url", "host"}
                and isinstance(item, str)
                and "://" in item
            ):
                parsed = urlsplit(item)
                if (
                    parsed.username
                    or parsed.password
                    or any(
                        name.lower() in {"api_key", "key", "token", "access_token"}
                        for name, _ in parse_qsl(parsed.query)
                    )
                ):
                    raise ValueError("Endpoint must not embed credentials")
            no_plaintext_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_plaintext_secrets(item)


class Catalog:
    """Administrator-provided immutable reference snapshots; never accept paths from HTTP."""

    def __init__(
        self,
        *,
        environments: dict[str, Environment],
        credentials: dict[str, Path],
        inputs: dict[str, list[Path]],
        profiles: dict[str, dict],
    ):
        self.environments = deepcopy(environments)
        self.credentials = deepcopy(credentials)
        self.inputs = deepcopy(inputs)
        self.profiles = deepcopy(profiles)
        no_plaintext_secrets(self.profiles)
        for environment in self.environments.values():
            if not environment.code_revision or not environment.dependency_revision:
                raise ValueError(
                    "Execution environment must identify code and dependencies"
                )

    def environment_snapshot(self, reference: str) -> dict:
        environment = self.environments[reference]
        return {
            "id": reference,
            "project_directory": environment.project_directory,
            "python": environment.python,
            "code_revision": environment.code_revision,
            "dependency_revision": environment.dependency_revision,
        }

    def profile_snapshot(self, rag) -> dict:
        refs = set(rag.model_profiles.values()) | {rag.embedding_profile}
        return {
            reference: deepcopy(self.profiles[reference]) for reference in sorted(refs)
        }

    def snapshot(self, document: Configuration) -> dict:
        return {
            "environment": self.environment_snapshot(document.bridge.environment_id),
            "rag_profiles": self.profile_snapshot(document.rag),
        }

    def execution(self, document: Configuration, allow: Callable) -> dict:
        bridge = document.bridge
        allow("use_environment", bridge.environment_id)
        environment = self.environments[bridge.environment_id]
        if not Path(environment.project_directory).is_dir() or not os.access(
            environment.python, os.X_OK
        ):
            raise ValueError("Selected project or interpreter is unavailable")
        if bridge.credential_id is not None:
            allow("use_credential", bridge.credential_id)
            if not self.credentials[bridge.credential_id].is_file():
                raise ValueError("Credential reference is unavailable")
        for reference in bridge.input_ids:
            allow("use_input", reference)
            for path in self.inputs[reference]:
                if not path.is_absolute() or not path.exists():
                    raise ValueError("Input reference is unavailable")
        refs = set(document.rag.model_profiles.values()) | {
            document.rag.embedding_profile
        }
        profiles = {}
        for reference in sorted(refs):
            allow("use_profile", reference)
            profiles[reference] = deepcopy(self.profiles[reference])
        return {
            "environment": {
                "id": bridge.environment_id,
                "project_directory": environment.project_directory,
                "python": environment.python,
                "code_revision": environment.code_revision,
                "dependency_revision": environment.dependency_revision,
            },
            "rag_profiles": profiles,
        }

    def bridge(self, paths: ControlPaths, batch: dict, document: Configuration) -> dict:
        config = document.bridge
        environment = self.environments[config.environment_id]
        return {
            "version": 1,
            "project_directory": environment.project_directory,
            "python": environment.python,
            "credentials_file": str(self.credentials[config.credential_id])
            if config.credential_id
            else None,
            "jobs_directory": str(
                paths.under(paths.runtime_root, "data-workflow", batch["namespace"])
            ),
            "input": {
                "paths": [
                    str(path)
                    for reference in config.input_ids
                    for path in self.inputs[reference]
                ],
                "records_path": config.records_path,
            },
            "limits": {
                name: getattr(config, name)
                for name in (
                    "max_input_bytes",
                    "max_records",
                    "max_active_jobs",
                    "timeout_seconds",
                )
            },
        }
