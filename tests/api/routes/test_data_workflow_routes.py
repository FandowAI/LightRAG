from pathlib import Path
import sys
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

with patch.object(sys, "argv", ["lightrag-server"]):
    from lightrag.api.routers import data_workflow_routes as routes
from lightrag.integrations.data_workflow.storage import JobConflict
from lightrag.integrations.data_workflow.progress import public_job

pytestmark = pytest.mark.offline


@pytest.fixture
async def client(monkeypatch):
    def auth_factory(api_key, *, respect_whitelist):
        assert respect_whitelist is False

        async def auth(x_api_key: str | None = Header(None)):
            if x_api_key != api_key:
                raise HTTPException(403, "Invalid API Key")

        return auth

    monkeypatch.setattr(routes, "get_combined_auth_dependency", auth_factory)
    service = Mock()

    def job(status):
        return public_job(
            {
                "job_id": "distill-1",
                "request_id": "click-1",
                "status": status,
                "phase": status,
                "created_at": "2026-10-22T00:00:00+00:00",
                "updated_at": "2026-10-22T00:00:00+00:00",
                "directory": "/not-created/job",
            }
        )

    service.submit.return_value = job("preparing")
    service.submit_batch.return_value = job("preparing")
    service.get.return_value = job("running")
    service.start.return_value = job("starting")
    service.list.return_value = {
        "items": [job("running")],
        "total": 1,
        "limit": 20,
        "offset": 0,
    }
    service.logs.return_value = {
        "text": "log",
        "offset": 3,
        "next_offset": 6,
        "has_more": False,
    }
    app = FastAPI()
    app.include_router(routes.create_data_workflow_routes(service, "test-secret"))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "test-secret"},
    ) as http:
        yield http, service


async def test_start_button_only_queues_the_complete_pipeline(client):
    http, service = client
    response = await http.post(
        "/integrations/data-work-flow/submit", json={"request_id": "click-1"}
    )
    assert response.status_code == 202
    service.submit.assert_called_once_with("click-1", True)
    assert (
        response.json()["links"]["status"]
        == "/integrations/data-work-flow/jobs/distill-1"
    )


async def test_prepare_only_option_and_independent_polling(client):
    http, service = client
    assert (
        await http.post(
            "/integrations/data-work-flow/submit",
            json={"request_id": "click-1", "start": False},
        )
    ).status_code == 202
    service.submit.assert_called_once_with("click-1", False)
    assert (await http.get("/integrations/data-work-flow/jobs")).status_code == 200
    assert (await http.get("/integrations/data-work-flow/jobs/distill-1")).json()[
        "status"
    ] == "running"
    assert (
        await http.post("/integrations/data-work-flow/jobs/distill-1/start")
    ).status_code == 202
    assert (
        await http.get(
            "/integrations/data-work-flow/jobs/distill-1/logs?offset=3&limit=100"
        )
    ).status_code == 200
    service.logs.assert_called_once_with("distill-1", "stderr", 3, 100)


async def test_recipe_settings_and_explicit_retry_use_existing_authentication(client):
    http, service = client
    service.recipe_settings.return_value = {"revision": "a" * 64, "settings": {}}
    service.save_recipe_settings.return_value = {"revision": "b" * 64}
    assert (
        await http.get("/integrations/data-work-flow/recipe-settings")
    ).status_code == 200
    assert (
        await http.get(
            "/integrations/data-work-flow/recipe-settings",
            headers={"X-API-Key": "invalid"},
        )
    ).status_code == 403
    saved = await http.put(
        "/integrations/data-work-flow/recipe-settings",
        json={
            "revision": "a" * 64,
            "settings": {
                "questions_per_source": 2,
            },
        },
    )
    assert saved.status_code == 200
    assert service.save_recipe_settings.call_args.args[1].questions_per_source == 2
    invalid = await http.put(
        "/integrations/data-work-flow/recipe-settings",
        json={"revision": "a" * 64, "settings": {"models": []}},
    )
    assert invalid.status_code == 422
    service.retry.return_value = service.submit.return_value
    response = await http.post(
        "/integrations/data-work-flow/jobs/distill-1/retry",
        json={"request_id": "retry-click"},
    )
    assert response.status_code == 202
    service.retry.assert_called_once_with("distill-1", "retry-click", True)


async def test_all_endpoints_require_authentication(client):
    http, _ = client
    for method, path in [
        ("GET", "/config"),
        ("GET", "/recipe-settings"),
        ("PUT", "/recipe-settings"),
        ("POST", "/jobs/distill-1/retry"),
        ("POST", "/submit"),
        ("GET", "/jobs"),
        ("GET", "/jobs/distill-1"),
        ("POST", "/jobs/distill-1/start"),
        ("GET", "/jobs/distill-1/logs"),
    ]:
        response = await http.request(
            method,
            "/integrations/data-work-flow" + path,
            headers={"X-API-Key": "wrong"},
            json={} if method in {"POST", "PUT"} else None,
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "../bad"},
        {"start": "false"},
        {"python": "/arbitrary/program"},
        {"input_path": "/etc/passwd"},
    ],
)
async def test_button_cannot_override_server_program_or_input_paths(client, body):
    http, service = client
    assert (
        await http.post("/integrations/data-work-flow/submit", json=body)
    ).status_code == 422
    service.submit.assert_not_called()


@pytest.mark.parametrize(
    "error,code",
    [
        (KeyError("missing"), 404),
        (JobConflict("busy"), 409),
        (ValueError("bad input"), 422),
        (FileNotFoundError("config"), 503),
    ],
)
async def test_polling_errors_have_distinct_http_status(client, error, code):
    http, service = client
    service.get.side_effect = error
    assert (
        await http.get("/integrations/data-work-flow/jobs/unknown")
    ).status_code == code


async def test_query_limits_and_log_stream_are_validated(client):
    http, _ = client
    for suffix in (
        "/jobs?limit=0",
        "/jobs?offset=-1",
        "/jobs/distill-1/logs?stream=../../settings.json",
        "/jobs/distill-1/logs?limit=65537",
    ):
        assert (
            await http.get("/integrations/data-work-flow" + suffix)
        ).status_code == 422


async def test_router_does_not_read_config_or_start_processes_at_registration(
    monkeypatch,
):
    from lightrag.integrations.data_workflow.service import DataWorkflowService

    monkeypatch.setattr(
        routes, "get_combined_auth_dependency", lambda *a, **k: lambda: None
    )
    service = DataWorkflowService("workspace", Path("/not-configured-yet.yaml"))
    app = FastAPI()
    app.include_router(routes.create_data_workflow_routes(service, None))
    spec = app.openapi()
    assert "/integrations/data-work-flow/submit" in spec["paths"]


async def test_batch_button_uses_canonical_service_without_directory_input(client):
    http, service = client
    response = await http.post(
        "/integrations/data-work-flow/submit",
        json={
            "source": {"type": "batch", "batch_id": "batch-1"},
            "start": True,
        },
    )
    assert response.status_code == 202
    service.submit_batch.assert_called_once_with("batch-1", True)
    service.submit.assert_not_called()


async def test_batch_association_can_be_restored_from_backend(client):
    http, service = client
    response = await http.get(
        "/integrations/data-work-flow/jobs?source_batch_id=batch-1&limit=1"
    )
    assert response.status_code == 200
    service.list.assert_called_once_with(1, 0, "batch-1")


@pytest.mark.parametrize(
    "source",
    [
        {"type": "batch", "batch_id": "../another"},
        {"type": "batch", "batch_id": "a", "path": "/etc/passwd"},
        {"type": "unknown", "batch_id": "a"},
    ],
)
async def test_batch_source_has_no_arbitrary_path_override(client, source):
    http, service = client
    assert (
        await http.post("/integrations/data-work-flow/submit", json={"source": source})
    ).status_code == 422
    service.submit_batch.assert_not_called()


@pytest.mark.parametrize(
    "settings",
    [
        {"topics": []},
        {"fallback_topic": "Other"},
        {"topic_catalog_file": "/topics.yaml"},
        {"quality_thresholds": {"grounding": 1}},
        {"max_revision_attempts": 1},
        {"expert_parallelism": 2},
    ],
)
async def test_removed_settings_are_rejected(client, settings):
    http, service = client
    result = await http.put(
        "/integrations/data-work-flow/recipe-settings",
        json={"revision": "a" * 64, "settings": settings},
    )
    assert result.status_code == 422
    service.save_recipe_settings.assert_not_called()


async def test_prompt_endpoints_use_authenticated_workspace_service(client):
    http, service = client
    service.prompt_files.return_value = {"items": []}
    service.read_prompt.return_value = {"content": "$payload"}
    service.save_prompt.return_value = {
        "path": "/config/prompts/workspace/scenario-hash.md"
    }
    service.preview_prompt.return_value = {"preview": "synthetic"}
    assert (
        await http.get("/integrations/data-work-flow/prompts?stage=scenario")
    ).status_code == 200
    service.prompt_files.assert_called_once_with("scenario")
    assert (
        await http.get(
            "/integrations/data-work-flow/prompts/qa",
            params={"path": "/config/prompts/qa.md"},
        )
    ).status_code == 200
    service.read_prompt.assert_called_once_with("qa", "/config/prompts/qa.md")
    document = {"stage": "scenario", "filename": "scenario.md", "content": "$payload"}
    assert (
        await http.post("/integrations/data-work-flow/prompts", json=document)
    ).status_code == 201
    service.save_prompt.assert_called_once_with("scenario", "scenario.md", "$payload")
    assert (
        await http.post("/integrations/data-work-flow/prompts/preview", json=document)
    ).status_code == 200
    service.preview_prompt.assert_called_once_with("scenario", "$payload")
    assert (
        await http.post(
            "/integrations/data-work-flow/prompts",
            json={**document, "stage": "revision"},
        )
    ).status_code == 422
    assert (
        await http.post(
            "/integrations/data-work-flow/prompts",
            json=document,
            headers={"X-API-Key": "wrong"},
        )
    ).status_code == 403
