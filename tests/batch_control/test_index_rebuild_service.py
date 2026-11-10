"""Authenticated reservation, drain, recovery and API contracts with fake processes."""

# ruff: noqa: F811

import json

import pytest

from lightrag.batch_control.deployment import BatchDeployment
from lightrag.batch_control.index_rebuild.service import IndexRebuilds
from lightrag.batch_control.settings import SettingsService
from lightrag.batch_control.storage import Conflict
from lightrag.batch_control.task_store import TaskStore
from .test_applications import setup  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.fixture
def prepared(setup):
    control, policy, backend, runtime = setup
    control.catalog.profiles["embed-v2"] = {
        "model_name": "new-offline-embedding",
        "api_url": "http://127.0.0.1:9/v1",
        "dimensions": 16,
    }
    batch = control.create("user", "Source", "source")
    lease = runtime.start(batch["id"], batch["active"])
    service = IndexRebuilds(control, policy, actor="user", backend=backend)
    return control, policy, backend, runtime, batch, lease, service


def request(prepared):
    *_, batch, lease, service = prepared
    return service.request(batch["id"], batch["active"], "embed-v2", "rebuild")


def finish(service, batch, operation):
    for _ in range(8):
        record = service.step(batch, operation)
        if record["terminal"] or record["state"] == "rollback_failed":
            return record
    pytest.fail("Recovery did not reach a terminal or explicit failed state")


def test_request_freezes_binding_is_idempotent_and_progress_is_separate(prepared):
    control, policy, backend, runtime, batch, lease, service = prepared
    record = request(prepared)
    assert request(prepared) == record
    assert backend.launches == 1 and backend.signals == []
    owner, _, document, _, manifest = control.read(
        "user", batch["id"], record["config_revision_id"]
    )
    assert (
        owner["active"] == batch["active"]
        and owner["pending"] == record["operation_id"]
    )
    assert document.rag.embedding_profile == "embed-v2"
    assert manifest["sources"]["index_binding_id"] == record["operation_id"]
    public = service.public(batch["id"], record["operation_id"])
    assert public["state"] == "draining" and public["can_cancel"]
    assert public["target"]["dimensions"] == 16 and public["progress"] is None
    assert not any(
        value in json.dumps(public)
        for value in ("capability", "spec_path", "worker.log", "control_paths")
    )
    assert service.list(batch["id"])["items"] == [public]
    snapshot = SettingsService(control, policy, actor="user").get(batch["id"])
    assert snapshot["application"] is None
    assert snapshot["index_rebuild"]["operation_id"] == record["operation_id"]
    with pytest.raises(Conflict, match="different request"):
        service.request(batch["id"], batch["active"], "embed-v1", "rebuild")
    with pytest.raises(Conflict, match="pending"):
        service.request(batch["id"], batch["active"], "embed-v2", "other")


def test_drain_waits_for_admitted_work_then_enters_owned_capture(prepared):
    control, _, backend, _, batch, lease, service = prepared
    task = control.register_task(
        "user",
        batch["id"],
        batch["active"],
        "accepted",
        "query",
        {"data_batch_id": batch["id"], "type": "index"},
        payload={"version": 1},
    )
    tasks = TaskStore(control.registry)
    tasks.admit(batch["id"], task["task_id"], lease)
    record = request(prepared)
    waiting = service.step(batch["id"], record["operation_id"])
    assert waiting["state"] == "draining" and waiting["waiting_reason"]
    assert (
        service.step(batch["id"], record["operation_id"])["version"]
        == waiting["version"]
    )
    assert backend.signals == []
    tasks.update(
        batch["id"], task["task_id"], generation=lease.generation, state="completed"
    )
    assert (
        service.step(batch["id"], record["operation_id"])["state"] == "source_capturing"
    )


def test_cancel_restores_previous_runtime_and_preserves_peer(prepared):
    control, _, backend, runtime, batch, lease, service = prepared
    peer = control.create("user", "Peer", "peer")
    peer_lease = runtime.start(peer["id"], peer["active"])
    record = request(prepared)
    service.cancel(batch["id"], record["operation_id"])
    result = finish(service, batch["id"], record["operation_id"])
    assert result["state"] == "rolled_back"
    assert result["restored_lease"]["collections"] == lease.collections
    assert control.registry.get(batch["id"])["pending"] is None
    assert runtime.inspect(peer["id"]) == peer_lease
    assert backend.signals == [lease.pid]


def test_changed_target_profile_fails_before_capture_and_restores_source(prepared):
    control, _, backend, _, batch, lease, service = prepared
    record = request(prepared)
    control.catalog.profiles["embed-v2"]["model_name"] = "changed-after-request"
    failed = service.step(batch["id"], record["operation_id"])
    assert failed["state"] == "rollback_stopping" and failed["error"]
    assert backend.signals == []
    assert (
        finish(service, batch["id"], record["operation_id"])["state"] == "rolled_back"
    )
    assert (
        service.store.binding(batch["id"], record["operation_id"])["state"]
        == "abandoned"
    )


def test_unconfirmed_stop_is_observed_without_another_signal(prepared, monkeypatch):
    control, _, backend, _, batch, lease, service = prepared
    record = request(prepared)
    service.cancel(batch["id"], record["operation_id"])
    service.step(batch["id"], record["operation_id"])

    def timeout(row, deadline):
        backend.signals.append(row["pid"])
        raise TimeoutError("Unconfirmed exit")

    monkeypatch.setattr(backend, "terminate", timeout)
    for _ in range(3):
        assert (
            service.step(batch["id"], record["operation_id"])["state"]
            == "rollback_stopping"
        )
    assert backend.signals == [lease.pid]
    assert control.registry.get(batch["id"])["pending"] == record["operation_id"]
    backend.alive[lease.pid] = False
    assert (
        finish(service, batch["id"], record["operation_id"])["state"] == "rolled_back"
    )


def test_explicit_recovery_can_use_authorized_new_executor_without_rebuilding(prepared):
    control, policy, backend, _, batch, _, service = prepared
    record = request(prepared)
    control.authorize = lambda actor, action, resource: (
        actor == "admin" or action != "rebuild_index"
    )
    failed = service.step(batch["id"], record["operation_id"])
    assert failed["state"] == "rollback_stopping"
    failed = service.step(batch["id"], record["operation_id"])
    assert failed["state"] == "rollback_failed"
    admin = IndexRebuilds(control, policy, actor="admin", backend=backend)
    with pytest.raises(Conflict):
        admin.recover(batch["id"], record["operation_id"], failed["version"] - 1)
    recovered = admin.recover(batch["id"], record["operation_id"], failed["version"])
    assert recovered["executor_actor"] == "admin" and recovered["actor"] == "user"
    assert finish(admin, batch["id"], record["operation_id"])["state"] == "rolled_back"
    assert backend.launches == 2


@pytest.mark.asyncio
async def test_rebuild_api_validates_identity_permissions_and_hides_private_fields(
    prepared,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from lightrag.batch_control.routes import create_control_routes

    control, policy, backend, _, batch, _, _ = prepared
    deployment = BatchDeployment(control, policy)
    deployment.dispatcher.backend = backend
    app = FastAPI()
    app.include_router(create_control_routes(deployment, lambda: "user"))
    root = f"/data-batches/{batch['id']}/index-rebuilds"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        body = {
            "expected_revision": batch["active"],
            "embedding_profile": "embed-v2",
            "request_id": "api",
        }
        assert (
            await client.post(root, json={**body, "source_path": "/untrusted"})
        ).status_code == 422
        response = await client.post(root, json=body)
        assert response.status_code == 202, response.text
        record = response.json()
        operation = record["operation_id"]
        status_url = root + "/" + operation
        assert (await client.get(status_url)).json()["version"] == record["version"]
        assert len((await client.get(root)).json()["items"]) == 1
        other = control.create("user", "Other", "other")
        assert (
            await client.get(f"/data-batches/{other['id']}/index-rebuilds/{operation}")
        ).status_code == 404
        events = (await client.get(status_url + "/events")).json()["items"]
        assert events[0]["event"] == "requested" and "source_lease" not in events[0]
        control.authorize = lambda actor, action, resource: (
            action not in {"rebuild_index", "audit"}
        )
        assert not (await client.get(status_url)).json()["can_cancel"]
        assert (await client.post(status_url + "/cancel")).status_code == 403
        assert (await client.get(status_url + "/events")).status_code == 403
