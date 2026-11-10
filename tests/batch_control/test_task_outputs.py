"""Published files and logs remain scoped to their batch, task and generation."""

# ruff: noqa: F811

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import json

from lightrag.batch_control.chain_service import ChainService
from lightrag.batch_control.deployment import BatchDeployment
from lightrag.batch_control.routes import create_control_routes
from lightrag.batch_control.task_store import TaskStore
from lightrag.batch_control.storage import Conflict, IntegrityError
from tests.batch_control.test_applications import setup  # noqa: F401

pytestmark = pytest.mark.offline


def task(control, batch, request="task"):
    return control.register_task(
        "user",
        batch["id"],
        batch["active"],
        request,
        "query",
        {"data_batch_id": batch["id"], "type": "index"},
        payload={"version": 1},
    )


async def test_artifact_catalog_and_download_never_accept_another_batch_task(setup):
    control, policy, _, _ = setup
    batch, peer = [control.create("user", "Batch", key) for key in ("a", "b")]
    record = task(control, batch)
    service = ChainService(control, policy, actor="user")
    with pytest.raises(Conflict):
        service.artifacts(batch["id"], record["task_id"])
    root = (
        control.paths.runtime_root
        / "tasks"
        / batch["namespace"]
        / record["task_id"]
        / "workflow"
    )
    output = root / "outputs" / "run-test"
    output.mkdir(parents=True)
    (root / "input.jsonl").write_text('{"text":"original evidence"}\n')
    (output / "review.jsonl").write_text('{"review":"published"}\n')
    (output / "run.json").write_text('{"run_id":"run-test"}')
    store = TaskStore(control.registry)
    store.update(
        batch["id"],
        record["task_id"],
        state="completed",
        result={
            "distillation": {"result": {"run_id": "run-test", "directory": str(output)}}
        },
    )
    app = FastAPI()
    app.include_router(
        create_control_routes(BatchDeployment(control, policy), lambda: "user")
    )
    base = f"/data-batches/{batch['id']}/tasks/{record['task_id']}/artifacts"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(base)
        assert response.status_code == 200
        assert {row["artifact"] for row in response.json()["items"]} == {
            "workflow-input",
            "workflow-review",
            "workflow-run",
        }
        assert str(root) not in response.text
        assert (
            await client.get(base + "/workflow-review")
        ).text == '{"review":"published"}\n'
        assert (
            await client.get(base.replace(batch["id"], peer["id"]))
        ).status_code == 404
        assert (
            await client.get(base.replace(batch["id"], peer["id"]) + "/workflow-review")
        ).status_code == 404
    unchanged = store.update(
        batch["id"],
        record["task_id"],
        result={
            "distillation": {
                "result": {"run_id": "run-test", "directory": str(output.parent)}
            }
        },
    )
    assert unchanged["result"]["distillation"]["result"]["directory"] == str(output)
    corrupt = store.get(batch["id"], record["task_id"])
    corrupt["result"]["distillation"]["result"]["directory"] = str(output.parent)
    with control.registry.transaction() as db:
        db.execute(
            "UPDATE tasks SET record=? WHERE batch=? AND id=?",
            (json.dumps(corrupt), batch["id"], record["task_id"]),
        )
    with pytest.raises(IntegrityError):
        service.artifacts(batch["id"], record["task_id"])


def test_failed_task_without_workflow_results_has_an_empty_catalog(setup):
    control, policy, _, _ = setup
    batch = control.create("user", "Batch", "a")
    record = task(control, batch)
    TaskStore(control.registry).update(
        batch["id"], record["task_id"], state="failed", result=None
    )
    assert ChainService(control, policy, actor="user").artifacts(
        batch["id"], record["task_id"]
    ) == {"items": []}


async def test_worker_logs_use_the_task_generation_and_require_audit_permission(setup):
    control, policy, _, runtime = setup
    batch, peer = [control.create("user", "Batch", key) for key in ("a", "b")]
    lease = runtime.start(batch["id"], batch["active"])
    record = task(control, batch)
    TaskStore(control.registry).admit(batch["id"], record["task_id"], lease)
    generation = control.paths.runtime_root / "batch-runtime" / batch["namespace"]
    (generation / "generation-1/worker.log").write_text("original generation log")
    runtime.stop(batch["id"])
    runtime.start(batch["id"], batch["active"])
    (generation / "generation-2/worker.log").write_text("different generation")
    app = FastAPI()
    app.include_router(
        create_control_routes(BatchDeployment(control, policy), lambda: "user")
    )
    path = f"/data-batches/{batch['id']}/tasks/{record['task_id']}/logs?stream=worker&offset=0&limit=8"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "text": "original",
            "offset": 0,
            "next_offset": 8,
            "has_more": True,
        }
        assert (
            await client.get(path.replace(batch["id"], peer["id"]))
        ).status_code == 404
        control.authorize = lambda actor, action, resource: action != "audit"
        assert (await client.get(path)).status_code == 403
