"""Authenticated settings and dry-run API for name-only graph resolution."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lightrag.api import utils_api
from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.entity_merge import (
    EntityMergeManager,
    EntityMergeSettings,
    StaleEntityMergeRevision,
)


class SettingsPayload(BaseModel):
    enabled: bool = True
    similarity_threshold: float = Field(ge=0, le=1)
    margin_threshold: float = Field(ge=0, le=1)
    top_k: int = Field(ge=1, le=100)


class SaveRequest(BaseModel):
    revision: int = Field(ge=0)
    settings: SettingsPayload


class PreviewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    entity_type: str = Field(min_length=1, max_length=128)


def create_entity_merge_routes(
    manager: EntityMergeManager, api_key: str | None, workers: int
) -> APIRouter:
    router = APIRouter(prefix="/entity-merge", tags=["entity-merge"])
    auth = get_combined_auth_dependency(api_key, respect_whitelist=False)

    @router.get("", dependencies=[Depends(auth)])
    def snapshot() -> dict:
        result = manager.snapshot()
        result["write_enabled"] = workers == 1 and bool(
            utils_api.auth_configured or api_key
        )
        return result

    @router.put("", dependencies=[Depends(auth)])
    def save(body: SaveRequest) -> dict:
        if not (utils_api.auth_configured or api_key):
            raise HTTPException(
                403, "Configure authentication before changing graph settings"
            )
        if workers != 1:
            raise HTTPException(
                409, "Graph settings editing requires one server worker"
            )
        try:
            result = manager.save(
                body.revision, EntityMergeSettings(**body.settings.model_dump())
            )
            result["write_enabled"] = True
            return result
        except StaleEntityMergeRevision as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/preview", dependencies=[Depends(auth)])
    async def preview(body: PreviewRequest) -> dict:
        try:
            return await manager.preview(body.name, body.entity_type)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
