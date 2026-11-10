"""Execute authorized mailbox requests inside their actual batch ASGI application."""

import asyncio
import json
import os
import hashlib
import sqlite3
from pathlib import Path

from .http_store import HttpStore, business_action
from .paths import ControlPaths
from .runtime_process import process_identity
from .storage import Registry
from .resources import digest


async def business_activity(app):
    from lightrag.kg.shared_storage import get_namespace_data

    rag = app.state.rag
    pipeline = await get_namespace_data("pipeline_status", workspace=rag.workspace)
    drives = getattr(rag, "_admin_release_drives", lambda: ())()
    fence = getattr(app.state, "legacy_fence", None)
    result = {
        "deferred_admin": any(not task.done() for task in drives),
        "snapshot": bool(fence and fence.snapshot_active),
        "requests": bool(fence and fence.active),
        "pipeline": any(
            pipeline.get(key) for key in ("busy", "scanning", "pending_enqueues")
        ),
        "background": any(
            not task.done() for task in getattr(app.state, "background_tasks", set())
        ),
        "retrieval": False,
        "workflow": False,
    }
    root = os.getenv("LIGHTRAG_BATCH_OUTPUT_DIR", "")
    directory = (
        Path(root) / rag.workspace
        if root
        else Path(rag.working_dir) / rag.workspace / "batch_retrieval"
    )
    database = directory / "jobs.sqlite3"
    if database.is_file():
        with sqlite3.connect(
            database.as_uri() + "?mode=ro", uri=True, timeout=15
        ) as db:
            result["retrieval"] = (
                db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
                is not None
            )
    service = getattr(app.state, "data_workflow_service", None)
    if service is not None:
        from lightrag.integrations.data_workflow.progress import reconcile

        _, store, _ = service._resources()
        from lightrag.integrations.data_workflow.storage import ACTIVE

        result["workflow"] = any(
            reconcile(store, record)["status"] in ACTIVE for record in store.active()
        )
    result["busy"] = any(result.values())
    return result


class HttpRunner:
    def __init__(self, app, spec):
        self.app = app
        self.spec = {
            **spec,
            "pid": os.getpid(),
            "process_identity": process_identity(os.getpid()),
        }
        paths = ControlPaths(
            **{key: Path(value) for key, value in spec["control_paths"].items()}
        )
        self.store = HttpStore(Registry(Path(spec["registry"])), paths)
        self.running = {}

    async def execute(self, record):
        batch, identity = record["batch"], record["id"]
        directory = self.store.directory(batch, identity)
        try:
            request_file = directory / "request.json"
            body_file = directory / "request.bin"
            if request_file.is_symlink() or body_file.is_symlink():
                raise ValueError("HTTP request files must not be symlinks")
            request = json.loads(request_file.read_text())
            business_action(request["method"], request["path"])
            body = body_file.read_bytes()
            if (
                digest({**request, "body_sha256": hashlib.sha256(body).hexdigest()})
                != record["request_digest"]
            ):
                raise ValueError("HTTP request changed after authorization")
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                # A stream needs a live receiver until client cancellation or exit.
                await asyncio.Event().wait()

            response_file = directory / "response.bin"
            with response_file.open("xb", buffering=0) as output:
                response_file.chmod(0o600)

                async def send(message):
                    if self.store.get(batch, identity)["state"] == "cancelled":
                        raise asyncio.CancelledError()
                    if message["type"] == "http.response.start":
                        allowed = {
                            b"content-type",
                            b"content-disposition",
                            b"cache-control",
                            b"etag",
                            b"last-modified",
                            b"accept-ranges",
                            b"content-range",
                        }
                        self.store.update(
                            batch,
                            identity,
                            record["generation"],
                            status=message["status"],
                            headers=[
                                [key.decode("latin1"), value.decode("latin1")]
                                for key, value in message["headers"]
                                if key.lower() in allowed
                            ],
                        )
                    elif message["type"] == "http.response.body":
                        output.write(message.get("body", b""))
                        self.store.update(
                            batch,
                            identity,
                            record["generation"],
                            bytes_written=output.tell(),
                        )

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1",
                    "method": request["method"],
                    "scheme": "http",
                    "path": request["path"],
                    "raw_path": request["path"].encode(),
                    "query_string": request["query"].encode("latin1"),
                    "root_path": "",
                    "headers": [
                        (key.lower().encode("latin1"), value.encode("latin1"))
                        for key, value in request["headers"]
                        if key.lower() not in {"authorization", "x-api-key"}
                    ]
                    + [(b"x-api-key", self.spec["capability"].encode())],
                    "client": ("127.0.0.1", 0),
                    "server": ("batch-owned", 0),
                }
                await self.app(scope, receive, send)
            self.store.update(batch, identity, record["generation"], state="completed")
        except asyncio.CancelledError:
            self.store.cancel(batch, identity)
            raise
        except Exception as exc:
            from lightrag.utils import logger

            logger.exception("Batch business request failed: %s", identity)
            self.store.update(
                batch,
                identity,
                record["generation"],
                state="failed",
                error=type(exc).__name__ + ": inspect worker.log",
            )

    async def tick(self, *, admit=True):
        for identity, task in list(self.running.items()):
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                del self.running[identity]
            elif (
                self.store.get(self.spec["data_batch_id"], identity)["state"]
                == "cancelled"
            ):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                del self.running[identity]
        while admit and len(self.running) < 16:
            record = self.store.claim(self.spec)
            if not record:
                break
            self.running[record["id"]] = asyncio.create_task(self.execute(record))

    async def drain(self):
        for record in self.store.active(self.spec["data_batch_id"]):
            if record["state"] == "queued":
                self.store.update(
                    record["batch"],
                    record["id"],
                    record["generation"],
                    state="failed",
                    error="Runtime stopping; resubmit explicitly",
                )
        while self.running:
            await self.tick(admit=False)
            await asyncio.sleep(0.03)
        background = getattr(self.app.state, "background_tasks", set())
        if background:
            await asyncio.gather(*list(background), return_exceptions=True)
        if hasattr(self.app.state, "rag"):
            while (await business_activity(self.app))["busy"]:
                await asyncio.sleep(0.1)
