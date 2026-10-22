"""Authenticated batch evidence jobs; ordinary query routes stay unchanged."""

import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.batch_retrieval import BatchConflict, BatchManager, ModelUnavailable
from lightrag.entity_packages import PackageSettings


class BatchRequest(BaseModel):
    model_config = {"extra": "forbid"}
    centers: list[str] = Field(min_length=1, max_length=500)
    graph_depth: int = Field(default=2, ge=0, le=5)
    rag_top_k: int = Field(default=10, ge=1, le=100)

    @field_validator("centers")
    @classmethod
    def valid_centers(cls, value):
        if any(not name.strip() or len(name) > 512 for name in value):
            raise ValueError("Center names must be nonempty and at most 512 characters")
        return list(dict.fromkeys(name.strip() for name in value))


def create_batch_routes(rag, api_key=None):
    router = APIRouter(dependencies=[Depends(get_combined_auth_dependency(api_key))])
    manager = None

    def jobs():
        nonlocal manager
        if manager is None:
            manager = BatchManager(rag)
        return manager

    def get_job(job_id):
        try:
            return jobs().get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Batch not found")

    @router.get("/batch-retrieval/config")
    async def config():
        return {
            "distillation_configured": bool(
                os.getenv("DISTILLATION_INGEST_URL", "").strip()
            ),
            "retrieval_adapter": "graph_mode_dfs_mix_v1",
        }

    @router.post("/batch-retrieval/jobs", status_code=202)
    async def start(request: BatchRequest):
        try:
            return await jobs().start(
                request.centers,
                PackageSettings(
                    graph_depth=request.graph_depth, rag_top_k=request.rag_top_k
                ),
            )
        except BatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="embedding_unavailable"
            ) from exc

    @router.get("/batch-retrieval/jobs/{job_id}")
    async def status(job_id: str):
        return get_job(job_id)

    @router.post("/batch-retrieval/jobs/{job_id}/stop")
    async def stop(job_id: str):
        get_job(job_id)
        return jobs().cancel(job_id)

    @router.post("/batch-retrieval/jobs/{job_id}/resume", status_code=202)
    async def resume(job_id: str):
        get_job(job_id)
        try:
            return await jobs().resume(job_id)
        except BatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/batch-retrieval/jobs/{job_id}/rebuild", status_code=202)
    async def rebuild(job_id: str):
        previous = get_job(job_id)
        try:
            return await jobs().start(
                [item["center"] for item in previous["items"]],
                PackageSettings(**previous["settings"]),
                reuse_from=job_id,
            )
        except BatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def artifact(job_id, filename):
        job = get_job(job_id)
        if job["status"] in {"queued", "running"}:
            raise HTTPException(
                status_code=409, detail="Wait for the batch to finish before exporting"
            )
        folder = jobs().root / job_id
        if not job.get("export_path") or not (folder / "manifest.json").is_file():
            raise HTTPException(
                status_code=409,
                detail="No source-verified export is published for this batch",
            )
        path = folder / filename
        if not path.is_file():
            raise HTTPException(
                status_code=409, detail="Published export is incomplete"
            )
        return FileResponse(
            path,
            media_type="application/json"
            if filename.endswith(".json")
            else "application/x-ndjson",
            filename=filename,
        )

    @router.get("/batch-retrieval/jobs/{job_id}/packages.jsonl")
    async def export(job_id: str):
        return artifact(job_id, "packages.jsonl")

    @router.get("/batch-retrieval/jobs/{job_id}/sources.jsonl")
    async def export_sources(job_id: str):
        return artifact(job_id, "sources.jsonl")

    @router.get("/batch-retrieval/jobs/{job_id}/manifest.json")
    async def export_manifest(job_id: str):
        return artifact(job_id, "manifest.json")

    @router.post("/batch-retrieval/jobs/{job_id}/distill")
    async def distill(job_id: str):
        get_job(job_id)
        if not os.getenv("DISTILLATION_INGEST_URL", "").strip():
            raise HTTPException(
                status_code=503, detail="Distillation endpoint is not configured"
            )
        try:
            return await jobs().handoff(job_id)
        except BatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="Distillation system did not confirm receipt; packages are retained",
            ) from exc

    return router
