"""Directory browsing and server-local import through the existing upload ingress."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from starlette.concurrency import run_in_threadpool

from lightrag.api.server_files import ROOTS_ENV, ServerFiles
from lightrag.api.utils_api import get_combined_auth_dependency


class DirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class PreviewRequest(DirectoryRequest):
    recursive: StrictBool = True
    extensions: list[Annotated[str, Field(pattern=r"^\.[a-z0-9]+$")]] | None = Field(
        default=None, min_length=1, max_length=32
    )

    @field_validator("extensions", mode="before")
    @classmethod
    def normalize_extensions(cls, value):
        if isinstance(value, list):
            return [item.lower() if isinstance(item, str) else item for item in value]
        return value


class ImportRequest(DirectoryRequest):
    version: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def create_server_import_routes(
    doc_manager, upload, maximum, tasks_dependency, api_key=None
):
    router = APIRouter(
        prefix="/server-import",
        dependencies=[
            Depends(get_combined_auth_dependency(api_key, respect_whitelist=False))
        ],
    )

    @router.get("/config")
    def config():
        files = ServerFiles()
        roots = [str(root) for root in files.roots]
        return {
            "enabled": bool(roots),
            "roots": roots,
            "configuration_variable": ROOTS_ENV,
            "limits": {"max_files": files.max_files, "max_entries": files.max_entries},
        }

    @router.post("/browse")
    def browse(body: DirectoryRequest):
        return ServerFiles().browse(body.path)

    @router.post("/preview")
    def preview(body: PreviewRequest):
        return ServerFiles().preview(
            body.path,
            body.recursive,
            doc_manager.is_supported_file,
            maximum(),
            set(body.extensions) if body.extensions is not None else None,
        )

    @router.post("/file")
    async def import_file(
        body: ImportRequest,
        request: Request,
        managed_tasks: set = Depends(tasks_dependency),
    ):
        try:
            supported = doc_manager.is_supported_file(Path(body.path).name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not supported:
            raise HTTPException(400, "Unsupported source file type")
        snapshot, size, relative = await run_in_threadpool(
            ServerFiles().snapshot, body.path, body.version, maximum()
        )
        file = UploadFile(
            filename=body.path.rsplit("/", 1)[-1], file=snapshot, size=size
        )
        try:
            return await upload(
                managed_tasks=managed_tasks,
                file=file,
                http_request=request,
                relative_path=relative,
            )
        finally:
            await file.close()

    return router
