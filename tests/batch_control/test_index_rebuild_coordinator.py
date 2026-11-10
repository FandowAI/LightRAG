"""Actual API/coordinator-owned local rebuilds; SDK responses use private transports."""

# ruff: noqa: F811

import asyncio
import json
import os
from pathlib import Path
import time
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lightrag.batch_control.deployment import BatchDeployment
from lightrag.batch_control.index_rebuild.service import IndexRebuilds
from lightrag.batch_control.index_rebuild.coordinator import RebuildCoordinator
from lightrag.batch_control.routes import create_control_routes
from lightrag.batch_control.runtime import RuntimeSupervisor
from .test_index_build_runtime import TransportBackend, seed_source
from .test_index_rebuild_projection import canonical  # noqa: F401
from .test_runtime import runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_inflight,restart_coordinator",
    [(False, False), (True, False), (False, True)],
)
async def test_actual_api_and_automatic_coordinator_rebuild_or_restore_populated_source(
    runtime_setup, tmp_path, canonical, cancel_inflight, restart_coordinator
):
    control, policy = runtime_setup
    control.catalog.profiles["embed-v2"] = {
        "model_name": "new-offline-embedding",
        "api_url": "http://127.0.0.1:9/v1",
        "dimensions": 16,
    }
    backend = TransportBackend(tmp_path, hold=cancel_inflight or restart_coordinator)
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    peer = control.create("user", "Peer", "peer")
    batch = control.create("user", "Owned source", "source")
    seed_source(control, batch, canonical)
    queued = control.register_task(
        "user",
        batch["id"],
        batch["active"],
        "old-queued-query",
        "query",
        {"data_batch_id": batch["id"], "type": "index"},
        payload={"version": 1},
    )
    deployment = BatchDeployment(control, policy)
    deployment.dispatcher.backend = backend
    deployment.rebuild_coordinator.backend = backend
    coordinator = deployment.rebuild_coordinator
    app = FastAPI()
    app.include_router(create_control_routes(deployment, lambda: "user"))
    running = None
    evidence = None
    try:
        peer_lease = await asyncio.to_thread(runtime.start, peer["id"], peer["active"])
        async with AsyncClient(
            transport=ASGITransport(app), base_url="http://test"
        ) as client:
            base = f"/data-batches/{batch['id']}/index-rebuilds"
            response = await client.post(
                base,
                json={
                    "expected_revision": batch["active"],
                    "embedding_profile": "embed-v2",
                    "request_id": "api-rebuild",
                },
            )
            assert response.status_code == 202, response.text
            initial = response.json()
            operation = initial["operation_id"]
            status_url = initial["urls"]["status"]
            assert (
                await client.post(
                    f"/data-batches/{batch['id']}/api/query",
                    json={"query": "must not be admitted"},
                )
            ).status_code == 409
            # No explicit phase transitions: the same deployed coordinator drives every step.
            running = asyncio.create_task(coordinator.run())
            cancelled = False
            restarted = False
            observed = {initial["state"]}
            deadline = time.monotonic() + 120
            while True:
                public = (await client.get(status_url)).json()
                observed.add(public["state"])
                if public["terminal"]:
                    break
                assert public["state"] != "rollback_failed", public
                assert time.monotonic() < deadline, public
                if (
                    cancel_inflight
                    and not cancelled
                    and (tmp_path / "requests.jsonl").is_file()
                ):
                    cancelled_response = await client.post(initial["urls"]["cancel"])
                    assert cancelled_response.status_code == 202
                    assert cancelled_response.json()["cancel_requested"]
                    cancelled = True
                if (
                    restart_coordinator
                    and not restarted
                    and (tmp_path / "requests.jsonl").is_file()
                ):
                    before = runtime.store.get(batch["id"])
                    coordinator.stop_event.set()
                    await running
                    coordinator = RebuildCoordinator(control, policy, backend=backend)
                    running = asyncio.create_task(coordinator.run())
                    after = runtime.store.get(batch["id"])
                    assert {
                        key: after[key]
                        for key in ("generation", "lease_id", "pid", "process_identity")
                    } == {
                        key: before[key]
                        for key in ("generation", "lease_id", "pid", "process_identity")
                    }
                    (tmp_path / "release").touch()
                    restarted = True
                await asyncio.sleep(0.05)
            assert public["state"] == (
                "rolled_back" if cancel_inflight else "applied"
            ), public
            service = IndexRebuilds(control, policy, actor="user", backend=backend)
            record = service.store.get(batch["id"], operation)
            if restart_coordinator:
                assert restarted
                assert (
                    sum(
                        event["event"] == "builder_reserved"
                        for event in service.store.events(batch["id"], operation)
                    )
                    == 1
                )
            owner = control.registry.get(batch["id"])
            assert owner["pending"] is None
            assert owner["active"] == (
                batch["active"] if cancel_inflight else record["config_revision_id"]
            )
            lease = await asyncio.to_thread(runtime.inspect, batch["id"])
            assert lease is not None
            proof = await asyncio.to_thread(
                runtime._proof, runtime.store.get(batch["id"])
            )
            audit_sha = record["source_evidence"]["audit_sha256"]
            assert (
                proof["index_source_restored"] == audit_sha
                if cancel_inflight
                else proof["index_initial_verified"]["source_audit_sha256"] == audit_sha
            )
            assert await asyncio.to_thread(runtime.inspect, peer["id"]) == peer_lease
            requests = [
                json.loads(line)
                for line in (tmp_path / "requests.jsonl").read_text().splitlines()
            ]
            assert len(requests) == (1 if cancel_inflight else 3)
            assert {request["task"] for request in requests} == {operation}
            assert len(public["provider_admission"]["items"]) == 1
            events = (await client.get(initial["urls"]["events"])).json()["items"]
            phases = {event["state"] for event in events}
            assert {
                "draining",
                "source_capturing",
                "source_captured",
                "builder_starting",
                "building",
            } <= phases
            if not cancel_inflight:
                assert {
                    "stopping_builder",
                    "candidate_starting",
                    "candidate_verifying",
                    "applied",
                } <= phases
                assert public["progress"] == {
                    "state": "verified",
                    "total": 4,
                    "written": 4,
                    "verified": 4,
                }
                assert (
                    control.task("user", batch["id"], queued["task_id"])["state"]
                    == "interrupted"
                )
            else:
                assert {
                    "rollback_stopping",
                    "rollback_starting",
                    "rollback_verifying",
                    "rolled_back",
                } <= phases
                assert (
                    control.task("user", batch["id"], queued["task_id"])["state"]
                    == "queued"
                )
                assert lease.collections == record["source_lease"]["collections"]
            evidence = {
                "scope": "actual authenticated-route/automatic-coordinator flow with local stores and private SDK transports; not live model/database/deployment acceptance",
                "cancelled_inflight": cancel_inflight,
                "coordinator_reconstructed_during_build": restart_coordinator,
                "initial": initial,
                "terminal": public,
                "source_lease": record["source_lease"],
                "active_lease": lease.model_dump(),
                "peer_lease": peer_lease.model_dump(),
                "peer_unchanged": True,
                "source_evidence": record["source_evidence"],
                "observed_phases": sorted(observed),
                "journal_phases": sorted(phases),
                "embedding_requests": requests,
            }
    finally:
        (tmp_path / "release").touch()
        coordinator.stop_event.set()
        if running is not None:
            await running
        with control.registry.transaction() as db:
            batches = [row[0] for row in db.execute("SELECT id FROM batches")]
        for identity in batches:
            await asyncio.to_thread(runtime.stop, identity)
        assert all(
            backend.ownership(runtime.store.get(identity)) == "gone"
            for identity in batches
            if runtime.store.get(identity) is not None
        )
        if evidence is not None and os.getenv(
            "LIGHTRAG_INDEX_COORDINATOR_VALIDATION_RECORD"
        ):
            evidence["all_recorded_children_stopped"] = True
            path = Path(os.environ["LIGHTRAG_INDEX_COORDINATOR_VALIDATION_RECORD"])
            if cancel_inflight:
                path = path.with_stem(path.stem + "-cancelled")
            if restart_coordinator:
                path = path.with_stem(path.stem + "-reconstructed")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2) + "\n")


@pytest.mark.asyncio
async def test_coordinator_advances_batches_independently_and_joins_threads_on_close(
    factory, monkeypatch
):
    control = factory[0]()
    coordinator = RebuildCoordinator(control, SimpleNamespace(), backend=object())
    entered = {batch: threading.Event() for batch in ("a", "b")}
    release = threading.Event()
    calls = {batch: 0 for batch in entered}
    finished = set()

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def step(self, batch, operation):
            calls[batch] += 1
            entered[batch].set()
            assert release.wait(5)
            finished.add(batch)

    monkeypatch.setattr(
        "lightrag.batch_control.index_rebuild.coordinator.IndexRebuilds", Service
    )
    coordinator.store = SimpleNamespace(
        pending=lambda: [
            {"data_batch_id": batch, "operation_id": "op-" + batch, "actor": "user"}
            for batch in entered
            if batch not in finished
        ]
    )
    running = asyncio.create_task(coordinator.run())
    try:
        deadline = time.monotonic() + 3
        while not all(event.is_set() for event in entered.values()):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.6)
        assert calls == {"a": 1, "b": 1}
        coordinator.stop_event.set()
        await asyncio.sleep(0.02)
        assert not running.done()
    finally:
        coordinator.stop_event.set()
        release.set()
        await running
    assert finished == {"a", "b"} and not coordinator.running
