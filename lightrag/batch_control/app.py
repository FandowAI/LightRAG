"""Standalone control-plane app; no default-workspace RAG is constructed."""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI

from .deployment import load_deployment
from .routes import authenticated_principal, create_control_routes
from .ui import mount_settings_ui


def create_manager_app(
    config_file: Path, *, api_key: str | None = None, ui_directory: Path | None = None
):
    from lightrag.api import utils_api
    from lightrag.api.config import get_config, normalize_api_prefix

    key = api_key or os.getenv("LIGHTRAG_API_KEY")
    if not key and not utils_api.auth_configured:
        raise ValueError(
            "Configure LIGHTRAG_API_KEY or authenticated accounts for the batch manager"
        )
    deployment = load_deployment(config_file.absolute())

    @asynccontextmanager
    async def lifespan(app):
        await deployment.start()
        try:
            yield
        finally:
            await deployment.close()

    app = FastAPI(
        title="LightRAG batch manager",
        lifespan=lifespan,
        root_path=normalize_api_prefix(getattr(get_config(), "api_prefix", None)),
    )
    app.state.batch_deployment = deployment
    app.include_router(create_control_routes(deployment, authenticated_principal(key)))
    mount_settings_ui(app, ui_directory or deployment.ui_directory)
    from .shell import mount_manager_shell

    mount_manager_shell(app, key, ui_directory or deployment.ui_directory)
    return app
