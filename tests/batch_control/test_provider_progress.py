"""Provider queue status belongs to its displayed batch and task."""

# ruff: noqa: F811

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from lightrag.batch_control.deployment import BatchDeployment
from lightrag.batch_control.provider_quota.wiring import ensure
from lightrag.batch_control.routes import create_control_routes
from lightrag.batch_control.task_contracts import TaskCommand
from tests.batch_control.test_runtime import runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline


async def test_queue_status_is_scoped_to_batch_task_and_api_authority(runtime_setup):
    control, policy = runtime_setup
    deployment = BatchDeployment(control, policy)
    service = deployment.service("user")
    first = control.create("user", "First", "first")
    second = control.create("user", "Second", "second")
    task = service.submit(
        first["id"],
        TaskCommand.model_validate(
            {
                "expected_revision": first["active"],
                "request_id": "query",
                "kind": "query",
                "source": {"data_batch_id": first["id"], "type": "index"},
                "query": {"query": "Original question"},
            }
        ),
    )
    store = ensure(control)
    held = store.enqueue(
        "https://model.invalid/v1", "key", second["id"], "second-task", 1
    )
    store.configure({"defaults": {"max_concurrent": 1}})
    assert store.poll(held)["state"] == "granted"
    waiting = store.enqueue(
        "https://model.invalid/v1", "key", first["id"], task["task_id"], 1
    )
    assert store.poll(waiting)["state"] == "queued"
    listed = service.list(first["id"])[0]
    assert listed["provider_admission"] == {"queued": 1, "active": 0}
    assert (
        service.get(first["id"], task["task_id"])["provider_admission"]
        == listed["provider_admission"]
    )

    async def principal():
        return "user"

    app = FastAPI()
    app.include_router(create_control_routes(deployment, principal))
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/data-batches/{first['id']}/provider-admission",
            params={"task_id": task["task_id"]},
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["queued"] == 1
        assert response.json()["items"][0]["active"] == 0
        foreign = await client.get(
            f"/data-batches/{second['id']}/provider-admission",
            params={"task_id": task["task_id"]},
        )
        assert foreign.status_code == 404
        control.authorize = lambda actor, action, resource: False
        denied = await client.get(f"/data-batches/{first['id']}/provider-admission")
        assert denied.status_code == 403
