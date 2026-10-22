"""Headless start action and polling API for an independently placed progress UI."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.integrations.data_workflow.contracts import (
    JobListResponse,
    JobResponse,
    LogResponse,
)
from lightrag.integrations.data_workflow.service import DataWorkflowService
from lightrag.integrations.data_workflow.storage import JobConflict


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
        description="Reuse this ID when retrying a button click; a new ID creates a new job.",
    )
    start: StrictBool = Field(
        default=True,
        description="Scan, convert and start the workflow. False only prepares fixed input files.",
    )


def create_data_workflow_routes(
    service: DataWorkflowService, api_key: str | None
) -> APIRouter:
    router = APIRouter(prefix="/integrations/data-work-flow", tags=["data-work-flow"])
    auth = get_combined_auth_dependency(api_key, respect_whitelist=False)

    def invoke(action):
        try:
            return action()
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except JobConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                503, f"Data workflow configuration or storage unavailable: {exc}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def links(job: dict, request: Request) -> dict:
        prefix = request.scope.get("root_path", "").rstrip("/")
        base = f"{prefix}/integrations/data-work-flow/jobs/{job['job_id']}"
        return {
            **job,
            "links": {"status": base, "start": f"{base}/start", "logs": f"{base}/logs"},
        }

    @router.get("/config", dependencies=[Depends(auth)])
    def configuration():
        return invoke(service.configuration)

    @router.post(
        "/submit",
        status_code=202,
        response_model=JobResponse,
        dependencies=[Depends(auth)],
    )
    def submit(body: SubmitRequest, request: Request):
        return links(
            invoke(lambda: service.submit(body.request_id, body.start)), request
        )

    @router.get("/jobs", response_model=JobListResponse, dependencies=[Depends(auth)])
    def jobs(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        result = invoke(lambda: service.list(limit, offset))
        result["items"] = [links(job, request) for job in result["items"]]
        return result

    @router.get(
        "/jobs/{job_id}", response_model=JobResponse, dependencies=[Depends(auth)]
    )
    def job(job_id: str, request: Request):
        return links(invoke(lambda: service.get(job_id)), request)

    @router.post(
        "/jobs/{job_id}/start",
        status_code=202,
        response_model=JobResponse,
        dependencies=[Depends(auth)],
    )
    def start(job_id: str, request: Request):
        return links(invoke(lambda: service.start(job_id)), request)

    @router.get(
        "/jobs/{job_id}/logs", response_model=LogResponse, dependencies=[Depends(auth)]
    )
    def logs(
        job_id: str,
        stream: Literal["stdout", "stderr"] = "stderr",
        offset: int = Query(0, ge=0),
        limit: int = Query(8192, ge=4, le=65536),
    ):
        return invoke(lambda: service.logs(job_id, stream, offset, limit))

    return router
