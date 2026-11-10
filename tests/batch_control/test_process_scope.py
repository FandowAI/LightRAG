"""A foreign PID namespace cannot establish ownership, exit or released capacity."""

# ruff: noqa: F811

from dataclasses import replace
import json
import os

import psutil
import pytest

from lightrag.batch_control.runtime_process import (
    ProcessBackend,
    process_scope,
    scope_disposition,
)
from lightrag.batch_control.storage import Conflict, IntegrityError
from .test_runtime import managed, runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline


def foreign_scope(value):
    return {
        **value,
        "namespace": "pid:[99999999]",
        "procfs_namespace": "pid:[99999999]",
    }


@pytest.mark.parametrize(
    "kind", ["missing", "namespace", "machine", "procfs", "malformed_boot", "schema"]
)
def test_untrusted_scope_never_looks_up_or_signals_a_pid(monkeypatch, kind):
    scope = process_scope()
    if kind == "missing":
        scope = None
    elif kind == "namespace":
        scope = foreign_scope(scope)
    elif kind == "machine":
        scope = {**scope, "machine": "0" * 64}
    elif kind == "procfs":
        scope = {**scope, "procfs_namespace": "pid:[99999999]"}
    elif kind == "malformed_boot":
        scope = {**scope, "boot": "invalid"}
    else:
        scope = {**scope, "schema_version": True}
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda *args: pytest.fail(
            "A foreign PID must not be interpreted in this namespace"
        ),
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *args: pytest.fail("Untrusted scope must not authorize a signal"),
    )
    backend = ProcessBackend()
    record = {"pid": 2, "process_identity": "untrusted", "process_scope": scope}
    assert backend.ownership(record) == "unknown"
    with pytest.raises(RuntimeError, match="no signal"):
        backend.terminate(record, 0.01)


def test_confirmed_host_reboot_establishes_exit_without_inspecting_reused_pid(
    monkeypatch,
):
    scope = {**process_scope(), "boot": "00000000-0000-0000-0000-000000000000"}
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda *args: pytest.fail("Old-boot PID must not be inspected"),
    )
    monkeypatch.setattr(
        os, "killpg", lambda *args: pytest.fail("Old-boot PID must not be signalled")
    )
    assert scope_disposition(scope) == "rebooted"
    backend = ProcessBackend()
    record = {"process_scope": scope, "pid": 2}
    assert backend.ownership(record) == "gone"
    backend.terminate(record, 0.01)


def test_same_namespace_confirms_absence_and_unreadable_scope_remains_unknown(
    monkeypatch,
):
    scope = process_scope()

    def absent(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", absent)
    record = {"pid": 99999999, "process_scope": scope}
    assert ProcessBackend().ownership(record) == "gone"

    def unreadable():
        raise PermissionError("Procfs denied")

    monkeypatch.setattr(
        "lightrag.batch_control.runtime_process.process_scope", unreadable
    )
    assert ProcessBackend().ownership(record) == "unknown"


def test_foreign_scope_cannot_launch_before_any_child_is_created(monkeypatch):
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("Foreign scope launched a process"),
    )
    with pytest.raises(RuntimeError, match="reserved process scope"):
        ProcessBackend().launch({"process_scope": foreign_scope(process_scope())}, {})


def test_worker_attach_requires_the_reserved_scope(managed):
    control, runtime, _ = managed
    batch = control.create("user", "Scope", "scope")
    record = runtime.store.reserve(
        batch["id"], batch["active"], "a" * 64, "b" * 64, 2, "user"
    )
    record["process_scope"] = foreign_scope(record["process_scope"])
    with control.registry.transaction() as db:
        db.execute(
            "UPDATE runtimes SET record=? WHERE batch=?",
            (json.dumps(record), batch["id"]),
        )
    with pytest.raises(Conflict, match="does not own"):
        runtime.store.attach(
            batch["id"],
            record["generation"],
            record["startup_digest"],
            99999999,
            "foreign-birth",
        )
    assert runtime.store.get(batch["id"])["pid"] is None


def test_specification_and_observation_cannot_change_scope(managed):
    control, runtime, backend = managed
    batch = control.create("user", "Scope", "scope")
    lease = runtime.start(batch["id"], batch["active"])
    record = runtime.store.get(batch["id"])
    record["process_scope"] = foreign_scope(record["process_scope"])
    with control.registry.transaction() as db:
        db.execute(
            "UPDATE runtimes SET record=? WHERE batch=?",
            (json.dumps(record), batch["id"]),
        )
    with pytest.raises(IntegrityError, match="startup snapshot"):
        runtime.inspect(batch["id"])
    assert backend.signals == [] and backend.alive[lease.pid]


def test_terminal_status_cannot_replace_a_process_with_unconfirmed_exit(
    managed, monkeypatch
):
    control, runtime, backend = managed
    batch = control.create("user", "Scope", "scope")
    lease = runtime.start(batch["id"], batch["active"])
    runtime.store.update(
        batch["id"],
        lease.generation,
        {"ready"},
        "user",
        "test-untrusted-terminal-row",
        state="failed",
    )
    monkeypatch.setattr(backend, "ownership", lambda row: "unknown")
    with pytest.raises(Conflict, match="exit cannot be confirmed"):
        runtime.start(batch["id"], batch["active"])
    assert backend.launches == 1 and backend.signals == []


def test_uncertain_launch_without_a_pid_retains_its_slot_and_is_not_replayed(
    managed, monkeypatch
):
    control, runtime, backend = managed
    runtime.policy = replace(runtime.policy, max_processes=1)
    batch = control.create("user", "Scope", "scope")
    calls = []

    def uncertain(*args):
        calls.append("launch-intent")
        raise RuntimeError("Launch acknowledgement lost")

    monkeypatch.setattr(backend, "launch", uncertain)
    monkeypatch.setattr(backend, "ownership", lambda row: "unknown")
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        runtime.start(batch["id"], batch["active"])
    record = runtime.store.get(batch["id"])
    assert record["pid"] is None and record["state"] == "unknown"
    with pytest.raises(Conflict):
        runtime.start(batch["id"], batch["active"])
    other = control.create("user", "Other", "other")
    with pytest.raises(Conflict, match="capacity"):
        runtime.start(other["id"], other["active"])
    assert calls == ["launch-intent"] and backend.signals == []


def test_mounted_foreign_procfs_prevents_adoption_pid_inspection(monkeypatch):
    from lightrag.batch_control.adoption import SourceProcess

    real_readlink = os.readlink
    monkeypatch.setattr(
        os,
        "readlink",
        lambda path: (
            "pid:[99999999]" if path == "/proc/1/ns/pid" else real_readlink(path)
        ),
    )
    monkeypatch.setattr(
        psutil, "Process", lambda *args: pytest.fail("Foreign procfs PID lookup")
    )
    with pytest.raises(Conflict, match="cannot be inspected safely"):
        SourceProcess().stop(None, {})


def test_mounted_foreign_procfs_retains_provider_slot_and_refuses_new_owner(
    monkeypatch,
):
    from lightrag.batch_control.provider_quota.core import (
        QuotaError,
        owner_gone,
        process_owner,
    )

    owner = process_owner()
    real_readlink = os.readlink
    monkeypatch.setattr(
        os,
        "readlink",
        lambda path: (
            "pid:[99999999]" if path == "/proc/1/ns/pid" else real_readlink(path)
        ),
    )
    with pytest.raises(QuotaError, match="PID namespace"):
        process_owner()
    assert not owner_gone({**owner, "pid": 99999999})


def test_unreadable_procfs_never_releases_provider_slot(monkeypatch):
    from lightrag.batch_control.provider_quota.core import owner_gone, process_owner

    owner = process_owner()

    def denied(*args):
        raise PermissionError("Procfs denied")

    monkeypatch.setattr(os, "readlink", denied)
    assert not owner_gone({**owner, "pid": 99999999})
