"""Compile server-owned batch configuration into an isolated RAG startup snapshot."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field

from .contracts import Contract, Name
from .resources import digest


class ModelProfile(Contract):
    binding: str = "openai"
    model_name: str = Field(min_length=1)
    api_url: str = Field(min_length=1)
    credential_id: Name | None = None
    key_name_of_api_key: str | None = None
    timeout: int = Field(default=180, gt=0)
    max_async: int = Field(default=8, gt=0)


class EmbeddingProfile(ModelProfile):
    dimensions: int = Field(gt=0)
    batch_size: int = Field(default=16, gt=0)
    max_tokens: int = Field(default=8192, gt=0)


@dataclass(frozen=True)
class RuntimePolicy:
    """Deployment authority supplies this policy; configuration requests cannot."""

    project_directory: Path
    python: Path
    storage_credentials_file: Path | None = None
    storage_mode: str = "neo4j_milvus"
    max_processes: int = 2
    startup_timeout: float = 120
    stop_timeout: float = 30
    heartbeat_seconds: float = 1
    health_timeout: float = 10

    def __post_init__(self):
        if self.storage_mode not in {"neo4j_milvus", "local"}:
            raise ValueError("Unsupported managed storage mode")
        if (
            type(self.max_processes) is not int
            or self.max_processes < 1
            or any(
                not math.isfinite(value) or value <= 0
                for value in (
                    self.startup_timeout,
                    self.stop_timeout,
                    self.heartbeat_seconds,
                    self.health_timeout,
                )
            )
        ):
            raise ValueError("Runtime capacity and deadlines must be positive")
        if self.health_timeout <= 2 * self.heartbeat_seconds:
            raise ValueError("Health deadline must exceed two heartbeat intervals")
        if not self.project_directory.is_absolute() or not self.python.is_absolute():
            raise ValueError("Runtime project and interpreter must be absolute")


def fingerprint(project: Path) -> dict:
    """Pin actual Python sources, dependency lock and installed distributions."""
    hasher = hashlib.sha256()
    for path in sorted((project / "lightrag").rglob("*.py")):
        hasher.update(str(path.relative_to(project)).encode())
        hasher.update(path.read_bytes())
    lock = project / "uv.lock"
    packages = sorted(
        {
            (
                distribution.metadata["Name"].lower().replace("_", "-"),
                distribution.version,
            )
            for distribution in importlib.metadata.distributions()
            if distribution.metadata["Name"]
        }
    )
    return {
        "source_sha256": hasher.hexdigest(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "installed_sha256": digest(packages),
        "python": list(sys.version_info[:3]),
    }


def selected_fingerprint(policy: RuntimePolicy) -> dict:
    environment = clean_environment(policy.project_directory)
    result = subprocess.run(
        [
            str(policy.python),
            "-c",
            "import json,sys; from pathlib import Path; "
            "from lightrag.batch_control.runtime_config import fingerprint; "
            "print(json.dumps(fingerprint(Path(sys.argv[1]))))",
            str(policy.project_directory),
        ],
        cwd=policy.project_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def clean_environment(project: Path) -> dict[str, str]:
    """Do not inherit storage overrides, model secrets, Python hooks or startup flags."""
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TIKTOKEN_CACHE_DIR")
        if key in os.environ
    }
    environment.update(
        PYTHONPATH=str(project), PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1"
    )
    return environment


def _credential(control, actor, profile) -> str:
    if profile.credential_id is None:
        if profile.key_name_of_api_key is not None:
            raise ValueError("A key variable requires a credential reference")
        return ""
    control.allow(actor, "use_credential", profile.credential_id)
    if not profile.key_name_of_api_key:
        raise ValueError("Credential profiles must name their key variable")
    path = control.catalog.credentials[profile.credential_id]
    value = dotenv_values(path, interpolate=False).get(profile.key_name_of_api_key)
    if not value or not value.strip():
        raise ValueError("A referenced model credential is missing or empty")
    return value


def _profile(control, actor, reference, model_type):
    control.allow(actor, "use_profile", reference)
    profile = model_type.model_validate(control.catalog.profiles[reference])
    endpoint = urlsplit(profile.api_url)
    if (
        profile.binding != "openai"
        or endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError(
            "Managed RAG supports credential-free OpenAI-compatible base URLs"
        )
    return profile


def compile_startup(
    control, actor, batch, revision, document, manifest, policy, *, index_purpose="rag"
):
    """Refuse unsupported settings rather than launch a process with ignored values."""
    rag = document.rag
    from .bindings import BindingStore

    binding_record = BindingStore(control.registry).get(batch["id"])
    binding = binding_record["binding"] if binding_record else None
    from .index_rebuild.authority import effective_storage_binding, for_manifest

    if index_purpose not in {"rag", "index_build", "index_preflight"}:
        raise ValueError("Unsupported index runtime purpose")
    index_record = for_manifest(
        control.registry, batch, revision, manifest, purpose=index_purpose
    )
    if index_record:
        vector = index_record["binding"]
        if (
            vector["storage_mode"] != policy.storage_mode
            or vector["embedding_profile"] != rag.embedding_profile
            or vector["embedding_profile_sha256"]
            != digest(manifest["execution"]["rag_profiles"][rag.embedding_profile])
        ):
            raise ValueError(
                "Vector binding differs from its immutable embedding profile"
            )
        if binding_record and binding_record["state"] != "adopted":
            raise ValueError("Complete source adoption before rebuilding its vectors")
        binding = effective_storage_binding(binding, vector)
    if (
        binding_record
        and manifest["sources"].get("storage_binding_digest")
        != binding_record["digest"]
    ):
        raise ValueError("Revision does not own its adopted storage binding")
    if binding and binding["storage_mode"] != policy.storage_mode:
        raise ValueError("Adopted storage backend differs from the deployment policy")
    strategies = {
        "token": "F",
        "fixed_token": "F",
        "recursive_character": "R",
        "semantic_vector": "V",
        "paragraph_semantic": "P",
    }
    if rag.chunking_strategy not in strategies:
        raise ValueError("Unsupported managed chunking strategy")
    selector = strategies[rag.chunking_strategy]
    if not rag.model_profiles:
        raise ValueError("Managed runtime requires an explicit model profile")
    if control.catalog.profile_snapshot(rag) != manifest["execution"]["rag_profiles"]:
        raise ValueError("Model profile changed; register a new configuration revision")
    paths = control.paths
    runtime_root = paths.under(paths.runtime_root, "rag", batch["namespace"])
    if any(
        path != path.resolve()
        for path in (
            runtime_root,
            *(runtime_root / name for name in ("storage", "inputs", "logs")),
        )
    ):
        raise ValueError("Runtime directories must not traverse symlinks")
    environment = {
        "WORKSPACE": batch["workspace"],
        "LIGHTRAG_MANAGED_BATCH_ID": batch["id"],
        "LIGHTRAG_BATCH_REGISTRY": str(paths.registry),
        "WORKING_DIR": str(runtime_root / "storage"),
        "INPUT_DIR": str(runtime_root / "inputs"),
        "LOG_DIR": str(runtime_root / "logs"),
        "WORKERS": "1",
        "LIGHTRAG_KV_STORAGE": "JsonKVStorage",
        "LIGHTRAG_DOC_STATUS_STORAGE": "JsonDocStatusStorage",
        "LIGHTRAG_GRAPH_STORAGE": "Neo4JStorage"
        if policy.storage_mode == "neo4j_milvus"
        else "NetworkXStorage",
        "LIGHTRAG_VECTOR_STORAGE": "MilvusVectorDBStorage"
        if policy.storage_mode == "neo4j_milvus"
        else "NanoVectorDBStorage",
        "CHUNK_SIZE": str(rag.chunk_size),
        "LIGHTRAG_BATCH_CHUNKING_STRATEGY": "fixed_token"
        if rag.chunking_strategy == "token"
        else rag.chunking_strategy,
        "CHUNK_OVERLAP_SIZE": str(rag.chunk_overlap_size),
        "LIGHTRAG_PARSER": f"*:{rag.parse_engine}-te{selector},*:legacy-{selector}",
        "CHUNK_P_SIZE": str(rag.chunk_size),
        "CHUNK_P_OVERLAP_SIZE": str(rag.chunk_overlap_size),
        "CHUNK_R_SIZE": str(rag.chunk_size),
        "CHUNK_R_OVERLAP_SIZE": str(rag.chunk_overlap_size),
        "CHUNK_V_SIZE": str(rag.chunk_size),
        "CHUNK_V_OVERLAP_SIZE": str(rag.chunk_overlap_size),
        "LIGHTRAG_DATA_WORKFLOW_CONFIG_ROOT": str(paths.config_root / "business"),
        "MAX_PARALLEL_INSERT": str(rag.max_parallel_insert),
        "MAX_PARALLEL_PARSE_NATIVE": str(rag.max_parallel_parse_native),
        "RERANK_BINDING": "null",
        "ENTERPRISE_KNOWLEDGE_ENABLED": "false",
        "VLM_PROCESS_ENABLE": "true" if "vlm" in rag.role_profiles else "false",
        "PROMPT_DIR": str(policy.project_directory / "prompts"),
        "LIGHTRAG_BATCH_OUTPUT_DIR": str(paths.runtime_root / "retrieval"),
    }
    if binding:
        environment.update(binding["environment"])
        environment["WORKING_DIR"] = binding["working_directory"]
        environment["INPUT_DIR"] = binding["input_directory"]
        for name, resource in binding["resources"].items():
            from .adoption_resources import resource_sha256

            path = Path(resource["path"])
            if (
                not path.is_absolute()
                or path != path.resolve()
                or resource_sha256(path) != resource["sha256"]
                or path.is_dir() != (resource["kind"] == "directory")
            ):
                raise ValueError("Adopted runtime resource changed")
            if environment.get(name) != resource["path"]:
                raise ValueError("Adopted runtime resource binding changed")
        if binding["retrieval_directory"]:
            default_root = (
                Path(binding["working_directory"])
                / batch["workspace"]
                / "batch_retrieval"
            )
            if Path(binding["retrieval_directory"]) == default_root:
                environment.pop("LIGHTRAG_BATCH_OUTPUT_DIR", None)
            else:
                if Path(binding["retrieval_directory"]).name != batch["workspace"]:
                    raise ValueError(
                        "Adopted retrieval root does not contain its workspace"
                    )
                environment["LIGHTRAG_BATCH_OUTPUT_DIR"] = str(
                    Path(binding["retrieval_directory"]).parent
                )
    environment["LIGHTRAG_PARSER"] = (
        f"*:{rag.parse_engine}-te{selector},*:legacy-{selector}"
    )
    secrets = {}
    storage_keys = {
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NEO4J_DATABASE",
        "MILVUS_URI",
        "MILVUS_USER",
        "MILVUS_PASSWORD",
        "MILVUS_TOKEN",
        "MILVUS_DB_NAME",
    }
    if policy.storage_credentials_file is not None:
        values = dotenv_values(policy.storage_credentials_file, interpolate=False)
        if any(values.get(key) for key in ("NEO4J_WORKSPACE", "MILVUS_WORKSPACE")):
            raise ValueError("Storage workspace overrides would break batch isolation")
        for key in storage_keys:
            if values.get(key):
                if key.endswith("_URI"):
                    endpoint = urlsplit(values[key])
                    if (
                        endpoint.username
                        or endpoint.password
                        or endpoint.query
                        or endpoint.fragment
                    ):
                        raise ValueError(
                            "Database endpoints must not embed credentials"
                        )
                (
                    secrets
                    if any(part in key for part in ("PASSWORD", "TOKEN"))
                    else environment
                )[key] = values[key]
    if policy.storage_mode == "neo4j_milvus" and not all(
        environment.get(key) for key in ("NEO4J_URI", "MILVUS_URI")
    ):
        raise ValueError("Explicit Neo4j and Milvus endpoints are required")
    profiles = {
        name: _profile(control, actor, reference, ModelProfile)
        for name, reference in rag.model_profiles.items()
    }
    primary = (
        profiles["primary"]
        if "primary" in profiles
        else profiles[rag.role_profiles.get("query", sorted(profiles)[0])]
    )
    environment.update(
        LLM_BINDING=primary.binding,
        LLM_BINDING_HOST=primary.api_url,
        LLM_MODEL=primary.model_name,
        LLM_TIMEOUT=str(primary.timeout),
        MAX_ASYNC_LLM=str(primary.max_async),
    )
    secrets["LLM_BINDING_API_KEY"] = _credential(control, actor, primary)
    for role in ("query", "extract", "keyword", "vlm"):
        model = (
            profiles[rag.role_profiles[role]] if role in rag.role_profiles else primary
        )
        prefix = role.upper()
        environment.update(
            {
                f"{prefix}_LLM_BINDING": model.binding,
                f"{prefix}_LLM_BINDING_HOST": model.api_url,
                f"{prefix}_LLM_MODEL": model.model_name,
                f"{prefix}_LLM_TIMEOUT": str(model.timeout),
                f"{prefix}_MAX_ASYNC_LLM": str(
                    rag.role_concurrency.get(role, model.max_async)
                ),
            }
        )
        secrets[f"{prefix}_LLM_BINDING_API_KEY"] = _credential(control, actor, model)
    embedding = _profile(control, actor, rag.embedding_profile, EmbeddingProfile)
    if index_record and {
        "model": embedding.model_name,
        "dimensions": embedding.dimensions,
    } != {"model": vector["embedding_model"], "dimensions": vector["dimensions"]}:
        raise ValueError(
            "Vector binding embedding space differs from its selected profile"
        )
    if (
        binding
        and {"model": embedding.model_name, "dimensions": embedding.dimensions}
        != binding["embedding"]
    ):
        raise ValueError("Adopted embedding space cannot be changed without rebuilding")
    environment.update(
        EMBEDDING_BINDING=embedding.binding,
        EMBEDDING_BINDING_HOST=embedding.api_url,
        EMBEDDING_MODEL=embedding.model_name,
        EMBEDDING_DIM=str(embedding.dimensions),
        EMBEDDING_BATCH_NUM=str(embedding.batch_size),
        EMBEDDING_FUNC_MAX_ASYNC=str(embedding.max_async),
        EMBEDDING_TOKEN_LIMIT=str(embedding.max_tokens),
    )
    secrets["EMBEDDING_BINDING_API_KEY"] = _credential(control, actor, embedding)
    bridge = control.catalog.bridge(paths, batch, document)
    bridge["workflow_config"] = str(
        paths.revision(batch["id"], revision) / "data_workflow_job.yaml"
    )
    startup = {
        "schema_version": 1,
        "purpose": index_purpose,
        "data_batch_id": batch["id"],
        "workspace": batch["workspace"],
        "config_revision_id": revision,
        "rag_digest": manifest["components"]["rag"],
        "environment": environment,
        "bridge": bridge,
        "credential_references": sorted(
            {
                profile.credential_id
                for profile in [*profiles.values(), embedding]
                if profile.credential_id
            }
        ),
        "runtime_fingerprint": selected_fingerprint(policy),
        "project_directory": str(policy.project_directory),
        "python": str(policy.python),
        "storage_mode": policy.storage_mode,
        "heartbeat_seconds": policy.heartbeat_seconds,
    }
    from .provider_quota.wiring import runtime_spec

    startup["provider_admission"] = runtime_spec(
        control,
        batch["id"],
        [profile.api_url for profile in [*profiles.values(), embedding]],
    )
    if binding_record:
        startup["storage_binding"] = binding_record["binding"]
        startup["storage_binding_digest"] = binding_record["digest"]
    if index_record:
        startup["index_binding"] = vector
        startup["index_binding_digest"] = index_record["digest"]
        if policy.storage_mode == "neo4j_milvus":
            environment["MILVUS_WORKSPACE"] = vector["vector_workspace"]
    elif index_purpose == "index_build":
        raise ValueError("An index build requires its declared vector binding")
    return startup, {
        **clean_environment(policy.project_directory),
        **environment,
        **secrets,
    }
