"""A selected test candidate never authorizes foreign signals or hides old data."""

import errno
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest
import yaml

from lightrag.batch_control.resources import digest
from lightrag.batch_control.writer_claim import managed_owner

pytestmark = pytest.mark.offline


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    module = importlib.import_module("start_test_service_9631")
    project, service = tmp_path / "project", tmp_path / "test-service"
    project.mkdir()
    service.mkdir()
    monkeypatch.setattr(module, "PROJECT", project)
    monkeypatch.setattr(module, "SERVICE", service)
    return module


@pytest.fixture
def candidate(launcher, tmp_path):
    release = tmp_path / "release"
    installed = release / "installed"
    files = {
        "lightrag/__init__.py": "",
        "lightrag/batch_control/app.py": "value = 1\n",
        "lightrag/api/webui/index.html": "<html></html>",
        "lightrag/api/webui/assets/main.js": "export {}",
    }
    for name, content in files.items():
        path = installed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if name.endswith(".py"):
            source = launcher.PROJECT / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(content)
    wheel = release / "dist/candidate.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"test-wheel")
    record = {
        "schema_version": 1,
        "kind": "local_wheel_release_validation",
        "cli_help_verified": True,
        "wheel": str(wheel),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "verified_package_files": {
            name: hashlib.sha256(content.encode()).hexdigest()
            for name, content in files.items()
        },
        "installed_smoke": {
            "authenticated_api_status": 200,
            "html_status": 200,
            "manager_has_default_rag": False,
            "coordinators_closed": True,
            "workbench_html_status": 200,
            "manager_health_has_no_default_workspace": True,
            "legacy_workspace_html_unavailable": True,
        },
    }
    (release / "release.json").write_text(json.dumps(record))
    return release, installed, record


def test_verify_only_reads_candidate_without_preparing_or_contacting_service(
    launcher, candidate, monkeypatch
):
    release, installed, _ = candidate
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launcher",
            "--manager-only",
            "--verify-only",
            "--release-directory",
            str(release),
        ],
    )
    monkeypatch.setattr(
        launcher,
        "prepare",
        lambda *a, **kw: pytest.fail("Verification wrote configuration"),
    )
    monkeypatch.setattr(
        launcher,
        "ensure_port",
        lambda *a: pytest.fail("Verification inspected a live port"),
    )
    launcher.main()
    assert not list(launcher.SERVICE.iterdir())
    assert (
        launcher.verify_candidate(launcher.PROJECT, release, manager_only=True)
        == installed
    )


@pytest.mark.parametrize(
    "kind",
    [
        "wheel",
        "installed",
        "source",
        "extra_source",
        "extra_installed",
        "extra_ui",
        "missing_source",
        "escape",
        "smoke",
    ],
)
def test_candidate_integrity_changes_reject_before_service_work(
    launcher, candidate, kind
):
    release, installed, record = candidate
    if kind == "wheel":
        Path(record["wheel"]).write_bytes(b"changed")
    elif kind == "installed":
        (installed / "lightrag/__init__.py").write_text("changed")
    elif kind == "source":
        (launcher.PROJECT / "lightrag/__init__.py").write_text("changed")
    elif kind == "extra_source":
        (launcher.PROJECT / "lightrag/extra.py").write_text("")
    elif kind == "extra_installed":
        (installed / "lightrag/extra.py").write_text("")
    elif kind == "extra_ui":
        (installed / "lightrag/api/webui/extra.js").write_text("")
    elif kind == "missing_source":
        (launcher.PROJECT / "lightrag/__init__.py").unlink()
    elif kind == "escape":
        record["verified_package_files"]["../foreign.py"] = "a" * 64
    else:
        record["installed_smoke"]["manager_health_has_no_default_workspace"] = False
    (release / "release.json").write_text(json.dumps(record))
    with pytest.raises(RuntimeError):
        launcher.verify_candidate(launcher.PROJECT, release, manager_only=True)
    assert not list(launcher.SERVICE.iterdir())


@pytest.fixture
def adopted(launcher, tmp_path):
    runtime = launcher.SERVICE / "runtime"
    database = runtime / "batch-control/control.sqlite3"
    database.parent.mkdir(parents=True)
    config = launcher.SERVICE / "batch_control.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "runtime_root": str(runtime),
                "config_root": str(launcher.SERVICE / "config"),
            }
        )
    )
    environment = {
        "LIGHTRAG_BATCH_CONTROL_CONFIG": str(config),
        "LIGHTRAG_API_KEY": "test-key",
    }
    working = tmp_path / "storage"
    owner_file = working / launcher.WORKSPACE / ".batch-owner.json"
    owner_file.parent.mkdir(parents=True)
    owner_file.write_text(json.dumps(managed_owner("batch-test", database)))
    binding = {"workspace": launcher.WORKSPACE, "working_directory": str(working)}
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE batches (id TEXT PRIMARY KEY, workspace TEXT)")
        db.execute(
            "CREATE TABLE storage_bindings (batch TEXT PRIMARY KEY, digest TEXT, state TEXT, record TEXT)"
        )
        db.execute(
            "INSERT INTO batches VALUES (?,?)", ("batch-test", launcher.WORKSPACE)
        )
        db.execute(
            "INSERT INTO storage_bindings VALUES (?,?,?,?)",
            ("batch-test", digest(binding), "adopted", json.dumps(binding)),
        )
    return environment, database, owner_file


def test_completed_test_adoption_is_read_without_changing_registry_or_owner(
    launcher, adopted
):
    environment, database, owner = adopted
    before = database.read_bytes(), owner.read_bytes()
    launcher.require_test_adoption(environment)
    assert before == (database.read_bytes(), owner.read_bytes())


@pytest.mark.parametrize(
    "kind", ["registered", "wrong_workspace", "digest", "owner", "other_registry"]
)
def test_unfinished_or_foreign_test_binding_blocks_manager_start(
    launcher, adopted, kind
):
    environment, database, owner = adopted
    with sqlite3.connect(database) as db:
        if kind == "registered":
            db.execute("UPDATE storage_bindings SET state='registered'")
        elif kind == "wrong_workspace":
            db.execute("UPDATE batches SET workspace='main-workspace'")
        elif kind == "digest":
            db.execute("UPDATE storage_bindings SET digest='changed'")
        elif kind == "owner":
            owner.write_text(json.dumps(managed_owner("batch-other", database)))
        else:
            config = Path(environment["LIGHTRAG_BATCH_CONTROL_CONFIG"])
            config.write_text(
                yaml.safe_dump(
                    {
                        "runtime_root": str(launcher.PROJECT),
                        "config_root": str(launcher.SERVICE / "config"),
                    }
                )
            )
    with pytest.raises(RuntimeError):
        launcher.require_test_adoption(environment)


def test_manager_start_requires_completed_adoption_before_touching_port(
    launcher, adopted, candidate, monkeypatch
):
    environment, database, _ = adopted
    with sqlite3.connect(database) as db:
        db.execute("UPDATE storage_bindings SET state='registered'")
    monkeypatch.setattr(sys, "argv", ["launcher", "--manager-only"])
    monkeypatch.setattr(
        launcher, "prepare", lambda *a, **kw: (candidate[1], environment)
    )
    monkeypatch.setattr(
        launcher,
        "ensure_port",
        lambda *a: pytest.fail("Unfinished adoption touched port"),
    )
    with pytest.raises(RuntimeError, match="has not completed"):
        launcher.main()


@pytest.mark.parametrize("own_listener", [True, False])
def test_manager_command_uses_selected_package_and_only_reports_its_own_listener(
    launcher, adopted, candidate, monkeypatch, own_listener
):
    environment, _, _ = adopted
    release, installed, _ = candidate
    (launcher.SERVICE / "logs").mkdir()
    launched, calls = [], []
    polls = iter([None] if own_listener else [None, 2])
    monkeypatch.setattr(
        sys, "argv", ["launcher", "--manager-only", "--release-directory", str(release)]
    )
    monkeypatch.setattr(launcher, "prepare", lambda *a, **kw: (installed, environment))
    monkeypatch.setattr(launcher, "ensure_port", lambda *a: None)
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda command, **kw: (
            launched.append(command)
            or SimpleNamespace(pid=os.getpid(), poll=lambda: next(polls))
        ),
    )
    monkeypatch.setattr(launcher, "owns_port", lambda pid: own_listener)
    monkeypatch.setattr(
        launcher,
        "api",
        lambda path, key: (
            calls.append(path) or {"status": "healthy", "batch_only": True}
        ),
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    if own_listener:
        launcher.main()
        receipt = json.loads((launcher.SERVICE / "server-start.json").read_text())
        assert (
            receipt["port"] == 9631
            and receipt["manager_only"]
            and receipt["listener_verified"]
        )
        assert (
            receipt["process_scope"]["namespace"]
            == receipt["process_scope"]["procfs_namespace"]
        )
    else:
        with pytest.raises(RuntimeError, match="exited"):
            launcher.main()
        assert not (launcher.SERVICE / "server-start.json").exists()
    command = launched[0]
    assert str(installed) in command and "--deployment" in command
    assert "--workspace" not in command
    assert command[command.index("--port") + 1] == "9631"
    assert command[command.index("--ui-directory") + 1] == str(
        installed / "lightrag/api/webui"
    )
    assert set(calls) == {"/health", "/data-batches/status"}


def test_port_probe_denied_never_inspects_or_signals_a_process(launcher, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EPERM, "blocked")

    monkeypatch.setattr(launcher.socket, "socket", denied)
    monkeypatch.setattr(
        launcher.psutil,
        "Process",
        lambda *a: pytest.fail("Denied port looked up a process"),
    )
    with pytest.raises(RuntimeError, match="Host port access"):
        launcher.ensure_port({}, True)


@pytest.fixture
def occupied(launcher, monkeypatch):
    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            assert address == ("0.0.0.0", 9631)
            raise OSError(errno.EADDRINUSE, "occupied")

    signals = []
    process = SimpleNamespace(
        pid=123,
        environ=lambda: {
            "WORKSPACE": launcher.WORKSPACE,
            "WORKING_DIR": str(launcher.SERVICE),
        },
        cmdline=lambda: ["python", "-m", "lightrag.api.lightrag_server"],
        cwd=lambda: str(launcher.PROJECT),
        terminate=lambda: signals.append(123),
    )
    monkeypatch.setattr(launcher.socket, "socket", lambda *a: Probe())
    monkeypatch.setattr(
        launcher.psutil,
        "net_connections",
        lambda **kw: [
            SimpleNamespace(
                pid=123,
                status=launcher.psutil.CONN_LISTEN,
                laddr=SimpleNamespace(port=9631),
            )
        ],
    )
    monkeypatch.setattr(launcher.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(
        launcher.psutil, "wait_procs", lambda processes, timeout: (processes, [])
    )
    return process, signals


def test_foreign_workspace_listener_never_authorizes_a_signal(
    launcher, occupied, monkeypatch
):
    process, signals = occupied
    process.environ = lambda: {"WORKSPACE": "main-workspace"}
    monkeypatch.setattr(
        launcher,
        "api",
        lambda *a: pytest.fail("Foreign listener received an API request"),
    )
    with pytest.raises(RuntimeError, match="not the known 9631"):
        launcher.ensure_port({}, True)
    assert signals == []


def test_existing_manager_is_not_terminated_by_legacy_restart(
    launcher, occupied, monkeypatch
):
    _, signals = occupied
    monkeypatch.setattr(launcher, "api", lambda *a: {"batch_only": True})
    with pytest.raises(RuntimeError, match="existing batch manager"):
        launcher.ensure_port({"LIGHTRAG_API_KEY": "test"}, True)
    assert signals == []


def test_idle_known_legacy_test_listener_is_the_only_signalled_process(
    launcher, occupied, monkeypatch
):
    _, signals = occupied
    paths = []

    def api(path, key):
        paths.append(path)
        if path == "/health":
            return {
                "status": "healthy",
                "configuration": {"workspace": launcher.WORKSPACE},
            }
        if path == "/documents/pipeline_status":
            return {"busy": False}
        return {"items": []}

    monkeypatch.setattr(launcher, "api", api)
    launcher.ensure_port(
        {"LIGHTRAG_API_KEY": "test", "WORKING_DIR": str(launcher.SERVICE)}, True
    )
    assert signals == [123]
    assert paths == [
        "/health",
        "/documents/pipeline_status",
        "/data-batches",
        "/integrations/data-work-flow/jobs?limit=100&offset=0",
    ]


def test_a_word_match_is_not_a_known_test_server_command(
    launcher, occupied, monkeypatch
):
    process, signals = occupied
    process.cmdline = lambda: ["python", "-m", "lightrag.embedding_service"]
    monkeypatch.setattr(
        launcher,
        "api",
        lambda *a: pytest.fail("Unknown command received an API request"),
    )
    with pytest.raises(RuntimeError, match="not the known 9631"):
        launcher.ensure_port({}, True)
    assert signals == []


def test_selected_ui_updates_only_test_configuration_and_preserves_backup(
    launcher, candidate, monkeypatch
):
    from lightrag.integrations.data_workflow import config as bridge_config
    from lightrag.batch_control import deployment

    release, installed, _ = candidate
    (launcher.PROJECT / ".env").write_text("LIGHTRAG_API_KEY=test\n")
    recipe = launcher.SERVICE / "recipe.yaml"
    recipe.write_text("schema_version: 3\n")
    config = launcher.SERVICE / "batch_control.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "ui_directory": "old-ui",
                "config_root": str(launcher.SERVICE / "config"),
                "runtime_root": str(launcher.SERVICE / "runtime"),
                "recipe_file": str(recipe),
                "grants": [],
                "preserved": "original",
            }
        )
    )
    original = config.read_bytes()
    monkeypatch.setattr(
        bridge_config,
        "load_settings",
        lambda *a: SimpleNamespace(workflow_config=recipe),
    )
    monkeypatch.setattr(
        deployment,
        "load_deployment",
        lambda *a: SimpleNamespace(
            control=SimpleNamespace(
                paths=SimpleNamespace(config_root=launcher.SERVICE / "config")
            )
        ),
    )
    actual, environment = launcher.prepare(release, manager_only=True)
    assert actual == installed
    assert (
        environment["PORT"] == "9631" and environment["WORKSPACE"] == launcher.WORKSPACE
    )
    updated = yaml.safe_load(config.read_text())
    assert updated["ui_directory"] == str(installed / "lightrag/api/webui")
    assert updated["preserved"] == "original"
    backups = list((launcher.SERVICE / "config-history").glob("*.yaml"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    launcher.prepare(release, manager_only=True)
    assert list((launcher.SERVICE / "config-history").glob("*.yaml")) == backups


@pytest.mark.parametrize(
    "kind", ["config_root", "runtime_root", "source_port", "source_workspace"]
)
def test_dedicated_launcher_rejects_main_service_roots_and_sources(launcher, kind):
    config = {
        "config_root": str(launcher.SERVICE / "config"),
        "runtime_root": str(launcher.SERVICE / "runtime"),
        "legacy_sources": {
            "old-test": {
                "url": "http://127.0.0.1:9631",
                "workspace": launcher.WORKSPACE,
            }
        },
    }
    if kind in {"config_root", "runtime_root"}:
        config[kind] = str(launcher.PROJECT / "primary")
    elif kind == "source_port":
        config["legacy_sources"]["old-test"]["url"] = "http://127.0.0.1:9621"
    else:
        config["legacy_sources"]["old-test"]["workspace"] = "main-workspace"
    with pytest.raises(RuntimeError):
        launcher.require_test_roots(config)
    assert not list(launcher.SERVICE.iterdir())


@pytest.mark.parametrize(
    "arguments",
    [
        ["--workspace", "main-workspace"],
        ["--workspace=main-workspace"],
        ["--port=9621"],
        ["--workspace"],
    ],
)
def test_command_line_overrides_cannot_hide_behind_test_environment(
    launcher, occupied, monkeypatch, arguments
):
    process, signals = occupied
    process.cmdline = lambda: [
        "python",
        "-m",
        "lightrag.api.lightrag_server",
        *arguments,
    ]
    monkeypatch.setattr(
        launcher,
        "api",
        lambda *a: pytest.fail("Foreign CLI scope received an API request"),
    )
    with pytest.raises(RuntimeError, match="not the known 9631"):
        launcher.ensure_port({}, True)
    assert signals == []


def test_legacy_health_must_confirm_test_workspace_before_any_signal(
    launcher, occupied, monkeypatch
):
    _, signals = occupied
    monkeypatch.setattr(
        launcher, "api", lambda *a: {"configuration": {"workspace": "main-workspace"}}
    )
    with pytest.raises(RuntimeError, match="health does not identify"):
        launcher.ensure_port({"LIGHTRAG_API_KEY": "test"}, True)
    assert signals == []
