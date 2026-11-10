"""Inspect and freeze an existing RAG before a journaled ownership transfer."""

import asyncio
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import time
import uuid

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .contracts import Name
from .bindings import graph_baseline
from .runtime_process import process_identity
from .resources import digest

PUBLIC = (
    "/webui",
    "/workspace",
    "/static",
    "/ui/",
    "/data-batches",
    "/batch-settings",
    "/batch-adoption",
)


class FreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Name


class LegacyFence:
    def __init__(self, workers=1):
        self.owner = None
        self.active = 0
        self.boot_id = uuid.uuid4().hex
        self.workers = workers
        self.drain_timeout = 15
        self.snapshot_active = False
        self.snapshot_lock = asyncio.Lock()

    def middleware(self, downstream):
        async def application(scope, receive, send):
            path = scope.get("path", "")
            tracked = (
                scope["type"] == "http"
                and path
                not in {
                    "/",
                    "/health",
                    "/auth-status",
                    "/login",
                    "/auth/verify",
                    "/docs",
                    "/openapi.json",
                    "/redoc",
                }
                and not any(
                    path == prefix.rstrip("/")
                    or path.startswith(prefix.rstrip("/") + "/")
                    for prefix in PUBLIC
                )
            )
            if not tracked:
                return await downstream(scope, receive, send)
            if self.owner:
                body = b'{"detail":"Workspace ownership transfer is in progress"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 409,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                return await send({"type": "http.response.body", "body": body})
            self.active += 1
            try:
                await downstream(scope, receive, send)
            finally:
                self.active -= 1

        return application


def storage_identity(rag):
    graph = rag.chunk_entity_relation_graph
    collections = {
        "graph": graph._get_raw_workspace_label()
        if hasattr(graph, "_get_raw_workspace_label")
        else rag.workspace + "_" + graph.namespace
    }
    for key, attribute in (
        ("entities", "entities_vdb"),
        ("relationships", "relationships_vdb"),
        ("chunks", "chunks_vdb"),
    ):
        storage = getattr(rag, attribute)
        collections[key] = getattr(
            storage, "final_namespace", rag.workspace + "_" + storage.namespace
        )
    from lightrag.prompt_admin_cache import prompt_admin_cache_digest

    enterprise = getattr(getattr(rag, "entity_merge", None), "enterprise", None)
    resource_digests = {
        "prompt_admin": prompt_admin_cache_digest(),
        "extraction_profile": digest(
            getattr(rag, "_entity_extraction_prompt_profile", {})
        ),
        "user_prefix": digest(getattr(rag, "user_prompt_prefix", "")),
        "identity_registry": enterprise.registry.digest
        if enterprise is not None
        else "none",
    }
    return {
        "workspace": rag.workspace,
        "working_directory": str(Path(rag.working_dir).resolve()),
        "input_directory": str(Path(rag.input_dir).resolve()),
        "collections": collections,
        "embedding": {
            "model": rag.embedding_func.model_name,
            "dimensions": rag.embedding_func.embedding_dim,
        },
        "storage_classes": {
            "kv_storage": type(rag.full_docs).__name__,
            "doc_status_storage": type(rag.doc_status).__name__,
            "graph_storage": type(graph).__name__,
            "vector_storage": type(rag.entities_vdb).__name__,
        },
        "auxiliary_collections": {
            "name_entities": rag.entity_merge.name_vdb.final_namespace
        }
        if getattr(rag, "entity_merge", None) is not None
        else {},
        "storage_endpoints": storage_endpoints(rag),
        "runtime_resource_digests": resource_digests,
        "role_provider_options": {
            name: config.get("metadata", {}).get("provider_options", {})
            for name, config in rag.get_llm_role_config().items()
        },
    }


def storage_endpoints(rag):
    from urllib.parse import urlsplit

    if type(rag.chunk_entity_relation_graph).__name__ != "Neo4JStorage":
        return {}
    result = {
        "neo4j_uri": os.getenv("NEO4J_URI", ""),
        "neo4j_database": os.getenv("NEO4J_DATABASE", ""),
        "milvus_uri": os.getenv("MILVUS_URI", ""),
        "milvus_database": os.getenv("MILVUS_DB_NAME", "default"),
    }
    for key in ("neo4j_uri", "milvus_uri"):
        url = urlsplit(result[key])
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Storage URLs must not embed credentials for adoption")
    return result


async def legacy_busy(app, fence):
    from lightrag.kg.shared_storage import get_namespace_data

    rag = app.state.rag
    pipeline = await get_namespace_data("pipeline_status", workspace=rag.workspace)
    busy = (
        fence.active > 0
        or fence.snapshot_active
        or any(pipeline.get(key) for key in ("busy", "scanning", "pending_enqueues"))
        or any(
            not task.done() for task in getattr(app.state, "background_tasks", set())
        )
    )
    configured = os.getenv("LIGHTRAG_BATCH_OUTPUT_DIR", "")
    root = (
        Path(configured) / rag.workspace
        if configured
        else Path(rag.working_dir) / rag.workspace / "batch_retrieval"
    )
    database = root / "jobs.sqlite3"
    if database.is_file():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            busy = (
                busy
                or db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
                is not None
            )
    return bool(busy)


async def report(app, fence, *, graph_fingerprint=False):
    rag = app.state.rag
    result = {
        "schema_version": 1,
        "protocol_version": 2,
        **storage_identity(rag),
        "pid": os.getpid(),
        "process_identity": process_identity(os.getpid()),
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "host_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "command_digest": digest(__import__("psutil").Process().cmdline()),
        "boot_id": fence.boot_id,
        "workers": fence.workers,
        "freeze_owner": fence.owner,
        "busy": await legacy_busy(app, fence),
        "observed_at": time.time(),
        "roles": rag.get_llm_role_config(),
        "parameters": {
            "chunk_size": rag.chunk_token_size,
            "chunk_overlap_size": rag.chunk_overlap_token_size,
            "max_parallel_insert": rag.max_parallel_insert,
            "max_parallel_parse_native": rag.max_parallel_parse_native,
            "max_async_llm": rag.llm_model_max_async,
        },
        "chunking_function": getattr(
            getattr(rag, "chunking_func", None), "__name__", "unknown"
        ),
    }
    # Configuration values only: credentials remain server-owned references.
    allowed = {
        "ENTERPRISE_KNOWLEDGE_ENABLED",
        "VLM_PROCESS_ENABLE",
        "ENTITY_TYPE_PROMPT_FILE",
        "LIGHTRAG_PARSER",
        "SUMMARY_LANGUAGE",
        "MAX_ASYNC_LLM",
        "LLM_TIMEOUT",
        "EMBEDDING_FUNC_MAX_ASYNC",
        "EMBEDDING_BATCH_NUM",
        "EMBEDDING_TOKEN_LIMIT",
        "EMBEDDING_TIMEOUT",
    }
    result["environment"] = {
        key: os.environ[key] for key in allowed if key in os.environ
    }
    from lightrag.api.config import get_config
    from .bindings import ALLOWED_ENVIRONMENT

    args = get_config()
    result["environment"].update(
        {key: os.environ[key] for key in ALLOWED_ENVIRONMENT if key in os.environ}
    )
    from .adoption_options import (
        provider_environment,
        PROVIDER_ENVIRONMENT,
        RAG_ENVIRONMENT,
    )

    result["environment"] = {
        key: value
        for key, value in result["environment"].items()
        if key not in PROVIDER_ENVIRONMENT
    }
    result["environment"].update(provider_environment(args, result["roles"]))
    for name, attribute in RAG_ENVIRONMENT.items():
        if hasattr(rag, attribute):
            value = getattr(rag, attribute)
            if value is not None:
                result["environment"][name] = (
                    str(value).lower() if isinstance(value, bool) else str(value)
                )
    result["environment"].setdefault(
        "VLM_PROCESS_ENABLE",
        "true" if getattr(args, "vlm_process_enable", False) else "false",
    )

    def credential(value):
        return hashlib.sha256(value.encode()).hexdigest() if value else None

    from .adoption_resources import resource_sha256
    from lightrag.prompt import get_entity_type_prompt_dir

    prompt_directory = get_entity_type_prompt_dir().parent
    result["environment"]["PROMPT_DIR"] = str(prompt_directory)
    if result["environment"].get("ENTITY_IDENTITY_REGISTRY_FILE"):
        result["environment"]["ENTITY_IDENTITY_REGISTRY_FILE"] = str(
            Path(result["environment"]["ENTITY_IDENTITY_REGISTRY_FILE"]).resolve()
        )

    result["resource_sha256"] = {
        name: resource_sha256(result["environment"][name])
        for name in ("PROMPT_DIR", "ENTITY_IDENTITY_REGISTRY_FILE")
        if result["environment"].get(name)
        and (name != "PROMPT_DIR" or prompt_directory.exists())
    }
    result["base_model"] = {
        "model": getattr(rag, "llm_model_name", args.llm_model),
        "binding": args.llm_binding,
        "host": args.llm_binding_host,
        "max_async": rag.llm_model_max_async,
        "timeout": args.timeout,
        "credential_sha256": credential(
            args.llm_binding_api_key or os.getenv("OPENAI_API_KEY")
        ),
    }
    for role, value in result["roles"].items():
        private = getattr(rag, "_role_llm_states", {}).get(role)
        if private is not None and private.metadata.get(
            "provider_options", {}
        ) != value.get("metadata", {}).get("provider_options", {}):
            raise ValueError(
                "Source provider arguments contain credentials that require explicit migration"
            )
        key = private.metadata.get("api_key") if private else None
        value["credential_sha256"] = credential(
            key
            or getattr(args, role + "_llm_binding_api_key", None)
            or args.llm_binding_api_key
            or os.getenv("OPENAI_API_KEY")
        )
    result["embedding_profile"] = {
        "model": rag.embedding_func.model_name,
        "binding": args.embedding_binding,
        "host": args.embedding_binding_host,
        "max_async": args.embedding_func_max_async,
        "batch_size": args.embedding_batch_num,
        "max_tokens": args.embedding_token_limit,
        "timeout": int(os.getenv("EMBEDDING_TIMEOUT", "180")),
        "credential_sha256": credential(
            args.embedding_binding_api_key or os.getenv("OPENAI_API_KEY")
        ),
    }
    claim = getattr(app.state, "workspace_claim", None)
    result["writer_claim"] = {
        "held": claim is not None and claim.descriptor is not None,
        "owner": claim.owner if claim is not None else None,
    }
    configured = os.getenv("LIGHTRAG_BATCH_OUTPUT_DIR", "").strip()
    result["retrieval_directory"] = str(
        (
            Path(configured) / rag.workspace
            if configured
            else Path(rag.working_dir) / rag.workspace / "batch_retrieval"
        ).resolve()
    )
    result["rerank_enabled"] = getattr(args, "rerank_binding", None) not in {
        None,
        "null",
        "none",
    }
    counts = await rag.doc_status.get_status_counts()
    result["document_states"] = {
        str(getattr(key, "value", key)): value for key, value in counts.items()
    }
    if graph_fingerprint:
        if not fence.owner or result["busy"]:
            raise HTTPException(
                409,
                "Freeze and drain the workspace before taking its graph fingerprint",
            )
        result["graph"] = await graph_baseline(rag.chunk_entity_relation_graph)
    return result


def mount_legacy_adoption(app, api_key, *, workers=1):
    fence = LegacyFence(workers)
    app.state.legacy_fence = fence
    app.add_middleware(_FenceMiddleware, fence=fence)

    async def authorized(request: Request):
        supplied = request.headers.get("X-API-Key", "")
        if not api_key or not supplied or not hmac.compare_digest(api_key, supplied):
            raise HTTPException(403, "Workspace adoption requires the server API key")

    @app.get("/batch-adoption/inspect", dependencies=[Depends(authorized)])
    async def inspect():
        return await report(app, fence)

    @app.post("/batch-adoption/freeze", dependencies=[Depends(authorized)])
    async def freeze(body: FreezeRequest):
        if fence.workers != 1:
            raise HTTPException(
                409, "Restart with one server worker before ownership transfer"
            )
        if fence.owner is not None and fence.owner != body.request_id:
            raise HTTPException(409, "Another ownership transfer holds this workspace")
        fence.owner = body.request_id
        deadline = time.monotonic() + fence.drain_timeout
        while await legacy_busy(app, fence):
            if time.monotonic() >= deadline:
                raise HTTPException(
                    409, "Workspace remains busy; the transfer fence is retained"
                )
            await asyncio.sleep(0.05)
        return await report(app, fence, graph_fingerprint=True)

    @app.post("/batch-adoption/unfreeze", dependencies=[Depends(authorized)])
    async def unfreeze(body: FreezeRequest):
        if fence.owner != body.request_id:
            raise HTTPException(409, "Ownership transfer identity changed")
        if fence.snapshot_active:
            raise HTTPException(409, "Wait for the workspace snapshot to finish")
        fence.owner = None
        return {"status": "released"}

    @app.post("/batch-adoption/snapshot", dependencies=[Depends(authorized)])
    async def capture(body: FreezeRequest):
        if fence.owner != body.request_id or fence.snapshot_active:
            raise HTTPException(409, "This request does not hold an idle source freeze")
        async with fence.snapshot_lock:
            observation = await report(app, fence, graph_fingerprint=True)
            from .adoption_snapshot import snapshot

            directory = (
                Path(app.state.rag.working_dir).resolve()
                / ".adoption-snapshots"
                / app.state.rag.workspace
                / body.request_id
            )
            fence.snapshot_active = True
            try:
                return await snapshot(app.state.rag, directory, observation)
            finally:
                fence.snapshot_active = False


class _FenceMiddleware:
    def __init__(self, app, fence):
        self.app = fence.middleware(app)

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)
