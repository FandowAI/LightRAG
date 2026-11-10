"""Recoverable ownership transfer keeps original data, evidence and failed statuses."""

# ruff: noqa: F811

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest

from lightrag.batch_control.adoption import AdoptionService, LegacySource, SourceProcess
from lightrag.batch_control.adoption_files import backup, verify_backup
from lightrag.batch_control.adoption_snapshot import (
    milvus_snapshot,
    snapshot,
    vector_baselines,
    verify_snapshot,
    write_rows,
)
from lightrag.batch_control.bindings import BindingStore, rows_digest
from lightrag.batch_control.resources import digest
from lightrag.batch_control.storage import Conflict, IntegrityError
from lightrag.batch_control.writer_claim import WorkspaceClaim
from tests.batch_control.test_runtime import managed, runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline


def nano(rows):
    class NanoVectorDBStorage:
        @property
        async def client_storage(self):
            return {
                "data": [{"__id__": key, "content": "原始证据"} for key in rows],
                "matrix": np.array([[0.1, 0.2] for key in rows]),
            }

    return NanoVectorDBStorage()


def snapshot_rag():
    return SimpleNamespace(
        workspace="legacy",
        chunk_entity_relation_graph=SimpleNamespace(
            get_all_nodes=AsyncMock(return_value=[{"id": "entity-one"}]),
            get_all_edges=AsyncMock(return_value=[]),
        ),
        entities_vdb=nano(["entity-one"]),
        relationships_vdb=nano([]),
        chunks_vdb=nano(["chunk-one", "chunk-two"]),
        entity_merge=SimpleNamespace(name_vdb=nano(["name-one"])),
    )


async def test_snapshot_covers_auxiliary_vectors_and_retries_partial_attempt(tmp_path):
    rag = snapshot_rag()
    observation = {
        "workspace": "legacy",
        "boot_id": "boot",
        "freeze_owner": "operation",
        "graph": {
            "nodes": 1,
            "edges": 0,
            "nodes_sha256": rows_digest([{"id": "entity-one"}]),
            "edges_sha256": rows_digest([]),
        },
    }
    directory = tmp_path / "snapshot"
    directory.mkdir()
    (directory / "partial.jsonl").write_text("interrupted")
    result = await snapshot(rag, directory, observation)
    assert result["vectors"]["name_entities"]["rows"] == 1
    assert result["vectors"]["chunks"]["rows"] == 2
    assert len(list(tmp_path.glob("snapshot.partial-*"))) == 1
    assert await snapshot(rag, directory, observation) == result
    assert await vector_baselines(rag) == result["vectors"]
    row = json.loads((directory / "chunks.jsonl").read_text().splitlines()[0])
    assert row["vector"] == [0.1, 0.2] and row["content"] == "原始证据"
    (directory / "chunks.jsonl").write_text("damaged")
    with pytest.raises(IntegrityError, match="artifact changed"):
        verify_snapshot(directory, observation)


def test_vector_snapshot_rejects_duplicate_ids_and_milvus_truncation(tmp_path):
    with pytest.raises(IntegrityError, match="duplicate"):
        write_rows([{"id": "same"}, {"id": "same"}])
    closed = []
    batches = iter([[{"id": "one", "vector": [0.1]}], []])
    client = SimpleNamespace(
        query_iterator=lambda **kwargs: SimpleNamespace(
            next=lambda: next(batches), close=lambda: closed.append(True)
        ),
        query=lambda **kwargs: [{"count(*)": 2}],
    )
    with pytest.raises(IntegrityError, match="complete collection"):
        milvus_snapshot(
            SimpleNamespace(_client=client, final_namespace="original"),
            tmp_path / "vectors.jsonl",
        )
    assert closed == [True]


async def test_snapshot_rejects_redirected_directory_and_pending_vectors(tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    (tmp_path / "redirect").symlink_to(target, target_is_directory=True)
    with pytest.raises(IntegrityError, match="redirected"):
        await snapshot(snapshot_rag(), tmp_path / "redirect/snapshot", {})
    assert list(target.iterdir()) == []
    rag = snapshot_rag()
    rag.chunks_vdb._pending_upserts = {"uncommitted": True}
    with pytest.raises(Conflict, match="pending vector"):
        await vector_baselines(rag)


@pytest.fixture
def transfer(managed, tmp_path):
    control, runtime, backend = managed
    secret = tmp_path / "source.env"
    secret.write_text("LIGHTRAG_API_KEY=synthetic-source-key\n")
    control.catalog.credentials["source-key"] = secret
    source = LegacySource(
        url="http://127.0.0.1:9631",
        workspace="legacy",
        working_directory=str(tmp_path / "original"),
        input_directory=str(tmp_path / "inputs"),
        command_directory=str(tmp_path),
        command_sha256="a" * 64,
        credential_id="source-key",
    )
    root = Path(source.working_directory) / source.workspace
    root.mkdir(parents=True)
    (root / "kv_store_full_docs.json").write_text('{"doc":"Original evidence"}')
    (root / "kv_store_doc_status.json").write_text(
        '{"processed":2,"failed":1,"skipped":1}'
    )
    (root / "batch_retrieval").mkdir()
    (root / "batch_retrieval/sources.jsonl").write_text("old export\n")
    claim = WorkspaceClaim(source.working_directory, source.workspace).__enter__()
    graph = {
        "nodes": 0,
        "edges": 0,
        "nodes_sha256": rows_digest([]),
        "edges_sha256": rows_digest([]),
    }
    role = {
        "model": "offline-chat",
        "binding": "openai",
        "host": "http://127.0.0.1:9/v1",
        "max_async": 8,
        "timeout": 180,
        "credential_sha256": None,
    }
    observation = {
        "protocol_version": 2,
        "workers": 1,
        "writer_claim": {"held": True, "owner": None},
        "workspace": "legacy",
        "working_directory": source.working_directory,
        "input_directory": source.input_directory,
        "command_digest": source.command_sha256,
        "pid": 123,
        "process_identity": "original-birth",
        "boot_id": "source-boot",
        "pid_namespace": "same",
        "host_boot_id": "host",
        "collections": {
            name: "legacy_" + name
            for name in ("graph", "entities", "relationships", "chunks")
        },
        "embedding": {"model": "offline-embedding", "dimensions": 8},
        "storage_endpoints": {},
        "auxiliary_collections": {},
        "storage_classes": {"vector_storage": "NanoVectorDBStorage"},
        "rerank_enabled": False,
        "chunking_function": "chunking_by_token_size",
        "environment": {},
        "resource_sha256": {},
        "base_model": role,
        "roles": {
            name: deepcopy(role) for name in ("query", "extract", "keyword", "vlm")
        },
        "embedding_profile": {
            **role,
            "model": "offline-embedding",
            "batch_size": 16,
            "max_tokens": 8192,
        },
        "parameters": {
            "chunk_size": 1200,
            "chunk_overlap_size": 100,
            "max_parallel_insert": 5,
            "max_parallel_parse_native": 5,
        },
        "document_states": {"processed": 2, "failed": 1, "skipped": 1},
        "freeze_owner": None,
        "busy": False,
        "graph": graph,
        "retrieval_directory": str(root / "batch_retrieval"),
    }

    class Processes:
        alive = True
        signals = 0

        def check(self, source, observation, *, gone_ok=False):
            if self.alive:
                return self
            if gone_ok:
                return None
            raise Conflict("Source exited")

        def stop(self, source, observation):
            if self.alive:
                self.signals += 1
                claim.__exit__()
                self.alive = False
                return False
            return True

    processes = Processes()

    def handler(request):
        assert request.headers["X-API-Key"] == "synthetic-source-key"
        if request.url.path.endswith("/freeze"):
            observation["freeze_owner"] = json.loads(request.content)["request_id"]
        elif request.url.path.endswith("/unfreeze"):
            observation["freeze_owner"] = None
        elif request.url.path.endswith("/snapshot"):
            directory = (
                Path(source.working_directory)
                / ".adoption-snapshots/legacy"
                / observation["freeze_owner"]
            )
            directory.mkdir(parents=True)
            from lightrag.batch_control.runtime_process import atomic_json

            artifacts = {}
            for name in ("nodes", "edges", "entities", "relationships", "chunks"):
                (directory / (name + ".jsonl")).write_bytes(b"")
                artifacts[name + ".jsonl"] = {
                    "bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }

            atomic_json(
                directory / "manifest.json",
                {
                    "schema_version": 1,
                    **{
                        key: observation[key]
                        for key in ("workspace", "boot_id", "freeze_owner", "graph")
                    },
                    "vectors": {
                        key: write_rows([])
                        for key in ("entities", "relationships", "chunks")
                    },
                    "artifacts": artifacts,
                },
            )
        return httpx.Response(200, json=deepcopy(observation))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = AdoptionService(
        control,
        runtime.policy,
        {"old": source},
        actor="user",
        client=client,
        processes=processes,
        backend=backend,
    )

    def proof(value):
        binding = BindingStore(control.registry).get(value["data_batch_id"])["binding"]
        return {
            **value,
            "storage_binding_digest": digest(binding),
            "adoption_graph": binding["graph"],
            "adoption_vectors": binding["vectors"],
            "adoption_document_states": binding["document_states"],
        }

    backend.change = proof
    yield service, source, observation, processes, root
    claim.__exit__()
    client.close()


def advance(service, operation, phase):
    for _ in range(15):
        record = service.store.get(operation)
        if record["phase"] == phase:
            return record
        record = service.step(operation)
        assert not record.get("error"), record
    pytest.fail("Transfer did not reach " + phase)


def test_transfer_is_idempotent_recoverable_and_preserves_originals(transfer):
    service, source, observation, processes, root = transfer
    original = {path.name: path.read_bytes() for path in root.glob("*.json")}
    record = service.request("old", "Existing data", "same-request")
    operation = record["operation_id"]
    assert (
        service.request("old", "Existing data", "same-request")["operation_id"]
        == operation
    )
    advance(service, operation, "register")
    reconstructed = AdoptionService(
        service.control,
        service.policy,
        service.sources,
        actor="user",
        client=service.client,
        processes=processes,
        backend=service.runtime.backend,
    )
    ready = advance(reconstructed, operation, "ready")
    assert processes.signals == 1
    assert {name: (root / name).read_bytes() for name in original} == original
    assert (root / "batch_retrieval/sources.jsonl").read_text() == "old export\n"
    assert verify_backup(ready["backup_directory"])["workspace"] == "legacy"
    assert (
        BindingStore(service.control.registry).get(record["batch_id"])["state"]
        == "adopted"
    )
    with pytest.raises(Conflict, match="registered workspace"):
        reconstructed.abort(operation)
    reconstructed.runtime.stop(record["batch_id"])
    with pytest.raises(Conflict, match="different batch registry"):
        with WorkspaceClaim(source.working_directory, source.workspace):
            pytest.fail("Legacy service regained an adopted workspace")
    assert "synthetic-source-key" not in json.dumps(reconstructed.public(operation))


def test_configuration_changes_prevent_shutdown_and_abort_releases_freeze(transfer):
    service, _, observation, processes, _ = transfer
    operation = service.request("old", "Existing data", "request")["operation_id"]
    advance(service, operation, "freeze")
    observation["roles"]["extract"]["model"] = "changed-model"
    blocked = service.step(operation)
    assert blocked["phase"] == "freeze" and blocked["error"]
    assert processes.signals == 0
    # An operator can restore the declared source, then safely release the fence.
    observation["roles"]["extract"]["model"] = "offline-chat"
    assert service.abort(operation)["phase"] == "aborted"
    assert observation["freeze_owner"] is None and processes.alive


def test_abort_after_owner_marker_before_sql_keeps_backups_and_releases_only_own_marker(
    transfer, monkeypatch
):
    service, source, _, _, root = transfer
    operation = service.request("old", "Existing data", "request")["operation_id"]
    record = advance(service, operation, "register")

    def fail(*args):
        raise OSError("registration interrupted")

    monkeypatch.setattr(service, "register", fail)
    assert service.step(operation)["error"]
    assert (root / ".batch-owner.json").is_file()
    assert service.abort(operation)["phase"] == "aborted"
    assert not (root / ".batch-owner.json").exists()
    assert verify_backup(record["backup_directory"])
    with WorkspaceClaim(source.working_directory, source.workspace):
        pass


def test_foreign_pid_namespace_never_authorizes_a_signal(tmp_path):
    source = SimpleNamespace(command_sha256="a" * 64, command_directory=str(tmp_path))
    observation = {
        "pid_namespace": "foreign",
        "host_boot_id": "other",
        "pid": os.getpid(),
    }
    with pytest.raises(Conflict, match="PID namespace"):
        SourceProcess().stop(source, observation)


def test_changed_process_command_never_authorizes_a_signal(tmp_path, monkeypatch):
    signals = []
    process = SimpleNamespace(
        create_time=lambda: 1.0,
        status=lambda: "running",
        cmdline=lambda: ["foreign"],
        cwd=lambda: str(tmp_path),
        send_signal=signals.append,
    )
    monkeypatch.setattr(
        "lightrag.batch_control.adoption.psutil.Process", lambda pid: process
    )
    observation = {
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "host_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid": 123,
        "process_identity": "1.0",
    }
    with pytest.raises(Conflict, match="command or directory"):
        SourceProcess().stop(
            SimpleNamespace(command_sha256="a" * 64, command_directory=str(tmp_path)),
            observation,
        )
    assert signals == []


async def test_original_retrieval_directory_participates_in_runtime_drain(
    tmp_path, monkeypatch
):
    import sqlite3
    from lightrag.batch_control.http_worker import business_activity

    monkeypatch.delenv("LIGHTRAG_BATCH_OUTPUT_DIR", raising=False)
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_data", AsyncMock(return_value={})
    )
    directory = tmp_path / "legacy/batch_retrieval"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "jobs.sqlite3") as db:
        db.execute("CREATE TABLE jobs (status TEXT)")
        db.execute("INSERT INTO jobs VALUES ('running')")
    app = SimpleNamespace(
        state=SimpleNamespace(
            rag=SimpleNamespace(workspace="legacy", working_dir=str(tmp_path))
        )
    )
    assert (await business_activity(app))["busy"] is True
    with sqlite3.connect(directory / "jobs.sqlite3") as db:
        db.execute("UPDATE jobs SET status='completed'")
    assert (await business_activity(app))["busy"] is False


def test_interrupted_stop_is_not_replayed_without_explicit_retry(transfer, monkeypatch):
    service, _, _, processes, _ = transfer
    operation = service.request("old", "Existing data", "request")["operation_id"]
    advance(service, operation, "stopping")
    original = processes.stop
    attempts = []

    def interrupted(*args):
        attempts.append(True)
        raise OSError("Signal outcome is unknown")

    monkeypatch.setattr(processes, "stop", interrupted)
    assert service.step(operation)["error"]
    for _ in range(3):
        assert service.step(operation)["phase"] == "stopping"
    assert attempts == [True]
    monkeypatch.setattr(processes, "stop", original)
    service.retry_stop(operation)
    advance(service, operation, "backup")
    assert processes.signals == 1


async def test_adoption_api_has_separate_progress_and_enforces_authorization(
    transfer, monkeypatch
):
    from fastapi import FastAPI, Request
    from lightrag.batch_control.deployment import BatchDeployment
    from lightrag.batch_control.routes import create_control_routes

    service, _, _, _, _ = transfer
    monkeypatch.setattr(
        service.control, "authorize", lambda actor, action, resource: actor == "user"
    )
    deployment = BatchDeployment(service.control, service.policy)
    deployment.adoption = lambda actor: AdoptionService(
        service.control,
        service.policy,
        service.sources,
        actor=actor,
        client=service.client,
        processes=service.processes,
        backend=service.runtime.backend,
    )

    async def principal(request: Request):
        return request.headers.get("Actor", "user")

    app = FastAPI()
    app.include_router(create_control_routes(deployment, principal))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        body = {"source_id": "old", "name": "Existing", "request_id": "api-request"}
        denied = await client.post(
            "/data-batches/adoptions", json=body, headers={"Actor": "other"}
        )
        assert denied.status_code == 403
        created = await client.post("/data-batches/adoptions", json=body)
        assert created.status_code == 202
        operation = created.json()["operation_id"]
        progress = await client.post(f"/data-batches/adoptions/{operation}/step")
        assert progress.status_code == 200 and progress.json()["phase"] == "freeze"
        status = await client.get(f"/data-batches/adoptions/{operation}")
        assert status.status_code == 200 and status.json() == progress.json()
        assert (
            "prepared" not in status.json()
            and "synthetic-source-key" not in status.text
        )
        denied = await client.get(
            f"/data-batches/adoptions/{operation}", headers={"Actor": "other"}
        )
        assert denied.status_code == 403
        aborted = await client.post(f"/data-batches/adoptions/{operation}/abort")
        assert aborted.status_code == 200 and aborted.json()["phase"] == "aborted"


def test_backup_preserves_inputs_and_pins_resources_then_detects_corruption(tmp_path):
    source = SimpleNamespace(
        workspace="old",
        working_directory=str(tmp_path / "storage"),
        input_directory=str(tmp_path / "inputs"),
    )
    directory = Path(source.input_directory) / "old"
    directory.mkdir(parents=True)
    (directory / "source.md").write_text("original")
    remote = tmp_path / "remote"
    remote.mkdir()
    resource = tmp_path / "types.yaml"
    resource.write_text("types: [original]")
    history = tmp_path / "separate-history/old"
    history.mkdir(parents=True)
    (history / "sources.jsonl").write_text("original retrieval export\n")
    result = backup(
        source,
        tmp_path / "backup",
        remote,
        {"ENTITY_TYPE_PROMPT_FILE": str(resource)},
        retrieval_directory=history,
    )
    assert (
        result["resources"]["ENTITY_TYPE_PROMPT_FILE"]["sha256"]
        == hashlib.sha256(resource.read_bytes()).hexdigest()
    )
    assert (tmp_path / "backup/inputs/source.md").read_text() == "original"
    assert (
        tmp_path / "backup/retrieval/sources.jsonl"
    ).read_text() == "original retrieval export\n"
    (tmp_path / "backup/inputs/source.md").write_text("corrupted")
    with pytest.raises(IntegrityError, match="backup changed"):
        verify_backup(tmp_path / "backup")
