"""Exercise generation checks, response streaming and actual child business routes."""

import asyncio
import json
from types import SimpleNamespace
import os
from pathlib import Path
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
import pytest

from lightrag.batch_control.applications import ApplicationService
from lightrag.batch_control.deployment import BatchDeployment
from lightrag.batch_control.http_store import HttpStore, business_action
from lightrag.batch_control.http_worker import HttpRunner
from lightrag.batch_control.routes import create_control_routes
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.storage import Conflict
from lightrag.batch_control.task_contracts import TaskCommand
from lightrag.batch_control.runtime_process import atomic_json
from tests.batch_control.helpers import change
from tests.batch_control.test_full_chain import chain_setup  # noqa: F401
from tests.batch_control.test_runtime import FakeBackend

pytestmark = pytest.mark.offline


@pytest.fixture
def mailbox(chain_setup, monkeypatch):  # noqa: F811
    control, policy, *_ = chain_setup
    monkeypatch.setattr(
        "lightrag.batch_control.runtime_config.selected_fingerprint",
        lambda policy: {"source": "http-unit"},
    )
    batch = control.create("user", "HTTP batch", "create")
    backend = FakeBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    lease = runtime.start(batch["id"], batch["active"])
    store = HttpStore(control.registry, control.paths)
    spec = {
        **backend.specs[lease.pid],
        "pid": lease.pid,
        "process_identity": lease.process_identity,
    }
    return control, policy, batch, runtime, lease, store, spec


@pytest.mark.parametrize(
    "path",
    [
        "/auth-status",
        "/login",
        "/data-batches",
        "/docs",
        "/documents/../login",
        "//documents",
        "/integrations/other-service/recipe-settings",
    ],
)
def test_manager_and_auth_routes_are_not_forwarded(path):
    with pytest.raises(ValueError):
        business_action("POST", path)


@pytest.mark.parametrize(
    "method,path,action",
    [
        ("GET", "/entity-merge", "read"),
        ("GET", "/prompt-admin", "read"),
        ("PUT", "/prompt-admin/graph_answer/draft", "edit"),
        ("GET", "/integrations/data-work-flow/recipe-settings", "read"),
        ("PUT", "/integrations/data-work-flow/recipe-settings", "edit"),
        ("POST", "/integrations/data-work-flow/submit", "submit"),
        ("PUT", "/entity-merge", "edit"),
        ("POST", "/entity-merge/preview", "submit"),
    ],
)
def test_entity_merge_routes_have_explicit_batch_permissions(method, path, action):
    assert business_action(method, path) == action


@pytest.mark.asyncio
async def test_entity_merge_write_uses_private_generation_auth_and_preserves_other_workspace(
    mailbox, tmp_path, monkeypatch
):
    from lightrag.api import utils_api
    from lightrag.api.routers.entity_merge_routes import create_entity_merge_routes
    from lightrag.entity_merge import EntityMergeManager

    monkeypatch.setenv("ENTERPRISE_KNOWLEDGE_ENABLED", "false")
    monkeypatch.setattr(utils_api, "auth_configured", False)
    _, _, batch, _, lease, store, spec = mailbox

    def manager(workspace):
        rag = SimpleNamespace(
            workspace=workspace,
            embedding_func=SimpleNamespace(model_name="offline", embedding_dim=8),
            vector_db_storage_cls=lambda **kwargs: SimpleNamespace(
                index_config=SimpleNamespace(metric_type="COSINE")
            ),
        )
        return EntityMergeManager(rag, str(tmp_path / "settings"), workspace)

    own, peer = manager(batch["workspace"]), manager("peer-workspace")
    app = FastAPI()
    app.include_router(create_entity_merge_routes(own, spec["capability"], 1))
    payload = {
        "revision": 0,
        "settings": {
            "enabled": True,
            "similarity_threshold": 0.93,
            "margin_threshold": 0.1,
            "top_k": 33,
        },
    }
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://private-child"
    ) as client:
        assert (await client.put("/entity-merge", json=payload)).status_code in {
            401,
            403,
        }
    row = store.submit(
        "user",
        batch["id"],
        lease,
        "PUT",
        "/entity-merge",
        "",
        [
            ["Content-Type", "application/json"],
            ["X-API-Key", "untrusted-manager-key"],
            ["Authorization", "Bearer untrusted"],
        ],
        json.dumps(payload).encode(),
    )
    claimed = store.claim(spec)
    await HttpRunner(app, spec).execute(claimed)
    result = store.get(batch["id"], row["id"])
    assert result["status"] == 200
    body = json.loads(
        (store.directory(batch["id"], row["id"]) / "response.bin").read_bytes()
    )
    assert body["workspace"] == batch["workspace"] and body["write_enabled"]
    assert own.snapshot()["settings"]["top_k"] == 33 and own.snapshot()["revision"] == 1
    assert peer.snapshot()["revision"] == 0
    assert spec["capability"] not in json.dumps(body)


@pytest.mark.asyncio
async def test_entity_merge_parent_permissions_reject_edits_and_paid_previews(mailbox):
    control, policy, batch, runtime, _, _, _ = mailbox
    deployment = BatchDeployment(control, policy)
    deployment.dispatcher.backend = runtime.backend
    app = FastAPI()
    app.include_router(create_control_routes(deployment, lambda: "user"))
    control.authorize = lambda actor, action, resource: (
        action in {"read", "run_runtime"}
    )
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://manager"
    ) as client:
        base = f"/data-batches/{batch['id']}/api/entity-merge"
        assert (await client.put(base, json={})).status_code == 403
        assert (await client.post(base + "/preview", json={})).status_code == 403
    assert runtime.backend.signals == []


def test_claim_is_lease_bound_and_configuration_pending_closes_admission(mailbox):
    control, _, batch, _, lease, store, spec = mailbox
    row = store.submit("user", batch["id"], lease, "GET", "/health", "", [], b"")
    assert store.claim({**spec, "generation": 999}) is None
    claimed = store.claim(spec)
    assert claimed["id"] == row["id"] and claimed["state"] == "running"
    assert store.claim(spec) is None
    with pytest.raises(KeyError):
        store.get("batch-foreign", row["id"])
    document = control.read("user", batch["id"])[2]
    control.save(
        "user",
        batch["id"],
        batch["active"],
        change(document, "rag", max_parallel_insert=7),
        "pending",
    )
    with pytest.raises(Conflict):
        store.submit(
            "user", batch["id"], lease, "POST", "/documents/upload", "", [], b"file"
        )


def test_lost_generation_retires_running_and_queued_http_without_replaying(mailbox):
    _, _, batch, runtime, lease, store, spec = mailbox
    running = store.submit(
        "user", batch["id"], lease, "POST", "/documents/text", "", [], b"text"
    )
    queued = store.submit("user", batch["id"], lease, "GET", "/graphs", "", [], b"")
    completed = store.submit("user", batch["id"], lease, "GET", "/health", "", [], b"")
    store.update(
        batch["id"], completed["id"], lease.generation, state="completed", status=200
    )
    assert store.claim(spec)["id"] == running["id"]
    # An uncertain stop does not establish process exit and must retain requests.
    runtime.store.update(
        batch["id"],
        lease.generation,
        {"ready"},
        "user",
        "stop_requested",
        state="stopping",
    )
    assert len(store.active(batch["id"])) == 2
    runtime.store.update(
        batch["id"],
        lease.generation,
        {"stopping"},
        "user",
        "process_lost",
        state="failed",
    )
    assert store.active(batch["id"]) == []
    for row in (running, queued):
        result = store.get(batch["id"], row["id"])
        assert result["state"] == "failed" and "no request replayed" in result["error"]
    assert store.get(batch["id"], completed["id"])["state"] == "completed"


@pytest.mark.asyncio
async def test_stream_is_visible_before_the_child_response_finishes(mailbox):
    _, _, batch, _, lease, store, spec = mailbox
    app = FastAPI()
    released = asyncio.Event()

    @app.get("/query/stream")
    async def stream():
        async def chunks():
            yield b'{"response":"first"}\n'
            await released.wait()
            yield b'{"response":"second"}\n'

        return StreamingResponse(chunks(), media_type="application/x-ndjson")

    row = store.submit("user", batch["id"], lease, "GET", "/query/stream", "", [], b"")
    claimed = store.claim(spec)
    runner = HttpRunner(app, spec)
    task = asyncio.create_task(runner.execute(claimed))
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = store.get(batch["id"], row["id"])
            if current["bytes_written"]:
                break
            await asyncio.sleep(0.01)
        assert current["status"] == 200 and current["state"] == "running"
        response = store.directory(batch["id"], row["id"]) / "response.bin"
        assert response.read_bytes() == b'{"response":"first"}\n'
        released.set()
        await asyncio.wait_for(task, 3)
        assert store.get(batch["id"], row["id"])["state"] == "completed"
        assert b"second" in response.read_bytes()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_changed_request_bytes_fail_before_the_business_handler(mailbox):
    _, _, batch, _, lease, store, spec = mailbox
    calls = []
    app = FastAPI()

    @app.post("/documents/upload")
    async def upload():
        calls.append(True)
        return {"accepted": True}

    row = store.submit(
        "user", batch["id"], lease, "POST", "/documents/upload", "", [], b"original"
    )
    claimed = store.claim(spec)
    (store.directory(batch["id"], row["id"]) / "request.bin").write_bytes(b"changed")
    await HttpRunner(app, spec).execute(claimed)
    assert calls == []
    assert store.get(batch["id"], row["id"])["state"] == "failed"


def test_application_waits_for_http_work_without_signalling_the_worker(mailbox):
    control, policy, batch, runtime, lease, store, _ = mailbox
    row = store.submit("user", batch["id"], lease, "GET", "/graphs", "label=*", [], b"")
    document = control.read("user", batch["id"])[2]
    saved = control.save(
        "user",
        batch["id"],
        batch["active"],
        change(document, "rag", max_parallel_insert=7),
        "pending",
    )
    application = ApplicationService(
        control, policy, actor="user", backend=runtime.backend
    )
    application.request(batch["id"], saved["operation"], saved["revision"], "apply")
    assert application.step(batch["id"], saved["operation"])["state"] == "draining"
    assert runtime.backend.signals == []
    store.cancel(batch["id"], row["id"])
    assert application.step(batch["id"], saved["operation"])["state"] == "applied"


@pytest.mark.asyncio
async def test_explicit_retry_creates_one_new_task_without_replaying_the_old_attempt(
    mailbox,
):
    control, policy, batch, runtime, _, _, _ = mailbox
    deployment = BatchDeployment(control, policy)
    deployment.dispatcher.backend = runtime.backend
    service = deployment.service("user")
    original = service.submit(
        batch["id"],
        TaskCommand.model_validate(
            {
                "expected_revision": batch["active"],
                "request_id": "initial",
                "kind": "centers",
                "source": {"data_batch_id": batch["id"], "type": "index"},
            }
        ),
    )
    service.cancel(batch["id"], original["task_id"])
    app = FastAPI()
    app.include_router(create_control_routes(deployment, lambda: "user"))
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        url = f"/data-batches/{batch['id']}/tasks/{original['task_id']}/retry"
        first = await client.post(
            url,
            json={"expected_revision": batch["active"], "request_id": "explicit-retry"},
        )
        second = await client.post(
            url,
            json={"expected_revision": batch["active"], "request_id": "explicit-retry"},
        )
    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert first.json()["task_id"] != original["task_id"]
    assert service.get(batch["id"], original["task_id"])["state"] == "cancelled"


@pytest.mark.asyncio
async def test_actual_workers_serve_business_http_in_the_requested_workspace(
    chain_setup,  # noqa: F811
):
    control, policy, *_ = chain_setup
    batches = [control.create("user", "HTTP batch", key) for key in ("a", "b")]
    deployment = BatchDeployment(control, policy)
    app = FastAPI()
    app.include_router(create_control_routes(deployment, lambda: "user"))
    proof = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app), base_url="http://test", timeout=90
        ) as client:
            for batch in batches:
                health = await client.get(f"/data-batches/{batch['id']}/api/health")
                assert health.status_code == 200, health.text
                assert health.json()["configuration"]["workspace"] == batch["workspace"]
                graph = await client.get(
                    f"/data-batches/{batch['id']}/api/graphs",
                    params={"label": "*", "max_depth": 1, "max_nodes": 10},
                )
                assert graph.status_code == 200, graph.text
                assert graph.json()["nodes"] == []
            denied = await client.get(
                f"/data-batches/{batches[0]['id']}/api/auth-status"
            )
            assert denied.status_code == 422
            missing = await client.get("/data-batches/batch-foreign/api/health")
            assert missing.status_code == 404
            requests = [
                HttpStore(control.registry, control.paths).active(batch["id"])
                for batch in batches
            ]
            assert requests == [[], []]
            proof = {
                "schema_version": 1,
                "kind": "actual_child_business_http",
                "workspaces": [batch["workspace"] for batch in batches],
                "leases": [
                    deployment.service("user").runtime.inspect(batch["id"]).model_dump()
                    for batch in batches
                ],
                "health_and_graph_verified": True,
                "manager_auth_path_not_forwarded": True,
                "foreign_batch_rejected": True,
                "external_model_requests": 0,
            }
    finally:
        for batch in batches:
            await asyncio.to_thread(
                deployment.service("user").runtime.stop, batch["id"]
            )
    if proof is not None:
        proof["all_children_stopped"] = all(
            deployment.service("user").runtime.store.get(batch["id"])["state"]
            == "stopped"
            for batch in batches
        )
        assert proof["all_children_stopped"]
        archive = os.getenv("LIGHTRAG_HTTP_VALIDATION_RECORD")
        if archive:
            atomic_json(Path(archive), proof)
