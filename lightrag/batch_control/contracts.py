"""Typed identities, configuration groups and future admission ports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Name = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
Group = Literal["rag", "recipe", "bridge"]


def identifier(kind: str) -> str:
    if kind not in {"batch", "rev", "op", "task", "adoption", "backup"}:
        raise ValueError("Unsupported identity kind")
    return f"{kind}-{uuid.uuid4().hex}"


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


class BatchRecord(Contract):
    id: Name
    workspace: Name
    namespace: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
    name: str
    active: Name
    desired: Name | None = None
    pending: Name | None = None
    index_state: Literal["unprovisioned", "empty", "populated", "unknown"]
    lifecycle: Literal["registered", "provisioning", "ready", "failed"] = "registered"


class ApplyOperation(Contract):
    id: Name
    data_batch_id: Name
    config_revision_id: Name
    status: Literal["applied", "pending_restart", "failed"]
    requires_restart: bool


class RagConfig(Contract):
    # Adapter-neutral startup values; server environment compilation belongs to phase 2.
    parse_engine: Literal["native", "mineru", "docling", "legacy"] = "native"
    chunk_size: int = Field(default=2000, gt=0)
    chunk_overlap_size: int = Field(default=100, ge=0)
    chunking_strategy: Name = "paragraph_semantic"
    max_parallel_insert: int = Field(default=3, gt=0)
    max_parallel_parse_native: int = Field(default=5, gt=0)
    model_profiles: dict[Name, Name] = Field(default_factory=dict)
    role_profiles: dict[Literal["extract", "keyword", "query", "vlm"], Name] = Field(
        default_factory=dict
    )
    role_concurrency: dict[
        Literal["extract", "keyword", "query", "vlm"], Annotated[int, Field(gt=0)]
    ] = Field(default_factory=dict)
    embedding_profile: Name

    @model_validator(mode="after")
    def coherent(self):
        if self.chunk_overlap_size >= self.chunk_size:
            raise ValueError("Chunk overlap must be smaller than chunk size")
        if set(self.role_profiles.values()) - set(self.model_profiles):
            raise ValueError("RAG role references an unknown model profile")
        return self


class BridgeConfig(Contract):
    environment_id: Name
    credential_id: Name | None = None
    input_ids: list[Name] = Field(default_factory=list)
    records_path: list[str | int] = Field(default_factory=list)
    max_input_bytes: int = Field(default=67108864, gt=0)
    max_records: int = Field(default=10000, gt=0)
    max_active_jobs: int = Field(default=1, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)


class Configuration(Contract):
    schema_version: Literal[1] = 1
    rag: RagConfig
    retrieval: dict[str, JsonValue]
    recipe: dict[str, JsonValue]
    bridge: BridgeConfig


@dataclass(frozen=True)
class Environment:
    project_directory: str
    python: str
    code_revision: str
    dependency_revision: str


class TaskSource(Contract):
    data_batch_id: Name
    type: Literal["retrieval", "directory", "index"]
    retrieval_job_id: Name | None = None
    input_ids: list[Name] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.type == "retrieval" and (
            self.retrieval_job_id is None or self.input_ids
        ):
            raise ValueError(
                "Retrieval source requires one retrieval job and no input references"
            )
        if self.type == "directory" and (
            self.retrieval_job_id is not None or not self.input_ids
        ):
            raise ValueError(
                "Directory source requires nonempty input references and no retrieval job"
            )
        if self.type == "index" and (
            self.retrieval_job_id is not None or self.input_ids
        ):
            raise ValueError(
                "Index source does not accept input or retrieval references"
            )
        return self


class AdmissionRequest(Contract):
    task_id: Name
    data_batch_id: Name
    config_revision_id: Name
    kind: Literal["rag_process", "ingest", "retrieval", "distillation", "model_request"]
    provider_group: Name | None = None
    units: int = Field(default=1, gt=0)
    estimated_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def model_budget(self):
        if self.kind == "model_request" and self.provider_group is None:
            raise ValueError(
                "Model admission requires a shared provider/credential group"
            )
        if self.kind != "model_request" and self.estimated_tokens is not None:
            raise ValueError("Token estimates only apply to model request admission")
        return self


class AdmissionDecision(Contract):
    state: Literal["queued", "granted", "rejected"]
    lease_id: Name | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def lease(self):
        if (self.state == "granted") != (self.lease_id is not None):
            raise ValueError("Only granted admission has a lease")
        return self


class AdmissionPort(Protocol):
    """Phase 1 defines the port; no in-process semaphore pretends to be global quota."""

    def request(self, request: AdmissionRequest) -> AdmissionDecision: ...
    def release(self, lease_id: str) -> None: ...
    def status(self, task_id: str) -> AdmissionDecision: ...


class RuntimeLease(Contract):
    """Phase-2 supervisor must verify identity before acquiring or recovering this lease."""

    lease_id: Name
    data_batch_id: Name
    generation: int = Field(gt=0)
    pid: int = Field(gt=0)
    process_identity: str = Field(min_length=1)
    startup_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    workspace: Name
    collections: dict[Name, Name]


class RuntimePort(Protocol):
    def inspect(self, data_batch_id: str) -> RuntimeLease | None: ...
