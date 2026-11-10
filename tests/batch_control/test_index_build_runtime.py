"""Actual owned builder and full-candidate activation without external models."""

# ruff: noqa: F811

import json
import os
from pathlib import Path
import time

import networkx as nx
import pytest

from lightrag.batch_control.index_rebuild.build_runtime import BuilderRuntime
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_process import ProcessBackend
from lightrag.batch_control.storage import Conflict
from lightrag.batch_control.provider_quota.core import QuotaStore
from lightrag.batch_control.provider_quota.wiring import registry_path
from .test_index_rebuild_store import advance, prepare_rebuild
from .test_index_rebuild_projection import canonical  # noqa: F401
from .test_runtime import runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline

TRANSPORT = """
import asyncio,json,os
from pathlib import Path
import httpx
async def transport(client, request):
    if not str(request.url).endswith('/embeddings'):
        raise AssertionError('Unexpected offline model request: ' + str(request.url))
    payload=json.loads(request.content)
    assert payload['model']=='new-offline-embedding', payload
    texts=payload['input']; texts=[texts] if isinstance(texts,str) else texts
    from lightrag.batch_control.provider_quota.hooks import TASK
    with Path(os.environ['OFFLINE_EMBEDDINGS_LOG']).open('a') as stream:
        stream.write(json.dumps({'task':TASK.get(),'model':payload['model'],'input':texts})+'\\n')
    gate=os.environ.get('OFFLINE_EMBEDDINGS_GATE')
    while gate and not Path(gate).exists():
        await asyncio.sleep(.02)
    dimension=int(os.environ['EMBEDDING_DIM'])
    rows=[{'object':'embedding','index':i,'embedding':[float(len(text)+j+1) for j in range(dimension)]} for i,text in enumerate(texts)]
    return httpx.Response(200,json={'object':'list','model':payload['model'],'data':rows,'usage':{'prompt_tokens':7,'total_tokens':7}},request=request)
httpx.AsyncClient._send_single_request=transport
"""


class TransportBackend(ProcessBackend):
    def __init__(self, root, *, hold=False):
        super().__init__()
        self.root = root
        self.hold = hold
        bootstrap = root / "bootstrap"
        bootstrap.mkdir()
        (bootstrap / "sitecustomize.py").write_text(TRANSPORT)

    def launch(self, spec, environment):
        return super().launch(
            spec,
            {
                **environment,
                "PYTHONPATH": str(self.root / "bootstrap")
                + os.pathsep
                + environment["PYTHONPATH"],
                "OFFLINE_EMBEDDINGS_LOG": str(self.root / "requests.jsonl"),
                **(
                    {"OFFLINE_EMBEDDINGS_GATE": str(self.root / "release")}
                    if self.hold
                    else {}
                ),
            },
        )


def seed_source(control, batch, canonical):
    # Sole writer: these files are created before the first worker starts.
    credential = control.paths.config_root / "offline-model.env"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("OFFLINE_TEST_KEY=synthetic-offline-value\n")
    control.catalog.credentials["offline-model"] = credential
    control.catalog.profiles["embed-v2"].update(
        credential_id="offline-model", key_name_of_api_key="OFFLINE_TEST_KEY"
    )
    root = (
        control.paths.runtime_root
        / "rag"
        / batch["namespace"]
        / "storage"
        / batch["workspace"]
    )
    root.mkdir(parents=True)
    values = {
        "full_docs": {
            "doc-a": {"content": "甲部门管理乙项目。", "file_path": "one.md"}
        },
        "text_chunks": {
            row["id"]: {key: value for key, value in row.items() if key != "id"}
            for row in canonical["chunks"]
        },
        "doc_status": {
            "doc-a": {
                "content_summary": "Synthetic document",
                "content_length": 12,
                "file_path": "one.md",
                "status": "processed",
                "created_at": "2026-11-04T00:00:00Z",
                "updated_at": "2026-11-04T00:00:00Z",
                "chunks_list": ["chunk-a"],
                "chunks_count": 1,
            }
        },
        "full_entities": {
            "doc-a": {
                "entity_names": [row["id"] for row in canonical["nodes"]],
                "count": 2,
            }
        },
        "full_relations": {
            "doc-a": {
                "relation_pairs": [
                    [row["source"], row["target"]] for row in canonical["edges"]
                ],
                "count": 1,
            }
        },
        "entity_chunks": {
            row["id"]: {"chunk_ids": ["chunk-a"], "count": 1}
            for row in canonical["nodes"]
        },
        "relation_chunks": {
            "synthetic-relation": {"chunk_ids": ["chunk-a"], "count": 1}
        },
    }
    for name, rows in values.items():
        (root / ("kv_store_" + name + ".json")).write_text(
            json.dumps(rows, ensure_ascii=False)
        )
    graph = nx.Graph()
    for row in canonical["nodes"]:
        graph.add_node(
            row["id"], **{key: value for key, value in row.items() if key != "id"}
        )
    for row in canonical["edges"]:
        graph.add_edge(
            row["source"],
            row["target"],
            **{
                key: value
                for key, value in row.items()
                if key not in {"source", "target"}
            },
        )
    nx.write_graphml(graph, root / "graph_chunk_entity_relation.graphml")


def capture_source(case):
    advance(case, "source_capturing")
    deadline = time.monotonic() + 30
    while True:
        captured = case.store.get(case.batch["id"], case.operation)
        if captured["state"] != "source_capturing":
            break
        assert time.monotonic() < deadline, case.runtime.store.get(case.batch["id"])
        assert (
            case.backend.ownership(case.runtime.store.get(case.batch["id"])) == "owned"
        )
        time.sleep(0.05)
    assert captured["state"] == "source_captured", captured
    assert set(captured["source_evidence"]) == {"manifest_sha256", "audit_sha256"}
    return captured


@pytest.mark.parametrize(
    "populated,retire_before_verify", [(False, False), (True, False), (True, True)]
)
def test_actual_builder_then_full_candidate_activates_without_touching_peer(
    runtime_setup,
    tmp_path,
    canonical,
    populated,
    retire_before_verify,
):
    control, policy = runtime_setup
    backend = TransportBackend(tmp_path) if populated else ProcessBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    peer = control.create("user", "Peer", "peer")
    evidence = None
    try:
        peer_lease = runtime.start(peer["id"], peer["active"])
        case = prepare_rebuild(
            control,
            policy,
            backend,
            runtime,
            seed=(lambda batch: seed_source(control, batch, canonical))
            if populated
            else None,
        )
        captured = capture_source(case)
        advance(case, "stopping_source")
        old = runtime.store.get(case.batch["id"])
        runtime.stop(case.batch["id"])
        assert backend.ownership(old) == "gone"
        advance(case, "builder_starting")
        builder = BuilderRuntime(runtime)
        row = builder.start(case.batch["id"], case.operation)
        assert row["purpose"] == "index_build"
        advance(case, "building", builder_lease=row)
        deadline = time.monotonic() + 30
        while True:
            row, proof = builder.inspect(case.batch["id"], case.operation)
            assert row["state"] != "failed", (
                Path(row["attempt_directory"]) / "worker.log"
            ).read_text()[-8000:]
            if row["state"] == "built":
                break
            assert time.monotonic() < deadline, row
            time.sleep(0.05)
        assert proof["state"] == "index_built"
        assert proof["provider_admission"]
        assert runtime.inspect(case.batch["id"]) is None
        assert (
            builder.start(case.batch["id"], case.operation)["generation"]
            == row["generation"]
        )
        # A completed builder retains process capacity until its exit is proven.
        third = control.create("user", "Capacity probe", "third")
        with pytest.raises(Conflict, match="capacity"):
            runtime.start(third["id"], third["active"])
        if retire_before_verify:
            runtime.stop(case.batch["id"])
            assert backend.ownership(row) == "gone"
            builder = BuilderRuntime(
                RuntimeSupervisor(control, policy, actor="user", backend=backend)
            )
            with pytest.raises(Conflict, match="cannot be replayed"):
                builder.start(case.batch["id"], case.operation)
        receipt, checksum = builder.receipt(case.batch["id"], case.operation)
        total = 4 if populated else 0
        assert receipt["counts"] == {
            "total": total,
            "written": total,
            "verified": total,
        }
        case.store.verify_build(
            case.batch["id"],
            case.operation,
            receipt,
            checksum,
            disposition="gone" if retire_before_verify else "owned",
        )
        runtime.stop(case.batch["id"])
        assert backend.ownership(row) == "gone"
        advance(case, "candidate_starting")
        candidate = runtime.start(
            case.batch["id"], case.revision["id"], application_id=case.operation
        )
        advance(case, "candidate_verifying")
        proof = runtime._proof(runtime.store.get(case.batch["id"]))
        assert (
            proof["index_initial_verified"]["source_audit_sha256"]
            == captured["source_evidence"]["audit_sha256"]
        )
        result = case.store.activate(case.batch["id"], case.operation, candidate, proof)
        assert result["state"] == "applied"
        assert control.registry.get(case.batch["id"])["active"] == case.revision["id"]
        assert case.store.binding(case.batch["id"], case.operation)["state"] == "active"
        assert runtime.inspect(peer["id"]) == peer_lease
        assert (
            RuntimeSupervisor(control, policy, actor="user").start(
                case.batch["id"], case.revision["id"]
            )
            == candidate
        )
        requests = []
        if populated:
            requests = [
                json.loads(line)
                for line in (tmp_path / "requests.jsonl").read_text().splitlines()
            ]
            assert sum(len(row["input"]) for row in requests) == 4
            assert {row["task"] for row in requests} == {case.operation}
            with QuotaStore(registry_path(control.paths)).database() as db:
                charges = [
                    dict(row)
                    for row in db.execute(
                        "SELECT batch,task,state,tokens FROM provider_requests"
                    )
                ]
            assert len(charges) == len(requests) == 3
            assert all(
                row
                == {
                    "batch": case.batch["id"],
                    "task": case.operation,
                    "state": "finished",
                    "tokens": 7,
                }
                for row in charges
            )
        evidence = {
            "scope": "actual source capture/build/full-candidate activation using private model transports; not live model/database acceptance or public coordinator",
            "populated": populated,
            "retired_before_receipt_recovery": retire_before_verify,
            "embedding_requests": requests,
            "source_lease": case.source_lease.model_dump(),
            "builder_generation": row["generation"],
            "builder_pid": row["pid"],
            "builder_process_exited": True,
            "builder_is_not_rag_ready": True,
            "candidate_lease": candidate.model_dump(),
            "peer_lease": peer_lease.model_dump(),
            "peer_unchanged": True,
            "source_evidence": captured["source_evidence"],
            "receipt_sha256": checksum,
            "receipt": receipt,
            "initial_attestation": proof["index_initial_verified"],
            "state": result["state"],
        }
    finally:
        with control.registry.transaction() as db:
            batches = [row[0] for row in db.execute("SELECT id FROM batches")]
        (tmp_path / "release").touch()
        for batch in batches:
            runtime.stop(batch)
        assert all(
            runtime.store.get(batch) is None
            or runtime.store.get(batch)["state"] in {"stopped", "failed"}
            for batch in batches
        )
        assert all(
            backend.ownership(runtime.store.get(batch)) == "gone"
            for batch in batches
            if runtime.store.get(batch) is not None
        )
        if evidence is not None and os.getenv("LIGHTRAG_INDEX_BUILD_VALIDATION_RECORD"):
            evidence["all_recorded_children_stopped"] = True
            path = Path(os.environ["LIGHTRAG_INDEX_BUILD_VALIDATION_RECORD"])
            if populated:
                path = path.with_stem(path.stem + "-populated")
            if retire_before_verify:
                path = path.with_stem(path.stem + "-gone")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2) + "\n")


def test_actual_inflight_embedding_cancel_restores_populated_source_without_replay(
    runtime_setup, tmp_path, canonical
):
    control, policy = runtime_setup
    backend = TransportBackend(tmp_path, hold=True)
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    evidence = None
    try:
        case = prepare_rebuild(
            control,
            policy,
            backend,
            runtime,
            seed=lambda batch: seed_source(control, batch, canonical),
        )
        captured = capture_source(case)
        advance(case, "stopping_source")
        runtime.stop(case.batch["id"])
        advance(case, "builder_starting")
        builder = BuilderRuntime(runtime)
        row = builder.start(case.batch["id"], case.operation)
        advance(case, "building", builder_lease=row)
        deadline = time.monotonic() + 30
        requests_file = tmp_path / "requests.jsonl"
        while not requests_file.is_file():
            assert backend.ownership(row) == "owned"
            assert time.monotonic() < deadline
            time.sleep(0.05)
        case.store.request_cancel(case.batch["id"], case.operation)
        while backend.ownership(row) != "gone":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        failed, _ = builder.inspect(case.batch["id"], case.operation)
        assert failed["state"] == "failed"
        from lightrag.batch_control.index_rebuild.worker import operation_directory

        root = operation_directory(
            control.paths, control.registry, case.batch["id"], case.operation
        )
        assert (root / "build" / "attempt.json").is_file()
        assert not (root / "build" / "verified.json").exists()
        requests = [json.loads(line) for line in requests_file.read_text().splitlines()]
        assert len(requests) == 1 and requests[0]["task"] == case.operation
        with pytest.raises(Conflict):
            BuilderRuntime(
                RuntimeSupervisor(control, policy, actor="user", backend=backend)
            ).start(case.batch["id"], case.operation)
        with QuotaStore(registry_path(control.paths)).database() as db:
            charges = [
                dict(value)
                for value in db.execute(
                    "SELECT batch,task,state,tokens,reason FROM provider_requests"
                )
            ]
        assert len(charges) == 1
        assert charges[0]["state"] == "finished" and charges[0]["tokens"] > 7
        assert charges[0]["reason"].startswith("Transport outcome unknown")
        advance(case, "rollback_stopping")
        advance(case, "rollback_starting")
        lease = runtime.start(
            case.batch["id"], case.batch["active"], rebuild_rollback_id=case.operation
        )
        advance(case, "rollback_verifying")
        proof = runtime._proof(runtime.store.get(case.batch["id"]))
        assert (
            proof["index_source_restored"]
            == captured["source_evidence"]["audit_sha256"]
        )
        result = case.store.restore(case.batch["id"], case.operation, lease, proof)
        assert result["state"] == "rolled_back"
        assert lease.collections == case.source_lease.collections
        assert control.registry.get(case.batch["id"])["pending"] is None
        assert (
            case.store.binding(case.batch["id"], case.operation)["state"] == "abandoned"
        )
        evidence = {
            "scope": "actual populated source restoration after in-flight private-SDK-transport cancellation",
            "source_evidence": captured["source_evidence"],
            "restored_lease": lease.model_dump(),
            "builder_exited": True,
            "no_verified_partial_receipt": True,
            "paid_attempts_not_replayed": True,
            "requests": requests,
            "charges": charges,
            "state": result["state"],
        }
    finally:
        with control.registry.transaction() as db:
            batches = [value[0] for value in db.execute("SELECT id FROM batches")]
        (tmp_path / "release").touch()
        for batch in batches:
            runtime.stop(batch)
        assert all(
            backend.ownership(runtime.store.get(batch)) == "gone"
            for batch in batches
            if runtime.store.get(batch) is not None
        )
        if evidence is not None and os.getenv("LIGHTRAG_INDEX_BUILD_VALIDATION_RECORD"):
            evidence["all_recorded_children_stopped"] = True
            path = Path(os.environ["LIGHTRAG_INDEX_BUILD_VALIDATION_RECORD"])
            path = path.with_stem(path.stem + "-cancelled")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2) + "\n")
