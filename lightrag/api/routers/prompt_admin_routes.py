"""Optional prompt-editor API, isolated from upstream document/query routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from lightrag.api import utils_api
from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.prompt_admin import PromptAdmin, PromptEditorConflict


class DraftRequest(BaseModel):
    revision: int = Field(ge=0)
    fields: dict[str, Any]


class RevisionRequest(BaseModel):
    revision: int = Field(ge=0)


class ResetRequest(RevisionRequest):
    language: Literal["zh", "en"] = "zh"


def create_prompt_admin_routes(manager: PromptAdmin, api_key: str | None) -> APIRouter:
    router = APIRouter(prefix="/prompt-admin", tags=["prompt-admin"])
    auth = get_combined_auth_dependency(api_key, respect_whitelist=False)

    def writable() -> None:
        if not (utils_api.auth_configured or api_key):
            raise HTTPException(403, "Configure AUTH_ACCOUNTS or LIGHTRAG_API_KEY before editing prompts")
        if not manager.write_enabled:
            raise HTTPException(409, "Prompt editing requires one server worker")

    def actor(request: Request) -> str:
        bearer = request.headers.get("Authorization", "")
        if bearer.lower().startswith("bearer "):
            try:
                token_info = utils_api.auth_handler.validate_token(bearer[7:])
                if token_info.get("role") != "guest":
                    return str(token_info.get("username") or "user")
            except HTTPException:
                pass
        return "api-key" if api_key else "unknown"

    @router.get("", dependencies=[Depends(auth)])
    def snapshot() -> dict[str, Any]:
        data = manager.snapshot()
        data["write_enabled"] = bool(data["write_enabled"] and (utils_api.auth_configured or api_key))
        return data

    @router.get("/{group}/history", dependencies=[Depends(auth)])
    def history(group: str) -> list[dict[str, Any]]:
        if not (utils_api.auth_configured or api_key):
            raise HTTPException(403, "Configure authentication before viewing prompt history")
        try:
            return manager.history(group)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.put("/{group}/draft", dependencies=[Depends(auth)])
    def save_draft(group: str, body: DraftRequest, request: Request) -> dict[str, Any]:
        writable()
        try:
            return manager.save_draft(group, body.fields, body.revision, actor(request))
        except PromptEditorConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/{group}/publish", dependencies=[Depends(auth)])
    def publish(group: str, body: RevisionRequest, request: Request) -> dict[str, Any]:
        writable()
        try:
            return manager.publish(group, body.revision, actor(request))
        except PromptEditorConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/{group}/reset", dependencies=[Depends(auth)])
    def reset(group: str, body: ResetRequest, request: Request) -> dict[str, Any]:
        writable()
        try:
            return manager.reset(group, body.revision, actor(request), body.language)
        except PromptEditorConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
