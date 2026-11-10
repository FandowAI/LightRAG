"""Authenticated streaming access to business routes in the selected batch process."""

import asyncio
import time
import anyio

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .http_store import HttpStore, TERMINAL, business_action
from .storage import Conflict


async def business_response(deployment, actor, batch, path, request):
    control = deployment.control
    path = "/" + path
    action = business_action(request.method, path)
    control.allow(actor, "read", batch)
    control.allow(actor, action, batch)
    if request.method not in {"GET", "HEAD"} and action == "edit":
        control.allow(actor, "submit", batch)
    control.allow(actor, "run_runtime", batch)
    owner, _, document, _, _ = await run_in_threadpool(control.read, actor, batch)
    if owner["pending"]:
        raise Conflict("Batch configuration is pending application")
    limit = document.bridge.max_input_bytes + 1024 * 1024
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(
                413, "Batch request exceeds the configured input size limit"
            )
        body.extend(chunk)
    runtime = deployment.service(actor).runtime
    lease = await run_in_threadpool(runtime.start, batch, owner["active"])
    store = HttpStore(control.registry, control.paths)
    record = await run_in_threadpool(
        store.submit,
        actor,
        batch,
        lease,
        request.method,
        path,
        request.scope["query_string"].decode("latin1"),
        [
            [key, value]
            for key, value in request.headers.items()
            if key.lower()
            in {"content-type", "accept", "range", "if-none-match", "if-modified-since"}
        ],
        bytes(body),
    )
    identity = record["id"]
    deadline = time.monotonic() + 600

    async def cancel_owned():
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(store.cancel, batch, identity)

    async def current():
        nonlocal record
        record = await run_in_threadpool(store.get, batch, identity)
        if await request.is_disconnected():
            await cancel_owned()
            raise asyncio.CancelledError()
        if record["state"] == "completed":
            return record
        live = await run_in_threadpool(runtime.store.get, batch)
        ownership = (
            await run_in_threadpool(runtime.backend.ownership, live) if live else "gone"
        )
        if (
            not live
            or live.get("generation") != lease.generation
            or ownership in {"gone", "reused", "foreign"}
        ):
            await run_in_threadpool(
                store.update,
                batch,
                identity,
                lease.generation,
                state="failed",
                error="Owning runtime exited; no request replayed",
            )
            raise HTTPException(503, "Batch runtime exited; request was not replayed")
        if time.monotonic() > deadline:
            await run_in_threadpool(store.cancel, batch, identity)
            raise HTTPException(
                504, "Batch request timed out; inspect its task progress"
            )
        return record

    try:
        while (await current())["status"] is None:
            if record["state"] in TERMINAL:
                raise HTTPException(
                    503, record.get("error") or "Batch request interrupted"
                )
            await asyncio.sleep(0.03)
    except BaseException:
        await cancel_owned()
        raise

    async def content():
        offset = 0
        response = store.directory(batch, identity) / "response.bin"
        try:
            while True:
                state = await current()
                available = state["bytes_written"] - offset
                if available:
                    if response.is_symlink():
                        raise RuntimeError("Batch response file must not be a symlink")
                    with response.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read(min(available, 1024 * 1024))
                    if not chunk:
                        raise RuntimeError("Batch response was truncated")
                    offset += len(chunk)
                    yield chunk
                    continue
                if state["state"] == "completed":
                    break
                if state["state"] in TERMINAL:
                    raise RuntimeError(state.get("error") or "Batch stream interrupted")
                await asyncio.sleep(0.03)
        finally:
            with anyio.CancelScope(shield=True):
                latest = await run_in_threadpool(store.get, batch, identity)
                if latest["state"] not in TERMINAL:
                    await cancel_owned()

    return StreamingResponse(
        content(), status_code=record["status"], headers=dict(record["headers"])
    )
