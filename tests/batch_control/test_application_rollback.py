"""A failed replacement cannot displace the last verified batch configuration."""

# ruff: noqa: F811

import os
from pathlib import Path

import pytest

from lightrag.batch_control.applications import ApplicationService
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_process import ProcessBackend, atomic_json
from lightrag.batch_control.settings import SettingsService
from lightrag.batch_control.storage import Conflict
from lightrag.batch_control.http_store import HttpStore
from tests.batch_control.test_applications import setup, desired, application  # noqa: F401
from tests.batch_control.test_runtime import runtime_setup  # noqa: F401
from tests.batch_control.helpers import change

pytestmark = pytest.mark.offline


def test_apply_reconciles_http_owned_by_a_confirmed_lost_process(setup):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "a")
    lease = runtime.start(batch["id"], batch["active"])
    http = HttpStore(control.registry, control.paths)
    queued = http.submit(
        "user", batch["id"], lease, "POST", "/query", "", [], b"original request"
    )
    saved = desired(control, batch)
    service = application(setup, batch, saved)
    backend.alive[lease.pid] = False
    assert service.step(batch["id"], saved["operation"])["state"] == "applied"
    assert http.get(batch["id"], queued["id"])["state"] == "failed"
    assert http.active(batch["id"]) == []
    assert backend.launches == 2


def test_failed_replacement_restores_previous_revision_and_leaves_peer_running(setup):
    control, policy, backend, runtime = setup
    batch, peer = [control.create("user", "Batch", key) for key in ("a", "b")]
    original = runtime.start(batch["id"], batch["active"])
    other = runtime.start(peer["id"], peer["active"])
    saved = desired(control, batch)
    service = application(setup, batch, saved)
    backend.change = lambda proof: (
        {**proof, "workspace": "incorrect"}
        if proof["parameters"]["max_parallel_insert"] == 7
        else proof
    )
    result = service.step(batch["id"], saved["operation"])
    assert result["state"] == "rolled_back" and result["terminal"]
    restored = runtime.inspect(batch["id"])
    assert restored.generation == original.generation + 2
    assert restored.collections == original.collections
    owner = control.registry.get(batch["id"])
    assert (
        owner["active"] == batch["active"]
        and owner["pending"] is None
        and owner["desired"] is None
    )
    assert runtime.inspect(peer["id"]) == other
    assert backend.launches == 4
    assert backend.signals == [original.pid, original.pid + 2]
    assert (
        service.request(batch["id"], saved["operation"], saved["revision"], "apply")[
            "state"
        ]
        == "rolled_back"
    )
    with pytest.raises(Conflict):
        service.request(
            batch["id"], saved["operation"], saved["revision"], "new-request"
        )
    snapshot = SettingsService(control, policy, actor="user").get(batch["id"])
    assert snapshot["application"]["state"] == "rolled_back"
    assert snapshot["application"]["error"]
    assert snapshot["configuration"]["rag"]["max_parallel_insert"] == 3
    # A new ordinary request can use the previous revision again.
    assert runtime.start(batch["id"], batch["active"]) == restored


def test_unconfirmed_writer_stop_keeps_recovery_pending_and_launches_nothing(setup):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "a")
    original = runtime.start(batch["id"], batch["active"])
    saved = desired(control, batch)
    service = application(setup, batch, saved)
    terminate = backend.terminate
    backend.terminate = lambda *args: (_ for _ in ()).throw(
        TimeoutError("writer remains alive")
    )
    result = service.step(batch["id"], saved["operation"])
    assert result["state"] == "rollback_stopping" and not result["terminal"]
    assert backend.launches == 1 and backend.alive[original.pid]
    assert control.registry.get(batch["id"])["pending"] == saved["operation"]
    backend.terminate = terminate
    recovered = ApplicationService(
        control, service.policy, actor="user", backend=backend
    )
    assert recovered.step(batch["id"], saved["operation"])["state"] == "rolled_back"
    assert backend.launches == 2


def test_crash_after_rollback_start_recovers_the_same_owned_generation(setup):
    control, policy, backend, runtime = setup
    batch = control.create("user", "Batch", "a")
    runtime.start(batch["id"], batch["active"])
    saved = desired(control, batch)
    service = application(setup, batch, saved)
    runtime.stop(batch["id"])
    service.store.update(
        batch["id"],
        saved["operation"],
        event="rollback_requested",
        state="rollback_starting",
        error="Simulated replacement failure",
    )
    lease = runtime.start(batch["id"], batch["active"], rollback_id=saved["operation"])
    recovered = ApplicationService(control, policy, actor="user", backend=backend)
    result = recovered.step(batch["id"], saved["operation"])
    assert result["state"] == "rolled_back"
    assert result["rollback_lease"]["lease_id"] == lease.lease_id
    assert backend.launches == 2


def test_failed_previous_runtime_is_never_reported_as_restored(setup):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "a")
    runtime.start(batch["id"], batch["active"])
    saved = desired(control, batch)
    service = application(setup, batch, saved)
    initial_launches = backend.launches
    backend.change = lambda proof: (
        {**proof, "workspace": "incorrect"}
        if backend.launches > initial_launches
        else proof
    )
    result = service.step(batch["id"], saved["operation"])
    assert result["state"] == "rollback_failed" and result["terminal"]
    assert result["rollback_error"] and result["error"]
    assert "rollback_lease" not in result
    assert control.registry.get(batch["id"])["pending"] == saved["operation"]
    assert runtime.inspect(batch["id"]) is None


def test_arbitrary_rollback_identity_does_not_bypass_pending_admission(setup):
    control, _, backend, runtime = setup
    batch = control.create("user", "Batch", "a")
    with pytest.raises(Conflict):
        runtime.start(batch["id"], batch["active"], rollback_id="unknown-operation")
    saved = desired(control, batch)
    application(setup, batch, saved)
    with pytest.raises(Conflict):
        runtime.start(batch["id"], batch["active"], rollback_id=saved["operation"])
    assert backend.launches == 0


def test_real_workers_restore_previous_configuration_after_bad_readiness(runtime_setup):
    control, policy = runtime_setup
    batch, peer = [control.create("user", "Batch", key) for key in ("a", "b")]
    backend = ProcessBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    try:
        original = runtime.start(batch["id"], batch["active"])
        other = runtime.start(peer["id"], peer["active"])
        document = control.read("user", batch["id"])[2]
        saved = control.save(
            "user",
            batch["id"],
            batch["active"],
            change(document, "rag", max_parallel_insert=7),
            "save",
        )
        service = ApplicationService(control, policy, actor="user", backend=backend)
        service.request(batch["id"], saved["operation"], saved["revision"], "apply")
        observe = backend.observe

        def wrong_replacement(record):
            proof = observe(record)
            if proof and proof.get("parameters", {}).get("max_parallel_insert") == 7:
                return {**proof, "workspace": "incorrect"}
            return proof

        backend.observe = wrong_replacement
        result = service.step(batch["id"], saved["operation"])
        assert result["state"] == "rolled_back"
        restored = runtime.inspect(batch["id"])
        assert restored.generation == original.generation + 2
        assert restored.collections == original.collections
        assert runtime.inspect(peer["id"]) == other
        proof = observe(runtime.store.get(batch["id"]))
        assert proof["parameters"]["max_parallel_insert"] == 3
        assert control.registry.get(batch["id"])["pending"] is None
        evidence = {
            "schema_version": 1,
            "storage_mode": "local",
            "model_requests": 0,
            "application": result,
            "original": original.model_dump(),
            "peer": other.model_dump(),
            "restored": restored.model_dump(),
            "peer_unchanged": True,
            "observation": {
                key: value for key, value in proof.items() if key != "capability"
            },
        }
    finally:
        for owner in (batch, peer):
            record = runtime.store.get(owner["id"])
            if record and backend.ownership(record) == "owned":
                runtime.stop(owner["id"])
    evidence["children_stopped"] = all(
        backend.ownership(runtime.store.get(owner["id"])) == "gone"
        for owner in (batch, peer)
    )
    assert evidence["children_stopped"]
    if os.getenv("LIGHTRAG_ROLLBACK_VALIDATION_RECORD"):
        atomic_json(Path(os.environ["LIGHTRAG_ROLLBACK_VALIDATION_RECORD"]), evidence)
