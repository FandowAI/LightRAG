"""The full manager serves auth/UI without opening a default storage writer."""

# ruff: noqa: F811

import sys

from httpx import ASGITransport, AsyncClient
import pytest

from lightrag.batch_control.deployment import BatchDeployment
from tests.batch_control.test_runtime import runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.fixture
def manager(runtime_setup, tmp_path, monkeypatch):
    from lightrag.api import config

    monkeypatch.setattr(sys, "argv", ["lightrag-server"])
    monkeypatch.setattr(config, "_global_args", None)
    monkeypatch.setattr(config, "_initialized", False)
    monkeypatch.setenv("LIGHTRAG_API_PREFIX", "/proxy")
    monkeypatch.setenv("AUTH_ACCOUNTS", "admin:test-password")
    monkeypatch.setenv("TOKEN_SECRET", "manager-test-signing-key-not-production")
    monkeypatch.setenv("WEBUI_TITLE", "Batch manager </script>")
    config.initialize_config(force=True)
    from lightrag.api import utils_api

    monkeypatch.setattr(utils_api, "auth_configured", True)
    monkeypatch.setattr(utils_api.auth_handler, "accounts", {"admin": "test-password"})
    monkeypatch.setattr(
        utils_api.auth_handler, "secret", "manager-test-signing-key-not-production"
    )
    control, policy = runtime_setup
    deployment = BatchDeployment(control, policy)
    monkeypatch.setattr(
        "lightrag.batch_control.app.load_deployment", lambda path: deployment
    )
    from lightrag.batch_control.app import create_manager_app

    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text(
        '<html><!-- __LIGHTRAG_RUNTIME_CONFIG__ --><script src="./assets/full.js"></script></html>'
    )
    (ui / "workspace.html").write_text("legacy workspace entry")
    (ui / "assets/full.js").write_text("export const batchOnly = true")
    app = create_manager_app(
        tmp_path / "manager.yaml", api_key="test-api-key", ui_directory=ui
    )
    return app, control, ui


async def test_full_manager_entry_prefix_health_and_static_assets(manager):
    app, _, _ = manager
    assert not hasattr(app.state, "rag")
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test/proxy"
    ) as client:
        assert (await client.get("/")).headers["location"] == "/proxy/webui/"
        assert (await client.get("/webui")).headers["location"] == "/proxy/webui/"
        html = await client.get("/webui/")
        assert html.status_code == 200
        assert '"apiPrefix": "/proxy"' in html.text
        assert '"batchOnly": true' in html.text
        assert "Batch manager </script>" not in html.text
        assert "Batch manager \\u003c/script>" in html.text
        assert html.headers["cache-control"] == "no-store"
        asset = await client.get("/webui/assets/full.js")
        assert asset.status_code == 200
        assert "immutable" in asset.headers["cache-control"]
        assert (await client.get("/webui/workspace.html")).status_code == 404
        assert (await client.get("/webui/../manager.yaml")).status_code == 404
        health = (await client.get("/health")).json()
        assert health["batch_only"] is True and health["webui_available"] is True
        assert health["workspace_available"] is False
        assert "configuration" not in health
        assert (await client.get("/ui/customization")).json()["customized"] is False
        assert (await client.get("/documents")).status_code == 404


async def test_manager_login_and_grants_share_normal_server_credentials(manager):
    app, control, _ = manager
    import hashlib

    actor = "user-" + hashlib.sha256(b"admin").hexdigest()[:32]
    control.authorize = lambda principal, action, resource: principal == actor
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test/proxy"
    ) as client:
        status = (await client.get("/auth-status")).json()
        assert status["auth_configured"] is True and "access_token" not in status
        assert (await client.get("/auth/verify")).status_code == 401
        assert (
            await client.post("/login", data={"username": "admin", "password": "wrong"})
        ).status_code == 401
        login = await client.post(
            "/login", data={"username": "admin", "password": "test-password"}
        )
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        assert (await client.get("/auth/verify", headers=headers)).status_code == 200
        assert (await client.get("/data-batches", headers=headers)).status_code == 200
        assert (
            await client.get("/data-batches", headers={"X-API-Key": "test-api-key"})
        ).status_code == 403
        assert not hasattr(app.state, "rag")


async def test_missing_full_build_is_explicit_not_a_legacy_fallback(manager):
    app, _, ui = manager
    (ui / "index.html").unlink()
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test/proxy"
    ) as client:
        assert (await client.get("/webui/")).status_code == 503
        assert (await client.get("/health")).json()["webui_available"] is False
