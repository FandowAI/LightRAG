"""Strict commands for managed ingestion, retrieval, query and distillation."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, Name, TaskSource


class RetrievalOptions(Contract):
    graph_depth: int = Field(default=2, ge=0, le=5)
    rag_top_k: int = Field(default=10, ge=1, le=100)
    near_dedup: bool = True
    near_threshold: float = Field(default=0.995, ge=0.8, le=1)


class QueryOptions(Contract):
    query: str = Field(min_length=1)
    mode: Literal["local", "global", "hybrid", "mix", "naive"] = "mix"
    top_k: int = Field(default=40, gt=0)
    chunk_top_k: int = Field(default=20, gt=0)
    max_entity_tokens: int = Field(default=6000, gt=0)
    max_relation_tokens: int = Field(default=8000, gt=0)
    max_total_tokens: int = Field(default=30000, gt=0)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value):
        if not value.strip():
            raise ValueError("Query must not be blank")
        return value


class CenterOptions(Contract):
    min_neighbors: int = Field(default=3, ge=1, le=1000)
    min_documents: int = Field(default=2, ge=1, le=100)


class TaskCommand(Contract):
    expected_revision: Name
    request_id: Name
    kind: Literal["ingest", "retrieval", "distillation", "chain", "query", "centers"]
    source: TaskSource
    centers: list[str] = Field(default_factory=list)
    retrieval: RetrievalOptions | None = None
    query: QueryOptions | None = None
    selection: CenterOptions | None = None

    @field_validator("centers")
    @classmethod
    def center_names(cls, values):
        if any(not name.strip() or len(name) > 512 for name in values):
            raise ValueError("Center names must be nonempty and at most 512 characters")
        return list(dict.fromkeys(name.strip() for name in values))

    @model_validator(mode="after")
    def source_kind(self):
        expected = {
            "ingest": {"directory"},
            "chain": {"directory"},
            "retrieval": {"index"},
            "query": {"index"},
            "centers": {"index"},
            "distillation": {"directory", "retrieval"},
        }
        if self.source.type not in expected[self.kind]:
            raise ValueError("Source type does not match the task kind")
        if self.kind in {"retrieval", "chain"}:
            if not self.centers:
                raise ValueError("Select centers explicitly for evidence retrieval")
        elif self.centers or self.retrieval is not None:
            raise ValueError("Only retrieval or chain tasks accept retrieval options")
        if (self.kind == "query") != (self.query is not None):
            raise ValueError("Only query tasks require query options")
        if self.kind != "centers" and self.selection is not None:
            raise ValueError("Only centers tasks accept selection options")
        return self
