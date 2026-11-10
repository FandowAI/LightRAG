"""Build only owned shadow vectors, without entering a canonical RAG lifespan.

The shared runtime reservation and provider hook must already be installed.
Never report ordinary readiness or flush/finalize canonical stores here.
See docs/design/BatchIndexRebuild.md.
"""

import asyncio
import hashlib
import os
from pathlib import Path
import signal
import time

from ..provider_quota.hooks import TASK, httpx_spec
from ..runtime_process import atomic_json, process_identity, process_scope
from ..storage import Conflict, IntegrityError
from ..writer_claim import WorkspaceClaim
from .authority import VectorBinding
from .binding import bind_vectors
from .builder import build_target
from .verification import content_transform, context


async def run_builder(rag, spec, index, runtime_store):
    from ..paths import ControlPaths

    paths = ControlPaths(
        **{key: Path(value) for key, value in spec["control_paths"].items()}
    )
    registry = runtime_store.registry
    batch, operation = spec["data_batch_id"], spec.get("application_id")
    record, root, audit = context(registry, paths, batch, operation)
    runtime = runtime_store.get(batch)
    if (
        index is None
        or index["state"] != "declared"
        or record["state"] not in {"builder_starting", "building"}
        or record["cancel_requested"]
        or index["binding"]["operation_id"] != operation
        or runtime.get("purpose") != "index_build"
        or runtime["state"] != "starting"
        or any(
            runtime.get(key) != spec.get(key)
            for key in ("generation", "lease_id", "startup_digest")
        )
        or runtime["pid"] != os.getpid()
        or runtime["process_identity"] != process_identity(os.getpid())
    ):
        raise Conflict("This process does not own a declared index build")
    target = VectorBinding.model_validate(index["binding"]).target(
        audit.identity, record["source_evidence"]["manifest_sha256"]
    )
    stores = bind_vectors(rag, target)
    if httpx_spec() != spec["startup"]["provider_admission"]:
        raise IntegrityError("Builder provider admission was not installed")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    from .store import RebuildStore

    store = RebuildStore(registry)

    def cancelled():
        current = store.get(batch, operation)
        return (
            stop.is_set()
            or current["cancel_requested"]
            or current["state"]
            not in {"builder_starting", "building", "stopping_builder"}
        )

    progress = {"state": "starting", "total": 0, "written": 0, "verified": 0}

    def update(value):
        progress.update(value)

    async def build():
        token = TASK.set(operation)
        try:
            return await build_target(
                root / "source",
                root / "build",
                target,
                stores,
                transform=content_transform(rag),
                cancelled=cancelled,
                on_progress=update,
            )
        finally:
            TASK.reset(token)

    # Preserve the original canonical ownership boundary without initializing or
    # finalizing graph/KV/status stores. Only the shadow vector stores are opened.
    with WorkspaceClaim.from_environment(rag.working_dir, rag.workspace):
        task = asyncio.create_task(build())
        receipt_sha256 = None
        try:
            while True:
                if stop.is_set():
                    if not task.done():
                        task.cancel()
                    await task
                    return
                if task.done():
                    await task
                    receipt_sha256 = hashlib.sha256(
                        (root / "build" / "verified.json").read_bytes()
                    ).hexdigest()
                elif cancelled():
                    task.cancel()
                    await task
                proof = {
                    "state": "index_built" if receipt_sha256 else "index_building",
                    "data_batch_id": batch,
                    "generation": spec["generation"],
                    "lease_id": spec["lease_id"],
                    "startup_digest": spec["startup_digest"],
                    "capability": spec["capability"],
                    "workspace": rag.workspace,
                    "pid": os.getpid(),
                    "process_identity": process_identity(os.getpid()),
                    "process_scope": process_scope(),
                    "provider_admission": httpx_spec(),
                    "index_binding_id": operation,
                    "index_binding_digest": index["digest"],
                    "target": target.model_dump(),
                    "receipt_sha256": receipt_sha256,
                    "progress": dict(progress),
                    "observed_at": time.time(),
                }
                atomic_json(Path(spec["attempt_directory"]) / "health.json", proof)
                try:
                    await asyncio.wait_for(
                        stop.wait(), spec["startup"]["heartbeat_seconds"]
                    )
                except TimeoutError:
                    pass
        finally:
            if not task.done():
                task.cancel()
            # build_target discards only owned pending operations on failure.
            # Joining prevents executor writes from outliving the process lease.
            await asyncio.gather(task, return_exceptions=True)
            # Cancelling a caller's embedding future does not stop its queue's
            # running SDK task. Set the wrapper's shutdown flag before cancelling
            # workers, otherwise they can consume cancellation and wait forever.
            # This closes only the dedicated builder queue, never its stores.
            shutdown = getattr(rag.embedding_func.func, "shutdown", None)
            if shutdown is not None:
                await shutdown(graceful=False)
