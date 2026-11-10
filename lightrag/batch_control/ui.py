"""Serve the dedicated settings entry without opening any default RAG workspace."""

import json
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles


def mount_manager_ui(app, directory, *, title=None):
    directory = (
        Path(directory)
        if directory is not None
        else Path(__file__).resolve().parents[1] / "api/webui"
    )

    class ManagerFiles(StaticFiles):
        async def get_response(self, path, scope):
            if path.lower().endswith(".html") and path.lower() != "index.html":
                raise HTTPException(404, "Not Found")
            if path in {"", "."}:
                path = "index.html"
            if path == "index.html":
                source = directory / path
                if not source.is_file():
                    raise HTTPException(
                        503, "Build the full batch WebUI and configure ui_directory"
                    )
                prefix = scope.get("root_path", "").removesuffix("/webui").rstrip("/")
                payload = json.dumps(
                    {
                        "apiPrefix": prefix,
                        "webuiPrefix": prefix + "/webui/",
                        "webuiTitle": title,
                        "batchOnly": True,
                    }
                ).replace("<", "\\u003c")
                content = source.read_text().replace(
                    "<!-- __LIGHTRAG_RUNTIME_CONFIG__ -->",
                    "<script>window.__LIGHTRAG_CONFIG__=" + payload + ";</script>",
                )
                return Response(
                    content,
                    media_type="text/html",
                    headers={"Cache-Control": "no-store"},
                )
            response = await super().get_response(path, scope)
            if path.startswith("assets/"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            return response

    @app.get("/", include_in_schema=False)
    async def home(request: Request):
        return RedirectResponse(
            request.scope.get("root_path", "").rstrip("/") + "/webui/"
        )

    @app.get("/webui", include_in_schema=False)
    async def redirect_webui(request: Request):
        return RedirectResponse(
            request.scope.get("root_path", "").rstrip("/") + "/webui/"
        )

    app.mount(
        "/webui",
        ManagerFiles(directory=directory, check_dir=False),
        name="manager-webui",
    )


def mount_settings_ui(app, directory=None):
    directory = (
        Path(directory)
        if directory is not None
        else Path(__file__).resolve().parents[1] / "api" / "webui"
    )

    @app.get("/batch-settings", include_in_schema=False)
    async def redirect(request: Request):
        prefix = request.scope.get("root_path", "").rstrip("/")
        return RedirectResponse(prefix + "/batch-settings/")

    @app.get("/batch-settings/", include_in_schema=False)
    async def index(request: Request):
        path = directory / "batch-settings.html"
        if not path.is_file():
            raise HTTPException(
                503, "Build the batch-settings WebUI and configure ui_directory"
            )
        prefix = request.scope.get("root_path", "").rstrip("/")
        payload = json.dumps(
            {"apiPrefix": prefix, "webuiPrefix": prefix + "/batch-settings/"}
        ).replace("<", "\\u003c")
        html = path.read_text(encoding="utf-8").replace(
            "<!-- __LIGHTRAG_RUNTIME_CONFIG__ -->",
            "<script>window.__LIGHTRAG_CONFIG__=" + payload + ";</script>",
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount(
        "/batch-settings/assets",
        StaticFiles(directory=directory / "assets", check_dir=False),
        name="batch-settings-assets",
    )
