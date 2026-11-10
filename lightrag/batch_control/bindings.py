"""Immutable, digest-checked bindings for adopted storage; callers cannot select paths."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Name
from .resources import digest
from .storage import Conflict, IntegrityError
from .adoption_options import PROVIDER_ENVIRONMENT, RAG_ENVIRONMENT

ALLOWED_ENVIRONMENT = (
    {
        "ENTERPRISE_KNOWLEDGE_ENABLED",
        "ENTITY_TYPE_PROMPT_FILE",
        "SUMMARY_LANGUAGE",
        "ENTITY_IDENTITY_REGISTRY_FILE",
        "PROMPT_DIR",
        "LIGHTRAG_PARSER",
        "USER_PROMPT_PREFIX_FILE",
        "USER_PROMPT_PREFIX",
        "ENTITY_EXTRACTION_USE_JSON",
        "FORCE_LLM_SUMMARY_ON_MERGE",
        "SUMMARY_MAX_TOKENS",
        "SUMMARY_CONTEXT_SIZE",
        "SUMMARY_LENGTH_RECOMMENDED",
        "ENABLE_LLM_CACHE",
        "ENABLE_LLM_CACHE_FOR_EXTRACT",
        "EMBEDDING_CHUNK_OVERLAP_TOKEN_SIZE",
        "VLM_PROCESS_ENABLE",
        "EMBEDDING_ENABLE_ASYMMETRIC",
        "EMBEDDING_DOCUMENT_PREFIX",
        "EMBEDDING_QUERY_PREFIX",
        "EMBEDDING_SEND_DIM",
        "OPENAI_LLM_MAX_TOKENS",
        "OPENAI_LLM_TEMPERATURE",
        "OPENAI_LLM_TOP_P",
        "OPENAI_LLM_PRESENCE_PENALTY",
        "OPENAI_LLM_FREQUENCY_PENALTY",
        "OPENAI_LLM_SEED",
    }
    | PROVIDER_ENVIRONMENT
    | set(RAG_ENVIRONMENT)
)


class EmbeddingIdentity(Contract):
    model: str
    dimensions: int = Field(gt=0)


class GraphBaseline(Contract):
    nodes: int = Field(ge=0)
    edges: int = Field(ge=0)
    nodes_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    edges_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VectorBaseline(Contract):
    rows: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FrozenResource(Contract):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["file", "directory"] = "file"


def rows_digest(rows):
    values = sorted(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str) for row in rows
    )
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


async def graph_baseline(graph):
    nodes = await graph.get_all_nodes()
    edges = await graph.get_all_edges()
    return GraphBaseline(
        nodes=len(nodes),
        edges=len(edges),
        nodes_sha256=rows_digest(nodes),
        edges_sha256=rows_digest(edges),
    ).model_dump()


def verify_storage_binding(identity, startup):
    """Check adopted identities before initializing any storage backend."""
    value = startup.get("storage_binding")
    if value is None:
        return
    binding = StorageBinding.model_validate(value).model_dump()
    if digest(binding) != startup.get("storage_binding_digest"):
        raise IntegrityError("Adopted startup binding changed")
    from .index_rebuild.authority import effective_storage_binding

    if startup.get("index_binding") is not None:
        if digest(startup["index_binding"]) != startup.get("index_binding_digest"):
            raise IntegrityError("Startup vector binding changed")
        binding = effective_storage_binding(binding, startup["index_binding"])
    for key in (
        "workspace",
        "working_directory",
        "input_directory",
        "collections",
        "embedding",
    ):
        if identity.get(key) != binding[key]:
            raise IntegrityError(
                "Adopted storage identity differs before initialization: " + key
            )
    for key in (
        "auxiliary_collections",
        "storage_endpoints",
        "role_provider_options",
    ):
        if binding[key] and identity.get(key) != binding[key]:
            raise IntegrityError(
                "Adopted storage identity differs before initialization: " + key
            )
    expected_classes = {
        key: startup["environment"]["LIGHTRAG_" + key.upper()]
        for key in (
            "kv_storage",
            "doc_status_storage",
            "graph_storage",
            "vector_storage",
        )
    }
    if identity.get("storage_classes") != expected_classes:
        raise IntegrityError("Adopted storage classes differ before initialization")


class StorageBinding(Contract):
    schema_version: Literal[1] = 1
    workspace: Name
    working_directory: str
    input_directory: str
    storage_mode: Literal["local", "neo4j_milvus"]
    collections: dict[str, str]
    embedding: EmbeddingIdentity
    environment: dict[str, str] = Field(default_factory=dict)
    graph: GraphBaseline
    vectors: dict[str, VectorBaseline] = Field(default_factory=dict)
    document_states: dict[str, int] = Field(default_factory=dict)
    auxiliary_collections: dict[str, str] = Field(default_factory=dict)
    storage_endpoints: dict[str, str] = Field(default_factory=dict)
    role_provider_options: dict[str, dict] = Field(default_factory=dict)
    runtime_resource_digests: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, FrozenResource] = Field(default_factory=dict)
    retrieval_directory: str | None = None
    source_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def scoped(self):
        for value in (self.working_directory, self.input_directory):
            path = Path(value)
            if not path.is_absolute() or path != path.resolve():
                raise ValueError(
                    "Adopted storage paths must be canonical absolute paths"
                )
        if set(self.collections) != {"graph", "entities", "relationships", "chunks"}:
            raise ValueError("All adopted graph/vector bindings must be declared")
        if set(self.environment) - ALLOWED_ENVIRONMENT:
            raise ValueError("Unsupported adopted runtime environment override")
        if self.vectors and set(self.vectors) != {
            "entities",
            "relationships",
            "chunks",
        } | set(self.auxiliary_collections):
            raise ValueError("Every adopted vector collection must have a baseline")
        if self.retrieval_directory and (
            not Path(self.retrieval_directory).is_absolute()
            or Path(self.retrieval_directory)
            != Path(self.retrieval_directory).resolve()
        ):
            raise ValueError("Adopted retrieval directory must be canonical")
        return self


class BindingStore:
    def __init__(self, registry):
        self.registry = registry

    @contextmanager
    def transaction(self):
        with self.registry.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS storage_bindings (batch TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS adopted_profiles (id TEXT PRIMARY KEY, digest TEXT NOT NULL, record TEXT NOT NULL)"
            )
            yield db

    @staticmethod
    def exists(db, table):
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    def get(self, batch):
        with self.registry.transaction() as db:
            if not self.exists(db, "storage_bindings"):
                return None
            row = db.execute(
                "SELECT digest,state,record FROM storage_bindings WHERE batch=?",
                (batch,),
            ).fetchone()
            if row is None:
                return None
            value = json.loads(row["record"])
            if digest(value) != row["digest"]:
                raise IntegrityError("Adopted storage binding changed")
            binding = StorageBinding.model_validate(value)
            if row["state"] not in {"registered", "adopted"}:
                raise IntegrityError("Unknown adopted storage state")
            owner = self.registry.row(db, batch)
            if binding.workspace != owner["workspace"]:
                raise IntegrityError("Adopted storage workspace changed")
            return {"binding": value, "digest": row["digest"], "state": row["state"]}

    def profiles(self):
        with self.registry.transaction() as db:
            if not self.exists(db, "adopted_profiles"):
                return {}
            result = {}
            for row in db.execute("SELECT id,digest,record FROM adopted_profiles"):
                value = json.loads(row["record"])
                if digest(value) != row["digest"]:
                    raise IntegrityError("Adopted model profile changed")
                result[row["id"]] = value
            return result

    def register(self, db, batch, binding, profiles):
        from .references import no_plaintext_secrets

        value = StorageBinding.model_validate(binding).model_dump()
        if value["workspace"] != self.registry.row(db, batch)["workspace"]:
            raise IntegrityError(
                "Adopted storage workspace does not belong to its batch"
            )
        no_plaintext_secrets(profiles)
        db.execute(
            "INSERT INTO storage_bindings VALUES (?,?,?,?)",
            (batch, digest(value), "registered", json.dumps(value)),
        )
        for name, profile in profiles.items():
            existing = db.execute(
                "SELECT digest FROM adopted_profiles WHERE id=?", (name,)
            ).fetchone()
            if existing and existing["digest"] != digest(profile):
                raise Conflict("Adopted model profile identity is already in use")
            db.execute(
                "INSERT OR IGNORE INTO adopted_profiles VALUES (?,?,?)",
                (name, digest(profile), json.dumps(profile)),
            )

    def complete(self, batch, lease, proof):
        with self.transaction() as db:
            binding_row = db.execute(
                "SELECT digest,state,record FROM storage_bindings WHERE batch=?",
                (batch,),
            ).fetchone()
            if not binding_row:
                raise Conflict("No storage binding is registered")
            value = json.loads(binding_row["record"])
            if digest(value) != binding_row["digest"]:
                raise IntegrityError("Adopted storage binding changed")
            if proof.get("index_binding_id") is not None:
                from .index_rebuild.authority import (
                    read_binding,
                    effective_storage_binding,
                )

                index = read_binding(db, batch, proof["index_binding_id"])
                if (
                    binding_row["state"] != "adopted"
                    or index["state"] not in {"verified", "active", "retired"}
                    or index["digest"] != proof.get("index_binding_digest")
                ):
                    raise IntegrityError(
                        "Adopted source has no matching verified vector replacement"
                    )
                value = effective_storage_binding(value, index["binding"])
            runtime_row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(runtime_row[0]) if runtime_row else {}
            if (
                runtime.get("state") != "ready"
                or runtime.get("lease_id") != lease.lease_id
                or runtime.get("generation") != lease.generation
                or proof.get("generation") != lease.generation
                or proof.get("startup_digest") != lease.startup_digest
                or proof.get("pid") != lease.pid
                or proof.get("process_identity") != lease.process_identity
                or proof.get("storage_binding_digest") != binding_row["digest"]
                or proof.get("collections") != value["collections"]
            ):
                raise Conflict("Adoption has no matching ready generation")
            if (
                binding_row["state"] == "registered"
                and proof.get("adoption_graph") != value["graph"]
            ):
                raise IntegrityError("Adopted graph does not match its frozen baseline")
            if (
                binding_row["state"] == "registered"
                and value["runtime_resource_digests"]
                and proof.get("runtime_resource_digests")
                != value["runtime_resource_digests"]
            ):
                raise IntegrityError(
                    "Initial adopted runtime resources were not verified"
                )
            if (
                binding_row["state"] == "registered"
                and value["vectors"]
                and proof.get("adoption_vectors") != value["vectors"]
            ):
                raise IntegrityError(
                    "Adopted vectors do not match their frozen baseline"
                )
            if (
                binding_row["state"] == "registered"
                and value["document_states"]
                and proof.get("adoption_document_states") != value["document_states"]
            ):
                raise IntegrityError("Adopted document states changed")
            db.execute(
                "UPDATE storage_bindings SET state='adopted' WHERE batch=? AND state='registered'",
                (batch,),
            )
