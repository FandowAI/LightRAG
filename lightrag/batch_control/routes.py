"""Batch-scoped headless APIs with a separate task-progress polling surface."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Annotated, Literal
from pydantic import Field

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .contracts import Configuration, Contract, Name, Group
from .settings import SettingsService
from .storage import Conflict, IntegrityError
from .task_contracts import TaskCommand


class CopyReference(Contract):
    batch: Name
    revision: Name


class CopyRequest(Contract):
    expected_revision: Name
    copies: dict[Group, CopyReference]


class ApplicationRequest(Contract):
    expected_revision: Name
    request_id: Name


class IndexRebuildRequest(Contract):
    expected_revision: Name
    embedding_profile: Name
    request_id: Name


class IndexRecoveryRequest(Contract):
    expected_version: Annotated[int, Field(gt=0)]


class CreateBatch(Contract):
    name: str
    request_id: Name
    copies: dict[Group, CopyReference] | None = None


class CreateAdoption(Contract):
    source_id: Name
    name: str
    request_id: Name


class TrainingExportRequest(Contract):
    format: Literal["messages", "alpaca", "sharegpt"] = "messages"
    selection: Literal["processed", "machine_passed"] = "processed"
    request_id: Name


class SaveConfiguration(Contract):
    expected_revision: Name
    request_id: Name
    configuration: Configuration
    copies: dict[Group, CopyReference] | None = None
    prompt_contents: dict[str, str] | None = None


def authenticated_principal(api_key):
    from lightrag.api import utils_api

    auth = utils_api.get_combined_auth_dependency(api_key, respect_whitelist=False)

    async def principal(request: Request, _=Depends(auth)):
        supplied = request.headers.get("X-API-Key", "")
        if api_key and supplied and hmac.compare_digest(supplied, api_key):
            return "api-key"
        header = request.headers.get("Authorization", "")
        if utils_api.auth_configured and header.lower().startswith("bearer "):
            token = utils_api.auth_handler.validate_token(header.split(None, 1)[1])
            if token.get("role") != "guest":
                return (
                    "user-"
                    + hashlib.sha256(token["username"].encode()).hexdigest()[:32]
                )
        raise HTTPException(
            403, "Batch control requires an authenticated non-guest principal"
        )

    return principal


def create_control_routes(deployment, principal_dependency):
    router = APIRouter(prefix="/data-batches", tags=["data-batches"])
    control = deployment.control

    @router.api_route(
        "/{batch}/api/{business_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def business(
        batch: Name,
        business_path: str,
        request: Request,
        actor=Depends(principal_dependency),
    ):
        from .http_service import business_response

        try:
            return await business_response(
                deployment, actor, batch, business_path, request
            )
        except PermissionError as exc:
            raise HTTPException(403, "Batch operation is not authorized") from exc
        except KeyError as exc:
            raise HTTPException(404, "Batch resource not found") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                503, "Batch business runtime is unavailable; inspect server logs"
            ) from exc

    async def invoke(function, *args, **kwargs):
        try:
            return await run_in_threadpool(function, *args, **kwargs)
        except PermissionError as exc:
            raise HTTPException(403, "Batch operation is not authorized") from exc
        except KeyError as exc:
            raise HTTPException(404, "Batch resource not found") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(409, "Resource integrity verification failed") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                503, "Batch storage or execution environment unavailable"
            ) from exc

    def links(task, request):
        root = request.scope.get("root_path", "").rstrip("/")
        base = f"{root}/data-batches/{task['data_batch_id']}/tasks/{task['task_id']}"
        return {
            **task,
            "links": {
                "status": base,
                "cancel": base + "/cancel",
                "result": base + "/result",
                "logs": base + "/logs",
                "events": base + "/events",
                "artifacts": base + "/artifacts",
            },
        }

    @router.post("/adoptions", status_code=202)
    async def request_adoption(
        body: CreateAdoption, actor=Depends(principal_dependency)
    ):
        service = deployment.adoption(actor)
        record = await invoke(
            service.request, body.source_id, body.name, body.request_id
        )
        return await invoke(service.public, record["operation_id"])

    @router.get("/adoptions/{operation}")
    async def adoption_status(operation: Name, actor=Depends(principal_dependency)):
        return await invoke(deployment.adoption(actor).public, operation)

    @router.post("/adoptions/{operation}/step")
    async def adoption_step(operation: Name, actor=Depends(principal_dependency)):
        service = deployment.adoption(actor)
        await invoke(service.step, operation)
        return await invoke(service.public, operation)

    @router.post("/adoptions/{operation}/abort")
    async def abort_adoption(operation: Name, actor=Depends(principal_dependency)):
        service = deployment.adoption(actor)
        await invoke(service.abort, operation)
        return await invoke(service.public, operation)

    @router.post("/adoptions/{operation}/retry-stop")
    async def retry_adoption_stop(operation: Name, actor=Depends(principal_dependency)):
        service = deployment.adoption(actor)
        await invoke(service.retry_stop, operation)
        return await invoke(service.public, operation)

    @router.post("", status_code=201)
    async def create(body: CreateBatch, actor=Depends(principal_dependency)):
        return await invoke(
            control.create,
            actor,
            body.name,
            body.request_id,
            copies={key: value.model_dump() for key, value in body.copies.items()}
            if body.copies is not None
            else None,
        )

    @router.get("")
    async def batches(actor=Depends(principal_dependency)):
        def read():
            control.allow(actor, "read", "batches")
            with control.registry.transaction() as db:
                ids = [
                    row[0]
                    for row in db.execute("SELECT id FROM batches ORDER BY rowid")
                ]
            return {
                "items": [
                    control.registry.get(batch)
                    for batch in ids
                    if control.authorize(actor, "read", batch)
                ]
            }

        return await invoke(read)

    @router.get("/status")
    async def dispatcher_status(actor=Depends(principal_dependency)):
        await invoke(control.allow, actor, "read", "batches")
        return {
            "schema_version": 1,
            "running": deployment.background is not None
            and not deployment.background.done(),
            "last_tick_at": deployment.dispatcher.last_tick_at,
            "error": deployment.dispatcher.last_error,
        }

    @router.get("/{batch}")
    async def configuration(batch: Name, actor=Depends(principal_dependency)):
        def read():
            owner, record, document, _, _ = control.read(actor, batch)
            return {
                "batch": owner,
                "configuration": document.model_dump(),
                "manifest_digest": record["digest"],
            }

        return await invoke(read)

    @router.get("/{batch}/provider-admission")
    async def provider_admission(
        batch: Name, task_id: Name | None = None, actor=Depends(principal_dependency)
    ):
        return await invoke(
            deployment.service(actor).provider_admission, batch, task_id
        )

    @router.put("/{batch}/configuration")
    async def save(
        batch: Name, body: SaveConfiguration, actor=Depends(principal_dependency)
    ):
        return await invoke(
            control.save,
            actor,
            batch,
            body.expected_revision,
            body.configuration,
            body.request_id,
            copies={key: value.model_dump() for key, value in body.copies.items()}
            if body.copies is not None
            else None,
            prompt_contents=body.prompt_contents,
        )

    @router.get("/{batch}/settings")
    async def settings(batch: Name, actor=Depends(principal_dependency)):
        return await invoke(
            SettingsService(control, deployment.policy, actor=actor).get, batch
        )

    @router.post("/{batch}/settings/preview")
    async def preview_settings(
        batch: Name, body: SaveConfiguration, actor=Depends(principal_dependency)
    ):
        return await invoke(
            SettingsService(control, deployment.policy, actor=actor).preview,
            batch,
            body.expected_revision,
            body.configuration,
            copies={key: value.model_dump() for key, value in body.copies.items()}
            if body.copies is not None
            else None,
            prompt_contents=body.prompt_contents,
        )

    @router.put("/{batch}/settings")
    async def save_settings(
        batch: Name, body: SaveConfiguration, actor=Depends(principal_dependency)
    ):
        return await invoke(
            SettingsService(control, deployment.policy, actor=actor).save,
            batch,
            body.expected_revision,
            body.configuration,
            body.request_id,
            copies={key: value.model_dump() for key, value in body.copies.items()}
            if body.copies is not None
            else None,
            prompt_contents=body.prompt_contents,
        )

    @router.post("/{batch}/settings/copy-preview")
    async def copy_preview(
        batch: Name, body: CopyRequest, actor=Depends(principal_dependency)
    ):
        return await invoke(
            control.preview,
            actor,
            batch,
            body.expected_revision,
            copies={key: value.model_dump() for key, value in body.copies.items()},
        )

    @router.post("/{batch}/index-rebuilds", status_code=202)
    async def request_index_rebuild(
        batch: Name, body: IndexRebuildRequest, actor=Depends(principal_dependency)
    ):
        service = deployment.index_rebuilds(actor)
        record = await invoke(
            service.request,
            batch,
            body.expected_revision,
            body.embedding_profile,
            body.request_id,
        )
        return await invoke(service.public, batch, record["operation_id"])

    @router.get("/{batch}/index-rebuilds")
    async def index_rebuild_history(
        batch: Name,
        limit: int = Query(default=20, ge=1, le=100),
        actor=Depends(principal_dependency),
    ):
        return await invoke(deployment.index_rebuilds(actor).list, batch, limit)

    @router.get("/{batch}/index-rebuilds/{operation}")
    async def index_rebuild_status(
        batch: Name, operation: Name, actor=Depends(principal_dependency)
    ):
        return await invoke(deployment.index_rebuilds(actor).public, batch, operation)

    @router.post("/{batch}/index-rebuilds/{operation}/cancel", status_code=202)
    async def cancel_index_rebuild(
        batch: Name, operation: Name, actor=Depends(principal_dependency)
    ):
        service = deployment.index_rebuilds(actor)
        await invoke(service.cancel, batch, operation)
        return await invoke(service.public, batch, operation)

    @router.post("/{batch}/index-rebuilds/{operation}/recover", status_code=202)
    async def recover_index_rebuild(
        batch: Name,
        operation: Name,
        body: IndexRecoveryRequest,
        actor=Depends(principal_dependency),
    ):
        service = deployment.index_rebuilds(actor)
        await invoke(service.recover, batch, operation, body.expected_version)
        return await invoke(service.public, batch, operation)

    @router.get("/{batch}/index-rebuilds/{operation}/events")
    async def index_rebuild_events(
        batch: Name, operation: Name, actor=Depends(principal_dependency)
    ):
        def read():
            control.allow(actor, "audit", batch)
            events = deployment.index_rebuilds(actor).store.events(batch, operation)
            return {
                "items": [
                    {
                        key: value.get(key)
                        for key in (
                            "event",
                            "version",
                            "state",
                            "updated_at",
                            "error",
                            "rollback_error",
                            "waiting_reason",
                            "progress",
                        )
                    }
                    for value in events
                ]
            }

        return await invoke(read)

    @router.get("/{batch}/applications/{operation}")
    async def application_status(
        batch: Name, operation: Name, actor=Depends(principal_dependency)
    ):
        def read():
            control.allow(actor, "read", batch)
            return deployment.application(actor).store.get(batch, operation)

        return await invoke(read)

    @router.post("/{batch}/applications/{operation}/start", status_code=202)
    async def application_start(
        batch: Name,
        operation: Name,
        body: ApplicationRequest,
        actor=Depends(principal_dependency),
    ):
        return await invoke(
            deployment.application(actor).request,
            batch,
            operation,
            body.expected_revision,
            body.request_id,
        )

    @router.post("/{batch}/applications/{operation}/discard")
    async def application_discard(
        batch: Name, operation: Name, actor=Depends(principal_dependency)
    ):
        return await invoke(deployment.application(actor).discard, batch, operation)

    @router.get("/{batch}/applications/{operation}/events")
    async def application_events(
        batch: Name, operation: Name, actor=Depends(principal_dependency)
    ):
        def read():
            control.allow(actor, "audit", batch)
            control.operation(actor, batch, operation)
            return {
                "items": deployment.application(actor).store.events(batch, operation)
            }

        return await invoke(read)

    @router.get("/{batch}/runtime")
    async def runtime_status(batch: Name, actor=Depends(principal_dependency)):
        def read():
            service = deployment.service(actor)
            lease = service.runtime.inspect(batch)
            record = service.runtime.store.get(batch)
            return {
                "state": record["state"] if record else "unprovisioned",
                "lease": lease.model_dump() if lease else None,
            }

        return await invoke(read)

    @router.post("/{batch}/runtime/start")
    async def runtime_start(batch: Name, actor=Depends(principal_dependency)):
        def start():
            owner, *_ = control.read(actor, batch)
            return (
                deployment.service(actor)
                .runtime.start(batch, owner["active"])
                .model_dump()
            )

        return await invoke(start)

    @router.post("/{batch}/runtime/stop")
    async def runtime_stop(batch: Name, actor=Depends(principal_dependency)):
        def stop():
            record = deployment.service(actor).runtime.stop(batch)
            return {"state": record["state"] if record else "unprovisioned"}

        return await invoke(stop)

    @router.post("/{batch}/tasks", status_code=202)
    async def submit(
        batch: Name,
        body: TaskCommand,
        request: Request,
        actor=Depends(principal_dependency),
    ):
        return links(
            await invoke(deployment.service(actor).submit, batch, body), request
        )

    @router.get("/{batch}/tasks")
    async def tasks(batch: Name, request: Request, actor=Depends(principal_dependency)):
        records = await invoke(deployment.service(actor).list, batch)
        return {"items": [links(task, request) for task in records]}

    @router.get("/{batch}/tasks/{task}")
    async def status(
        batch: Name, task: Name, request: Request, actor=Depends(principal_dependency)
    ):
        return links(await invoke(deployment.service(actor).get, batch, task), request)

    @router.post("/{batch}/tasks/{task}/cancel")
    async def cancel(
        batch: Name, task: Name, request: Request, actor=Depends(principal_dependency)
    ):
        return links(
            await invoke(deployment.service(actor).cancel, batch, task), request
        )

    @router.post("/{batch}/tasks/{task}/retry", status_code=202)
    async def retry_task(
        batch: Name,
        task: Name,
        body: ApplicationRequest,
        request: Request,
        actor=Depends(principal_dependency),
    ):
        return links(
            await invoke(
                deployment.service(actor).retry,
                batch,
                task,
                body.expected_revision,
                body.request_id,
            ),
            request,
        )

    @router.get("/{batch}/tasks/{task}/result")
    async def result(batch: Name, task: Name, actor=Depends(principal_dependency)):
        path = await invoke(deployment.service(actor).result_path, batch, task)
        return FileResponse(path, media_type="application/json", filename="result.json")

    @router.get("/{batch}/tasks/{task}/events")
    async def events(batch: Name, task: Name, actor=Depends(principal_dependency)):
        def read():
            control.allow(actor, "audit", batch)
            return {"items": deployment.service(actor).tasks.events(batch, task)}

        return await invoke(read)

    @router.post("/{batch}/tasks/{task}/training-exports", status_code=202)
    async def create_training_export(
        batch: Name,
        task: Name,
        body: TrainingExportRequest,
        actor=Depends(principal_dependency),
    ):
        service = deployment.training_exports(actor)
        record = await invoke(
            service.request, batch, task, body.format, body.selection, body.request_id
        )
        if record["state"] == "queued":
            deployment.launch_export(record)
        return await invoke(service.status, batch, task, record["export_id"])

    @router.get("/{batch}/tasks/{task}/training-exports")
    async def training_exports(
        batch: Name, task: Name, actor=Depends(principal_dependency)
    ):
        return await invoke(deployment.training_exports(actor).list, batch, task)

    @router.get("/{batch}/tasks/{task}/training-exports/{export}")
    async def training_export_status(
        batch: Name, task: Name, export: Name, actor=Depends(principal_dependency)
    ):
        return await invoke(
            deployment.training_exports(actor).status, batch, task, export
        )

    @router.get("/{batch}/tasks/{task}/training-exports/{export}/files/{artifact}")
    async def training_export_file(
        batch: Name,
        task: Name,
        export: Name,
        artifact: Literal["dataset", "provenance", "manifest", "log"],
        actor=Depends(principal_dependency),
    ):
        path = await invoke(
            deployment.training_exports(actor).artifact, batch, task, export, artifact
        )
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/json"
            if artifact == "manifest"
            else "text/plain"
            if artifact == "log"
            else "application/x-ndjson",
        )

    @router.get("/{batch}/tasks/{task}/artifacts")
    async def artifact_catalog(
        batch: Name, task: Name, actor=Depends(principal_dependency)
    ):
        return await invoke(deployment.service(actor).artifacts, batch, task)

    @router.get("/{batch}/tasks/{task}/artifacts/{artifact}")
    async def artifact_file(
        batch: Name,
        task: Name,
        artifact: Literal[
            "retrieval-packages",
            "retrieval-sources",
            "retrieval-manifest",
            "workflow-input",
            "workflow-review",
            "workflow-run",
        ],
        actor=Depends(principal_dependency),
    ):
        path = await invoke(
            deployment.service(actor).artifact_path, batch, task, artifact
        )
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/json"
            if path.suffix == ".json"
            else "application/x-ndjson",
        )

    @router.get("/{batch}/tasks/{task}/logs")
    async def logs(
        batch: Name,
        task: Name,
        stream: str = Query(default="stderr", pattern="^(stdout|stderr|worker)$"),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=65536, ge=1, le=131072),
        actor=Depends(principal_dependency),
    ):
        def read():
            service = deployment.service(actor)
            service.get(batch, task)
            owner = control.registry.get(batch)
            if stream == "worker":
                control.allow(actor, "audit", batch)
                generation = service.tasks.get(batch, task).get("generation")
                path = control.paths.under(
                    control.paths.runtime_root,
                    "batch-runtime",
                    owner["namespace"],
                    "generation-" + str(generation),
                    "worker.log",
                )
            else:
                path = control.paths.under(
                    control.paths.runtime_root,
                    "tasks",
                    owner["namespace"],
                    task,
                    "workflow",
                    stream + ".log",
                )
            if path != path.resolve():
                raise IntegrityError("Log path was redirected")
            if not path.exists():
                return {
                    "text": "",
                    "offset": offset,
                    "next_offset": offset,
                    "has_more": False,
                }
            with path.open("rb") as handle:
                handle.seek(offset)
                raw = handle.read(limit)
                more = bool(handle.read(1))
            return {
                "text": raw.decode("utf-8", errors="replace"),
                "offset": offset,
                "next_offset": offset + len(raw),
                "has_more": more,
            }

        return await invoke(read)

    return router


def install_batch_control(app, api_key):
    filename = os.getenv("LIGHTRAG_BATCH_CONTROL_CONFIG", "").strip()
    if not filename:
        return
    from .deployment import load_deployment

    deployment = load_deployment(Path(filename).absolute())
    from .ui import mount_settings_ui

    mount_settings_ui(app, deployment.ui_directory)
    app.state.batch_deployment = deployment
    app.include_router(
        create_control_routes(deployment, authenticated_principal(api_key))
    )
