"""Frozen legacy workspaces retain ownership until a verified transfer finishes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
import pytest

from lightrag.batch_control.bindings import graph_baseline
from lightrag.batch_control.legacy import mount_legacy_adoption, storage_identity

pytestmark = pytest.mark.offline


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lightrag.api.config.get_config",
        lambda: SimpleNamespace(
            llm_model="model-v1",
            llm_binding="openai",
            llm_binding_host="http://127.0.0.1:9/v1",
            llm_binding_api_key=None,
            timeout=180,
            embedding_binding="openai",
            embedding_binding_host="http://127.0.0.1:9/v1",
            embedding_func_max_async=8,
            embedding_batch_num=32,
            embedding_token_limit=8192,
            embedding_binding_api_key=None,
            rerank_binding=None,
        ),
    )
    for name in ("ENTITY_TYPE_PROMPT_FILE", "ENTITY_IDENTITY_REGISTRY_FILE"):
        monkeypatch.delenv(name, raising=False)

    def storage(name, **kwargs):
        return type(name, (), kwargs)()

    graph = storage(
        "Neo4JStorage",
        _get_raw_workspace_label=lambda self: "legacy",
        get_all_nodes=AsyncMock(return_value=[{"id": "one"}, {"id": "two"}]),
        get_all_edges=AsyncMock(return_value=[{"source": "one", "target": "two"}]),
    )
    rag = SimpleNamespace(
        workspace="legacy",
        working_dir=str(tmp_path / "rag"),
        input_dir=str(tmp_path / "inputs"),
        embedding_func=SimpleNamespace(model_name="embedding-v1", embedding_dim=8),
        chunk_entity_relation_graph=graph,
        full_docs=storage("JsonKVStorage"),
        doc_status=storage(
            "JsonDocStatusStorage",
            get_status_counts=AsyncMock(return_value={"processed": 2, "failed": 1}),
        ),
        chunk_token_size=1200,
        chunk_overlap_token_size=100,
        max_parallel_insert=5,
        max_parallel_parse_native=5,
        llm_model_max_async=15,
        get_llm_role_config=lambda: {"query": {"model": "model-v1"}},
    )
    for key in ("entities", "relationships", "chunks"):
        setattr(
            rag,
            key + "_vdb",
            storage(
                "MilvusVectorDBStorage",
                namespace=key,
                final_namespace="legacy_" + key + "_embedding_v1_8d",
            ),
        )
    app = FastAPI()
    app.state.rag = rag
    pipeline = {"busy": False, "scanning": False, "pending_enqueues": 0}
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_data",
        AsyncMock(return_value=pipeline),
    )
    mount_legacy_adoption(app, "test-secret")

    @app.post("/documents/text")
    async def mutation():
        return {"accepted": True}

    return app, rag, pipeline


async def test_inspection_requires_exact_api_key_even_with_public_whitelist(
    legacy, monkeypatch
):
    app, rag, _ = legacy
    monkeypatch.setenv("WHITELIST_PATHS", "/*")
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        for headers in (
            {},
            {"X-API-Key": "wrong"},
            {"Authorization": "Bearer arbitrary"},
        ):
            assert (
                await client.get("/batch-adoption/inspect", headers=headers)
            ).status_code == 403
        response = await client.get(
            "/batch-adoption/inspect", headers={"X-API-Key": "test-secret"}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["collections"] == storage_identity(rag)["collections"]
        assert result["document_states"] == {"processed": 2, "failed": 1}
        assert result["parameters"]["max_async_llm"] == 15
        assert result["environment"]["VLM_PROCESS_ENABLE"] == "false"
        assert "test-secret" not in response.text
        assert "graph" not in result


async def test_freeze_is_idempotent_blocks_writes_and_only_its_owner_can_release(
    legacy,
):
    app, rag, _ = legacy
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
        headers={"X-API-Key": "test-secret"},
    ) as client:
        frozen = await client.post(
            "/batch-adoption/freeze", json={"request_id": "transfer-a"}
        )
        assert frozen.status_code == 200
        assert frozen.json()["graph"] == await graph_baseline(
            rag.chunk_entity_relation_graph
        )
        assert (await client.post("/documents/text")).status_code == 409
        again = await client.post(
            "/batch-adoption/freeze", json={"request_id": "transfer-a"}
        )
        assert again.json()["boot_id"] == frozen.json()["boot_id"]
        assert (
            await client.post(
                "/batch-adoption/freeze", json={"request_id": "transfer-b"}
            )
        ).status_code == 409
        assert (
            await client.post(
                "/batch-adoption/unfreeze", json={"request_id": "transfer-b"}
            )
        ).status_code == 409
        assert (await client.post("/documents/text")).status_code == 409
        assert (
            await client.post(
                "/batch-adoption/unfreeze", json={"request_id": "transfer-a"}
            )
        ).status_code == 200
        assert (await client.post("/documents/text")).status_code == 200


async def test_stream_in_flight_must_finish_before_frozen_baseline_is_taken(legacy):
    app, _, _ = legacy
    started, finish = asyncio.Event(), asyncio.Event()

    @app.get("/query/stream")
    async def stream():
        async def body():
            started.set()
            yield b"first"
            await finish.wait()
            yield b"last"

        return StreamingResponse(body())

    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
        headers={"X-API-Key": "test-secret"},
    ) as client:
        query = asyncio.create_task(client.get("/query/stream"))
        await asyncio.wait_for(started.wait(), 2)
        freeze = asyncio.create_task(
            client.post("/batch-adoption/freeze", json={"request_id": "transfer-a"})
        )
        try:
            for _ in range(50):
                if app.state.legacy_fence.owner:
                    break
                await asyncio.sleep(0.01)
            assert app.state.legacy_fence.owner == "transfer-a"
            assert not freeze.done()
            assert (await client.post("/documents/text")).status_code == 409
            finish.set()
            assert (await query).text == "firstlast"
            response = await freeze
            assert response.status_code == 200 and response.json()["busy"] is False
        finally:
            finish.set()
            await asyncio.gather(query, freeze, return_exceptions=True)


async def test_busy_timeout_keeps_fence_and_multiple_workers_cannot_claim_exclusivity(
    legacy,
):
    app, _, pipeline = legacy
    fence = app.state.legacy_fence
    fence.drain_timeout = 0
    pipeline["pending_enqueues"] = 1
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
        headers={"X-API-Key": "test-secret"},
    ) as client:
        assert (
            await client.post(
                "/batch-adoption/freeze", json={"request_id": "transfer-a"}
            )
        ).status_code == 409
        assert fence.owner == "transfer-a"
        assert (await client.post("/documents/text")).status_code == 409
        pipeline["pending_enqueues"] = 0
        assert (
            await client.post(
                "/batch-adoption/unfreeze", json={"request_id": "transfer-a"}
            )
        ).status_code == 200
        fence.workers = 2
        assert (
            await client.post(
                "/batch-adoption/freeze", json={"request_id": "transfer-b"}
            )
        ).status_code == 409
        assert fence.owner is None
