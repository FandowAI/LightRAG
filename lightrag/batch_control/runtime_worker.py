"""One RAG lifespan per child process; private observations expose no model secrets."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import traceback

from .resources import digest
from .runtime_config import fingerprint
from .runtime_process import atomic_json, process_identity, process_scope
from .runtime_store import RuntimeStore
from .storage import Registry
from .bindings import BindingStore, graph_baseline, verify_storage_binding


def observation(rag, spec):
    from .provider_quota.hooks import httpx_spec
    from lightrag.api.lightrag_server import _get_storage_workspaces

    storages = {
        "kv_storage": rag.full_docs,
        "doc_status_storage": rag.doc_status,
        "graph_storage": rag.chunk_entity_relation_graph,
        "vector_storage": rag.entities_vdb,
    }
    if spec["startup"]["storage_mode"] == "neo4j_milvus":
        collections = {
            "graph": rag.chunk_entity_relation_graph._get_raw_workspace_label(),
            **{
                key: getattr(rag, attribute).final_namespace
                for key, attribute in (
                    ("entities", "entities_vdb"),
                    ("relationships", "relationships_vdb"),
                    ("chunks", "chunks_vdb"),
                )
            },
        }
    else:
        collections = {
            "graph": rag.workspace + "_" + rag.chunk_entity_relation_graph.namespace,
            **{
                key: getattr(rag, attribute).final_namespace
                for key, attribute in (
                    ("entities", "entities_vdb"),
                    ("relationships", "relationships_vdb"),
                    ("chunks", "chunks_vdb"),
                )
            },
        }
    storage_files = (
        {}
        if spec["startup"]["storage_mode"] == "neo4j_milvus"
        else {
            "graph": rag.chunk_entity_relation_graph._graphml_xml_file,
            **{
                key: getattr(rag, attribute)._client_file_name
                for key, attribute in (
                    ("entities", "entities_vdb"),
                    ("relationships", "relationships_vdb"),
                    ("chunks", "chunks_vdb"),
                )
            },
        }
    )
    from .legacy import storage_identity

    identity = storage_identity(rag)
    return {
        "state": "ready",
        "provider_admission": httpx_spec(),
        "index_binding_id": (spec["startup"].get("index_binding") or {}).get(
            "operation_id"
        ),
        "index_binding_digest": spec["startup"].get("index_binding_digest"),
        "observed_at": time.time(),
        "data_batch_id": spec["data_batch_id"],
        "generation": spec["generation"],
        "startup_digest": spec["startup_digest"],
        "capability": spec["capability"],
        "workspace": rag.workspace,
        "pid": os.getpid(),
        "process_identity": process_identity(os.getpid()),
        "process_scope": process_scope(),
        "working_directory": str(Path(rag.working_dir).resolve()),
        "input_directory": str(Path(rag.input_dir).resolve()),
        "storage_workspaces": _get_storage_workspaces(rag),
        "storage_classes": {
            key: type(storage).__name__ for key, storage in storages.items()
        },
        "collections": collections,
        "auxiliary_collections": identity["auxiliary_collections"],
        "storage_endpoints": identity["storage_endpoints"],
        "role_provider_options": identity["role_provider_options"],
        "runtime_resource_digests": identity["runtime_resource_digests"],
        "storage_files": storage_files,
        "embedding": {
            "model": rag.embedding_func.model_name,
            "dimensions": rag.embedding_func.embedding_dim,
        },
        "parameters": {
            "chunk_size": rag.chunk_token_size,
            "chunk_overlap_size": rag.chunk_overlap_token_size,
            "max_parallel_insert": rag.max_parallel_insert,
            "max_parallel_parse_native": rag.max_parallel_parse_native,
        },
        "roles": {
            role: {
                key: value.get(key) for key in ("model", "binding", "host", "max_async")
            }
            for role, value in rag.get_llm_role_config().items()
        },
    }


async def run(spec):
    startup = spec["startup"]
    registry = RuntimeStore(Registry(Path(spec["registry"])))
    record = registry.get(spec["data_batch_id"])
    if not record or hashlib.sha256(
        spec["capability"].encode()
    ).hexdigest() != record.get("capability_sha256"):
        raise ValueError("Worker capability does not own the reserved generation")
    if digest(startup) != spec["startup_digest"]:
        raise ValueError("Worker startup specification changed")
    registry.attach(
        spec["data_batch_id"],
        spec["generation"],
        spec["startup_digest"],
        os.getpid(),
        process_identity(os.getpid()),
    )
    if (
        fingerprint(Path(startup["project_directory"]))
        != startup["runtime_fingerprint"]
    ):
        raise ValueError(
            "Runtime source, dependency lock, interpreter or installed dependencies changed"
        )
    if any(
        os.environ.get(key) != value for key, value in startup["environment"].items()
    ):
        raise ValueError("Compiled runtime environment changed")
    from .provider_quota.hooks import install_httpx

    install_httpx(startup["provider_admission"])
    for key in ("WORKING_DIR", "INPUT_DIR", "LOG_DIR"):
        Path(startup["environment"][key]).mkdir(parents=True, exist_ok=True, mode=0o700)
    # No project .env is loaded: imports execute from the private attempt directory.
    sys.argv = ["lightrag-server"]
    from lightrag.api.config import initialize_config
    from lightrag.api.lightrag_server import create_app

    args = initialize_config(force=True)
    # This child has no listener; only its validated private channels receive
    # this per-generation key. Routes can retain their normal write-auth checks.
    args.key = spec["capability"]
    app = create_app(args)
    from .index_rebuild.authority import startup_binding
    from .index_rebuild.binding import bind_vectors
    from .index_rebuild.builder import TargetIdentity
    from .paths import ControlPaths

    purpose = startup.get("purpose", "rag")
    if purpose not in {"rag", "index_build"}:
        raise ValueError(
            "This worker entry does not accept a preflight/build as RAG readiness"
        )
    index = startup_binding(
        registry.registry,
        ControlPaths(
            **{key: Path(value) for key, value in spec["control_paths"].items()}
        ),
        spec,
    )
    if purpose == "index_build":
        from .index_rebuild.build_worker import run_builder

        await run_builder(app.state.rag, spec, index, registry)
        return
    if index is not None:
        target = TargetIdentity.model_validate(index["evidence"]["receipt"]["identity"])
        bind_vectors(app.state.rag, target)
    restoration = None
    if spec.get("rebuild_rollback_id") is not None:
        from .index_rebuild.store import RebuildStore

        restoration = RebuildStore(registry.registry).get(
            spec["data_batch_id"], spec["rebuild_rollback_id"]
        )
    app.state.skip_migrations_for_verified_index = bool(
        index is not None
        and index["state"] == "verified"
        or restoration
        and restoration.get("source_evidence")
    )
    from .legacy import storage_identity

    verify_storage_binding(storage_identity(app.state.rag), startup)
    adopted = BindingStore(registry.registry).get(spec["data_batch_id"])
    if bool(adopted) != bool(startup.get("storage_binding")) or (
        adopted and adopted["digest"] != startup.get("storage_binding_digest")
    ):
        raise ValueError("Adopted registry and startup binding differ")
    if (
        adopted
        and adopted["state"] == "registered"
        and adopted["binding"]["runtime_resource_digests"]
        and storage_identity(app.state.rag)["runtime_resource_digests"]
        != adopted["binding"]["runtime_resource_digests"]
    ):
        raise ValueError(
            "Initial adopted runtime resources differ from the frozen source"
        )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with app.router.lifespan_context(app):
        from .task_runner import TaskRunner
        from .http_worker import HttpRunner, business_activity
        from .index_rebuild.worker import CaptureRunner
        from .index_rebuild.verification import verify_candidate, verify_restoration

        adoption_proof = {}
        paths = ControlPaths(
            **{key: Path(value) for key, value in spec["control_paths"].items()}
        )
        if index is not None and index["state"] == "verified":
            adoption_proof["index_initial_verified"] = await verify_candidate(
                app.state.rag, spec, index, registry.registry, paths
            )
        restored_source = await verify_restoration(
            app.state.rag, spec, registry.registry, paths
        )
        if restored_source is not None:
            adoption_proof["index_source_restored"] = restored_source
        if adopted:
            adoption_proof["storage_binding_digest"] = adopted["digest"]
            if adopted["state"] == "registered":
                baseline = await graph_baseline(
                    app.state.rag.chunk_entity_relation_graph
                )
                if baseline != adopted["binding"]["graph"]:
                    raise ValueError("Adopted graph does not match its frozen baseline")
                adoption_proof["adoption_graph"] = baseline
                if adopted["binding"]["vectors"]:
                    from .adoption_snapshot import vector_baselines

                    vectors = await vector_baselines(app.state.rag)
                    if vectors != adopted["binding"]["vectors"]:
                        raise ValueError("Adopted vector contents changed")
                    adoption_proof["adoption_vectors"] = vectors
                if adopted["binding"]["document_states"]:
                    counts = await app.state.rag.doc_status.get_status_counts()
                    states = {
                        str(getattr(key, "value", key)): value
                        for key, value in counts.items()
                    }
                    if states != adopted["binding"]["document_states"]:
                        raise ValueError("Adopted document states changed")
                    adoption_proof["adoption_document_states"] = states
        runner = TaskRunner(app, spec)
        http = HttpRunner(app, spec)
        capture = CaptureRunner(app, spec)
        while not stop.is_set():
            await runner.tick()
            await http.tick()
            await capture.tick()
            proof = observation(app.state.rag, spec)
            proof.update(adoption_proof)
            proof["business_activity"] = await business_activity(app)
            atomic_json(
                Path(spec["attempt_directory"]) / "health.json",
                proof,
            )
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=startup["heartbeat_seconds"]
                )
            except TimeoutError:
                pass
        await capture.drain()
        await asyncio.gather(runner.drain(), http.drain())
    atomic_json(
        Path(spec["attempt_directory"]) / "health.json",
        {
            "state": "stopped",
            "generation": spec["generation"],
            "observed_at": time.time(),
        },
    )


def run_owned_loop(spec):
    """Join async work and executor threads before the process can release its lease."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run(spec))
    finally:
        pending = list(asyncio.all_tasks(loop))
        for task in pending:
            task.cancel()

        async def cleanup():
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await loop.shutdown_asyncgens()
            joining = asyncio.create_task(loop.shutdown_default_executor())
            # Keep observing completion while executor threads retire. This also
            # works on restricted hosts whose cross-thread socket wakeup is absent.
            while not joining.done():
                await asyncio.sleep(0.05)
            await joining

        try:
            loop.run_until_complete(cleanup())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    arguments = parser.parse_args()
    spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
    os.chdir(spec["attempt_directory"])
    try:
        run_owned_loop(spec)
    except BaseException:
        traceback.print_exc()
        atomic_json(
            Path(spec["attempt_directory"]) / "health.json",
            {
                "state": "failed",
                "generation": spec["generation"],
                "observed_at": time.time(),
            },
        )
        raise


if __name__ == "__main__":
    main()
