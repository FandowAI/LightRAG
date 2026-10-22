"""Versioned polling contract for a start button and a separate progress module."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


JobStatus = Literal[
    "preparing",
    "ready",
    "starting",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "interrupted",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Progress(Contract):
    unit: Literal["sources"]
    percent: float | None = Field(
        ge=0,
        le=100,
        description="Committed sources / accepted sources; null when not observed. Not QA acceptance rate.",
    )
    seen_sources: int = Field(ge=0)
    accepted_sources: int = Field(ge=0)
    rejected_sources: int = Field(ge=0)
    processed_sources: int | None = Field(ge=0)
    committed_batch: int | None = Field(ge=0)


class Receipt(Contract):
    run_id: str
    directory: str
    status: Literal["completed", "completed_with_errors", "failed"]
    summary: dict[str, Any]


class Artifacts(Contract):
    directory: str
    input: str
    config: str
    rejected: str
    stdout: str
    stderr: str


class JobResponse(Contract):
    schema_version: Literal[1]
    job_id: str
    request_id: str
    status: JobStatus
    phase: str
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    terminal: bool
    can_start: bool
    poll_after_ms: int = Field(ge=0)
    progress: Progress
    result: Receipt | None
    artifacts: Artifacts
    links: dict[str, str]


class JobListResponse(Contract):
    items: list[JobResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class LogResponse(Contract):
    text: str
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    has_more: bool
