"""Full manager UI/auth endpoints that never initialize an unowned default RAG."""

import os
from pathlib import Path

from lightrag._version import __version__, __api_version__
from lightrag.api.config import get_config
from lightrag.api.routers.session_routes import mount_session_routes
from lightrag.api.routers.ui_customization_routes import create_ui_customization_routes
from lightrag.api import utils_api

from .ui import mount_manager_ui


def mount_manager_shell(app, api_key, directory):
    args = get_config()
    metadata = {
        "core_version": __version__,
        "api_version": __api_version__,
        "webui_title": os.getenv("WEBUI_TITLE"),
        "webui_description": os.getenv("WEBUI_DESCRIPTION"),
        "ai_content_notice_enabled": os.getenv(
            "ENABLE_AI_CONTENT_NOTICE", "false"
        ).lower()
        == "true",
    }
    mount_session_routes(
        app,
        handler=utils_api.auth_handler,
        args=args,
        metadata=metadata,
        api_key=api_key,
    )
    app.include_router(
        create_ui_customization_routes(
            None, metadata["webui_title"], metadata["webui_description"]
        )
    )
    mount_manager_ui(app, directory, title=metadata["webui_title"])
    build_directory = (
        Path(directory)
        if directory is not None
        else Path(__file__).resolve().parents[1] / "api/webui"
    )

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "batch_only": True,
            "webui_available": (build_directory / "index.html").is_file(),
            "workspace_available": False,
            "api_docs_available": True,
            "pipeline_busy": False,
            "pipeline_active": False,
            "auth_mode": "enabled" if utils_api.auth_configured else "disabled",
            **metadata,
        }
