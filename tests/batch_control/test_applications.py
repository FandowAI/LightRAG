"""Configuration becomes active only after a drained, owned, verified generation."""

import json
from pathlib import Path
import sys
import os

import pytest

from lightrag.batch_control.applications import ApplicationService
from lightrag.batch_control.contracts import Configuration
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_config import RuntimePolicy
from lightrag.batch_control.settings import SettingsService
from lightrag.batch_control.storage import Conflict
from lightrag.batch_control.task_store import TaskStore
from tests.batch_control.helpers import change
from tests.batch_control.test_runtime import FakeBackend
from tests.batch_control.test_full_chain import chain_setup  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.fixture
def setup(factory, monkeypatch):
    catalog = factory[3]
    catalog.profiles["llm-v1"] = {
        "model_name": "offline-chat",
        "api_url": "http://127.0.0.1:9/v1",
    }
    catalog.profiles["embed-v1"] = {
        "model_name": "offline-embedding",
        "api_url": "http://127.0.0.1:9/v1",
        "dimensions": 8,
    }
    monkeypatch.setattr(
        "lightrag.batch_control.runtime_config.selected_fingerprint",
        lambda policy: {"source": "unit"},
    )
    control = factory[0]()
    policy = RuntimePolicy(
        Path(__file__).resolve().parents[2], Path(sys.executable), storage_mode="local"
    )
    backend = FakeBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    return control, policy, backend, runtime


def desired(control, batch, **updates):
    document = control.read("user", batch["id"])[2]
    return control.save(
        "user",
        batch["id"],
        batch["active"],
        change(document, "rag", **(updates or {"max_parallel_insert": 7})),
        "save",
    )


def application(setup, batch, saved):
    control, policy, backend, _ = setup
    service = ApplicationService(control, policy, actor="user", backend=backend)
    result = service.request(
        batch["id"], saved["operation"], saved["revision"], "apply"
    )
    assert result["state"] == "queued"
    return service


def test_restart_promotes_only_verified_generation_and_leaves_other_batch_alone(setup):
    control, _, backend, runtime = setup
    a, b = [control.create("user", "Batch", key) for key in ("a", "b")]
    original = runtime.start(a["id"], a["active"])
    other = runtime.start(b["id"], b["active"])
    saved = desired(control, a)
    service = application(setup, a, saved)
    assert control.registry.get(a["id"])["active"] == a["active"]
    result = service.step(a["id"], saved["operation"])
    assert result["state"] == "applied"
    assert result["lease"]["generation"] == original.generation + 1
    assert control.registry.get(a["id"])["active"] == saved["revision"]
    assert control.registry.get(a["id"])["pending"] is None
    assert runtime.inspect(b["id"]) == other
    assert backend.signals == [original.pid]
    assert service.store.events(a["id"], saved["operation"])[-1]["event"] == "applied"


def test_apply_waits_for_current_task_and_retires_queued_old_revision(setup):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "create")
    lease = runtime.start(batch["id"], batch["active"])
    source = {"data_batch_id": batch["id"], "type": "index"}
    active = control.register_task(
        "user",
        batch["id"],
        batch["active"],
        "active",
        "retrieval",
        source,
        payload={"version": 1},
    )
    queued = control.register_task(
        "user",
        batch["id"],
        batch["active"],
        "queued",
        "retrieval",
        source,
        payload={"version": 1},
    )
    tasks = TaskStore(control.registry)
    tasks.admit(batch["id"], active["task_id"], lease)
    tasks.claim(
        batch["id"],
        lease.generation,
        lease.startup_digest,
        lease.pid,
        lease.process_identity,
    )
    saved = desired(control, batch)
    service = application(setup, batch, saved)
    assert service.step(batch["id"], saved["operation"])["state"] == "draining"
    assert (
        backend.signals == []
        and control.registry.get(batch["id"])["active"] == batch["active"]
    )
    with pytest.raises(Conflict, match="closed"):
        tasks.admit(batch["id"], queued["task_id"], lease)
    tasks.update(
        batch["id"], active["task_id"], generation=lease.generation, state="completed"
    )
    assert service.step(batch["id"], saved["operation"])["state"] == "applied"
    assert tasks.get(batch["id"], queued["task_id"])["state"] == "interrupted"


def test_failed_preflight_retains_verified_old_process_and_reopens_admission(
    setup,
):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "create")
    lease = runtime.start(batch["id"], batch["active"])
    saved = desired(control, batch, chunking_strategy="unsupported_chunker")
    service = application(setup, batch, saved)
    assert service.step(batch["id"], saved["operation"])["state"] == "rolled_back"
    assert backend.signals == [] and runtime.inspect(batch["id"]) == lease
    assert control.registry.get(batch["id"])["active"] == batch["active"]
    assert control.registry.get(batch["id"])["pending"] is None
    assert runtime.inspect(batch["id"]) == lease


def test_bad_readiness_never_promotes_and_retry_requires_new_request_identity(setup):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "create")
    runtime.start(batch["id"], batch["active"])
    saved = desired(control, batch)
    service = application(setup, batch, saved)
    initial_launches = backend.launches
    backend.change = lambda proof: (
        {**proof, "workspace": "wrong"}
        if backend.launches > initial_launches
        else proof
    )
    assert service.step(batch["id"], saved["operation"])["state"] == "rollback_failed"
    assert control.registry.get(batch["id"])["active"] == batch["active"]
    assert (
        service.request(batch["id"], saved["operation"], saved["revision"], "apply")[
            "state"
        ]
        == "rollback_failed"
    )
    backend.change = lambda proof: proof
    assert (
        service.request(batch["id"], saved["operation"], saved["revision"], "retry")[
            "attempt"
        ]
        == 2
    )
    assert service.step(batch["id"], saved["operation"])["state"] == "applied"


def test_embedding_change_race_is_rechecked_before_stopping_old_runtime(
    setup, monkeypatch
):
    control, _, backend, runtime = setup
    control.catalog.profiles["embed-v2"] = {
        **control.catalog.profiles["embed-v1"],
        "dimensions": 16,
    }
    batch = control.create("user", "Batch", "create")
    prepare = control._prepare

    def racing_prepare(*args, **kwargs):
        result = prepare(*args, **kwargs)
        runtime.start(batch["id"], batch["active"])
        return result

    monkeypatch.setattr(control, "_prepare", racing_prepare)
    saved = desired(control, batch, embedding_profile="embed-v2")
    service = application(setup, batch, saved)
    result = service.step(batch["id"], saved["operation"])
    assert result["state"] == "rolled_back" and "Embedding" in result["error"]
    assert backend.signals == []


def test_settings_inline_prompt_is_versioned_and_retrieval_save_needs_no_restart(setup):
    control, policy, _, _ = setup
    batch = control.create("user", "Batch", "create")
    settings = SettingsService(control, policy, actor="user")
    snapshot = settings.get(batch["id"])
    draft = change(
        Configuration.model_validate(snapshot["configuration"]),
        "retrieval",
        batch={"rag_top_k": 12},
    )
    contents = {"qa": "Changed QA: $payload"}
    preview = settings.preview(
        batch["id"], batch["active"], draft, prompt_contents=contents
    )
    assert not preview["requires_restart"]
    result = settings.save(
        batch["id"], batch["active"], draft, "save", prompt_contents=contents
    )
    assert result["status"] == "applied"
    assert control.read("user", batch["id"])[3]["qa"] == contents["qa"]
    assert (
        settings.save(
            batch["id"], batch["active"], draft, "save", prompt_contents=contents
        )
        == result
    )
    assert "credentials_file" not in json.dumps(snapshot["options"])


def test_apply_permission_is_checked_before_any_process_signal(setup):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "create")
    runtime.start(batch["id"], batch["active"])
    saved = desired(control, batch)
    control.authorize = lambda actor, action, resource: action != "apply_config"
    with pytest.raises(PermissionError):
        application(setup, batch, saved)
    assert backend.signals == []


def test_real_apply_restarts_only_target_and_commits_after_actual_health_proof(
    chain_setup,  # noqa: F811
):
    from lightrag.batch_control.runtime_process import atomic_json

    control, policy, *_ = chain_setup
    runtime = RuntimeSupervisor(control, policy, actor="user")
    a, b = [control.create("user", "Batch", name) for name in ("first", "second")]
    before, other = [runtime.start(batch["id"], batch["active"]) for batch in (a, b)]
    try:
        saved = desired(control, a)
        application = ApplicationService(
            control, policy, actor="user", backend=runtime.backend
        )
        application.request(a["id"], saved["operation"], saved["revision"], "apply")
        applied = application.step(a["id"], saved["operation"])
        assert applied["state"] == "applied", applied
        after = runtime.inspect(a["id"])
        assert after.pid != before.pid and after.generation == before.generation + 1
        assert runtime.inspect(b["id"]) == other
        assert control.registry.get(a["id"])["active"] == saved["revision"]
        assert (
            runtime.backend.observe(runtime.store.get(a["id"]))["parameters"][
                "max_parallel_insert"
            ]
            == 7
        )
        archive = os.getenv("LIGHTRAG_APPLICATION_VALIDATION_RECORD")
        if archive:
            atomic_json(
                Path(archive),
                {
                    "schema_version": 1,
                    "kind": "real_local_rag_configuration_apply",
                    "old_lease": before.model_dump(),
                    "new_lease": after.model_dump(),
                    "unaffected_lease": other.model_dump(),
                    "application": applied,
                    "events": application.store.events(a["id"], saved["operation"]),
                    "external_model_requests": 0,
                    "active_after_readiness_verified": True,
                },
            )
    finally:
        for batch in (a, b):
            runtime.stop(batch["id"])
    if archive:
        record = json.loads(Path(archive).read_text())
        record["all_children_stopped"] = all(
            runtime.store.get(batch["id"])["state"] == "stopped" for batch in (a, b)
        )
        atomic_json(Path(archive), record)


@pytest.mark.asyncio
async def test_settings_and_application_routes_keep_identity_and_permissions(setup):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from lightrag.batch_control.deployment import BatchDeployment
    from lightrag.batch_control.routes import create_control_routes

    control, policy, _, _ = setup
    batch = control.create("user", "Batch", "create")
    app = FastAPI()
    app.include_router(
        create_control_routes(BatchDeployment(control, policy), lambda: "user")
    )
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        snapshot = (await client.get(f"/data-batches/{batch['id']}/settings")).json()
        draft = snapshot["configuration"]
        draft["rag"]["max_parallel_insert"] = 7
        body = {
            "expected_revision": batch["active"],
            "request_id": "save",
            "configuration": draft,
        }
        preview = await client.post(
            f"/data-batches/{batch['id']}/settings/preview", json=body
        )
        assert preview.status_code == 200 and preview.json()["requires_restart"]
        saved = (
            await client.put(f"/data-batches/{batch['id']}/settings", json=body)
        ).json()
        operation = saved["operation"]
        pending = await client.get(
            f"/data-batches/{batch['id']}/applications/{operation}"
        )
        assert pending.json()["state"] == "awaiting_action"
        assert (
            await client.post(
                f"/data-batches/{batch['id']}/applications/{operation}/start",
                json={"request_id": "apply", "expected_revision": "rev-wrong"},
            )
        ).status_code == 409
        result = await client.post(
            f"/data-batches/{batch['id']}/applications/{operation}/start",
            json={"request_id": "apply", "expected_revision": saved["revision"]},
        )
        assert result.status_code == 202 and result.json()["state"] == "queued"
        assert control.registry.get(batch["id"])["active"] == batch["active"]
        discarded = await client.post(
            f"/data-batches/{batch['id']}/applications/{operation}/discard"
        )
        assert discarded.json()["state"] == "discarded"
        other = control.create("user", "Other", "other")
        assert (
            await client.get(f"/data-batches/{other['id']}/applications/{operation}")
        ).status_code == 404


@pytest.mark.asyncio
async def test_settings_html_and_assets_preserve_proxy_prefix(tmp_path):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from lightrag.batch_control.ui import mount_settings_ui

    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "batch-settings.html").write_text(
        '<html><!-- __LIGHTRAG_RUNTIME_CONFIG__ --><script src="./assets/settings.js"></script></html>'
    )
    (assets / "settings.js").write_text("export const settings = true")
    app = FastAPI(root_path="/proxy")
    mount_settings_ui(app, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test/proxy"
    ) as client:
        response = await client.get("/batch-settings/")
        assert response.status_code == 200 and '"apiPrefix": "/proxy"' in response.text
        assert response.headers["cache-control"] == "no-store"
        assert (
            await client.get("/batch-settings/assets/settings.js")
        ).status_code == 200
        assert (await client.get("/batch-settings")).headers[
            "location"
        ] == "/proxy/batch-settings/"


def test_invalid_inline_template_never_publishes_another_revision(setup):
    control, policy, _, _ = setup
    batch = control.create("user", "Batch", "create")
    settings = SettingsService(control, policy, actor="user")
    draft = control.read("user", batch["id"])[2]
    with pytest.raises(ValueError):
        settings.save(
            batch["id"],
            batch["active"],
            draft,
            "save",
            prompt_contents={"qa": "$unknown_variable"},
        )
    assert control.registry.get(batch["id"])["active"] == batch["active"]
