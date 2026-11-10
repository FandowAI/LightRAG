"""Prepare and start the accepted full WebUI/API candidate on test port 9631."""

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from dotenv import dotenv_values
import psutil
import yaml

from release.candidate9631 import verify_candidate

PROJECT = Path(__file__).resolve().parents[1]
SERVICE = PROJECT.parent / "test-services" / "lightrag-9631"
RELEASE = (
    PROJECT.parent
    / "config/validation/batch-flow-ui-20261105/release"
)
PORT = 9631
WORKSPACE = "h200_lightrag_test_9631"
LEGACY_BOOTSTRAP = "import sys; sys.path.insert(0,sys.argv.pop(1)); from lightrag.api.lightrag_server import main; main()"


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def require_test_roots(document):
    config = Path(document["config_root"])
    runtime = Path(document["runtime_root"])
    if (
        config != SERVICE / "config"
        or config.resolve() != config
        or not runtime.is_absolute()
        or runtime.resolve() != runtime
        or not runtime.is_relative_to(SERVICE)
    ):
        raise RuntimeError("9631 control roots must stay in its test-service directory")
    for source in document.get("legacy_sources", {}).values():
        url = urlsplit(source["url"])
        if (
            url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.port != PORT
            or source["workspace"] != WORKSPACE
        ):
            raise RuntimeError("9631 launcher cannot operate another legacy service")


def prepare(release=RELEASE, *, manager_only=False):
    from lightrag.integrations.data_workflow.config import load_settings

    values = {
        key: value
        for key, value in dotenv_values(PROJECT / ".env", interpolate=False).items()
        if value is not None
    }
    if not values.get("LIGHTRAG_API_KEY"):
        raise RuntimeError("Configure LIGHTRAG_API_KEY in LightRAG/.env first")
    installed = verify_candidate(PROJECT, release, manager_only=manager_only)
    bridge = load_settings(PROJECT.parent / "config/data_workflow.yaml")
    config = SERVICE / "batch_control.yaml"
    for name in ("documents", "query-results", "logs"):
        (SERVICE / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    recipe_file = SERVICE / "recipe.yaml"
    if not recipe_file.exists():
        recipe = yaml.safe_load(bridge.workflow_config.read_text())
        parameters = recipe["workflow"]["parameters"]
        for stage in ("scenario", "qa", "classification", "inspection"):
            source = parameters[stage].get("prompt_file")
            if source:
                content = (bridge.workflow_config.parent / source).read_text()
                target = (
                    SERVICE
                    / "config/prompts/defaults"
                    / (
                        stage
                        + "-"
                        + hashlib.sha256(content.encode()).hexdigest()
                        + ".md"
                    )
                )
                write_private(target, content)
                parameters[stage]["prompt_file"] = str(target)
        source = parameters.get("topic_catalog_file")
        if source:
            content = (bridge.workflow_config.parent / source).read_text()
            target = (
                SERVICE
                / "config/topics/defaults"
                / (hashlib.sha256(content.encode()).hexdigest() + ".yaml")
            )
            write_private(target, content)
            parameters["topic_catalog_file"] = str(target)
        write_private(
            recipe_file, yaml.safe_dump(recipe, allow_unicode=True, sort_keys=False)
        )
    if not config.exists():
        document = yaml.safe_load(
            (PROJECT / "config/batch_control.example.yaml").read_text()
        )

        def profile(prefix, *, embedding=False):
            key = prefix + "_BINDING_API_KEY"
            result = {
                "binding": values.get(prefix + "_BINDING", "openai"),
                "model_name": values[prefix + "_MODEL"],
                "api_url": values[prefix + "_BINDING_HOST"],
                "max_async": int(
                    values.get(
                        "EMBEDDING_FUNC_MAX_ASYNC" if embedding else "MAX_ASYNC_LLM",
                        "8",
                    )
                ),
                "timeout": int(
                    values.get(
                        "EMBEDDING_TIMEOUT" if embedding else "LLM_TIMEOUT", "180"
                    )
                ),
            }
            if values.get(key):
                result.update(
                    credential_id="system-model-keys", key_name_of_api_key=key
                )
            if embedding:
                result.update(
                    dimensions=int(values["EMBEDDING_DIM"]),
                    batch_size=int(values.get("EMBEDDING_BATCH_NUM", "16")),
                    max_tokens=int(values.get("EMBEDDING_TOKEN_LIMIT", "8192")),
                )
            return result

        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=bridge.project_directory, text=True
        ).strip()
        lock = hashlib.sha256(
            (bridge.project_directory / "uv.lock").read_bytes()
        ).hexdigest()
        document.update(
            config_root=str(SERVICE / "config"),
            runtime_root=str(SERVICE / "managed-runtime"),
            ui_directory=str(installed / "lightrag/api/webui"),
            recipe_file=str(recipe_file),
        )
        document["runtime"].update(
            project_directory=str(PROJECT),
            python=str(PROJECT / ".venv/bin/python"),
            storage_credentials_file=str(PROJECT / ".env"),
        )
        document["environments"] = {
            "workflow-v1": {
                "project_directory": str(bridge.project_directory),
                "python": str(bridge.python),
                "code_revision": commit,
                "dependency_revision": lock,
            }
        }
        document["credentials"] = {"system-model-keys": str(PROJECT / ".env")}
        if bridge.credentials_file:
            document["credentials"]["workflow-keys"] = str(bridge.credentials_file)
        else:
            document["bridge"]["credential_id"] = None
        document["inputs"] = {
            "documents": [str(SERVICE / "documents")],
            "query-results": [str(SERVICE / "query-results")],
        }
        document["profiles"] = {
            "chat-v1": profile("LLM"),
            "embedding-v1": profile("EMBEDDING", embedding=True),
        }
        document["rag"]["max_parallel_insert"] = int(
            values.get("MAX_PARALLEL_INSERT", "5")
        )
        document["rag"]["role_concurrency"] = {
            role: int(
                values.get(
                    role.upper() + "_MAX_ASYNC_LLM", values.get("MAX_ASYNC_LLM", "8")
                )
            )
            for role in ("query", "extract", "keyword")
        }
        write_private(
            config, yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        )
    document = yaml.safe_load(config.read_text())
    require_test_roots(document)
    changed = False
    if document.get("ui_directory") != str(installed / "lightrag/api/webui"):
        document["ui_directory"] = str(installed / "lightrag/api/webui")
        changed = True
    if document.get("recipe_file") == str(bridge.workflow_config) and document.get(
        "recipe_file"
    ) != str(recipe_file):
        document["recipe_file"] = str(recipe_file)
        changed = True
    # The full WebUI uses a login JWT; retain the separate API-key principal.
    # Only the explicitly configured admin account receives this test grant.
    login_names = {
        account.split(":", 1)[0].strip()
        for account in values.get("AUTH_ACCOUNTS", "").split(",")
        if ":" in account
    }
    admin_actor = "user-" + hashlib.sha256(b"admin").hexdigest()[:32]
    if "admin" in login_names and not any(
        grant["actor"] == admin_actor for grant in document["grants"]
    ):
        document["grants"].append(
            {"actor": admin_actor, "actions": ["*"], "resources": ["*"]}
        )
        changed = True
    if changed:
        previous = config.read_bytes()
        backup = (
            SERVICE
            / "config-history"
            / ("batch-control-" + hashlib.sha256(previous).hexdigest() + ".yaml")
        )
        if not backup.exists():
            write_private(backup, previous.decode())
        write_private(
            config, yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        )
    environment = dict(os.environ)
    environment.update(values)
    for key in list(environment):
        if key.endswith("_WORKSPACE"):
            environment.pop(key)
    environment.pop("GUNICORN_CMD_ARGS", None)
    environment.update(
        WORKSPACE=WORKSPACE,
        HOST="0.0.0.0",
        PORT=str(PORT),
        WORKERS="1",
        INPUT_DIR=str(SERVICE / "documents"),
        LOG_DIR=str(SERVICE / "logs"),
        LIGHTRAG_BATCH_CONTROL_CONFIG=str(config),
        LIGHTRAG_API_PREFIX="",
        LIGHTRAG_DATA_WORKFLOW_CONFIG=str(PROJECT.parent / "config/data_workflow.yaml"),
        PROMPT_DIR=str(PROJECT / "prompts"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    from lightrag.batch_control.deployment import load_deployment

    deployment = load_deployment(config)
    assert deployment.control.paths.config_root == SERVICE / "config"
    return installed, environment


def api(path, key):
    request = Request(f"http://127.0.0.1:{PORT}" + path, headers={"X-API-Key": key})
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        return json.load(response)


def require_test_adoption(environment):
    """Read the completed test binding without creating or opening any writer."""
    from lightrag.batch_control.resources import digest
    from lightrag.batch_control.writer_claim import managed_owner, read_json

    config = Path(environment["LIGHTRAG_BATCH_CONTROL_CONFIG"])
    if config.resolve() != SERVICE / "batch_control.yaml":
        raise RuntimeError("Manager launch requires the dedicated 9631 deployment")
    document = yaml.safe_load(config.read_text())
    require_test_roots(document)
    root = Path(document["runtime_root"])
    database = root / "batch-control/control.sqlite3"
    if database.resolve() != database:
        raise RuntimeError("9631 registry must not be redirected")
    if not database.is_file():
        raise RuntimeError(
            "Adopt the existing 9631 test workspace before starting its manager"
        )
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_bindings'"
        ).fetchone():
            raise RuntimeError(
                "Adopt the existing 9631 test workspace before starting its manager"
            )
        row = db.execute(
            "SELECT s.batch,s.digest,s.state,s.record FROM storage_bindings s "
            "JOIN batches b ON b.id=s.batch WHERE b.workspace=?",
            (WORKSPACE,),
        ).fetchone()
    if not row or row[2] != "adopted":
        raise RuntimeError("9631 test-workspace adoption has not completed")
    binding = json.loads(row[3])
    if binding.get("workspace") != WORKSPACE or digest(binding) != row[1]:
        raise RuntimeError("9631 adopted storage binding changed")
    owner = Path(binding["working_directory"]) / WORKSPACE / ".batch-owner.json"
    if read_json(owner) != managed_owner(row[0], database):
        raise RuntimeError("9631 test-workspace writer ownership changed")


def test_arguments(command):
    for flag, expected in (("--workspace", WORKSPACE), ("--port", str(PORT))):
        for index, argument in enumerate(command):
            if argument == flag:
                if index + 1 >= len(command) or command[index + 1] != expected:
                    return False
            elif argument.startswith(flag + "=") and argument != flag + "=" + expected:
                return False
    return True


def ensure_port(environment, restart):
    from lightrag.batch_control.runtime_process import process_scope

    process_scope()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", PORT))
        return
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise RuntimeError("Host port access is unavailable: " + str(exc)) from exc
    if not restart:
        raise RuntimeError(
            "Port 9631 is occupied; use --restart to replace its idle test service"
        )
    listeners = {
        row.pid
        for row in psutil.net_connections(kind="inet")
        if row.status == psutil.CONN_LISTEN and row.laddr.port == PORT and row.pid
    }
    if not listeners:
        raise RuntimeError("Cannot identify the port owner; no process stopped")
    processes = [psutil.Process(pid) for pid in listeners]
    for process in processes:
        command = process.cmdline()
        known = bool(command) and (
            Path(command[0]).name == "lightrag-server"
            or (
                len(command) > 1
                and not command[1].startswith("-")
                and Path(command[1]).name == "lightrag-server"
            )
            or command[1:3] == ["-m", "lightrag.api.lightrag_server"]
            or command[1:3] == ["-c", LEGACY_BOOTSTRAP]
        )
        if (
            process.environ().get("WORKSPACE") != WORKSPACE
            or not known
            or not test_arguments(command)
        ):
            raise RuntimeError(
                "Port owner is not the known 9631 test workspace; no process stopped"
            )
    health = api("/health", environment["LIGHTRAG_API_KEY"])
    if health.get("batch_only"):
        raise RuntimeError(
            "Drain and stop the existing batch manager before changing its candidate; no process stopped"
        )
    if health.get("configuration", {}).get("workspace") != WORKSPACE:
        raise RuntimeError(
            "9631 health does not identify the test workspace; no process stopped"
        )
    state = api("/documents/pipeline_status", environment["LIGHTRAG_API_KEY"])
    if any(state.get(key) for key in ("busy", "scanning", "pending_enqueues")):
        raise RuntimeError(
            "Test document processing is active; wait for it to finish before restarting"
        )
    try:
        batches = api("/data-batches", environment["LIGHTRAG_API_KEY"])["items"]
    except HTTPError as exc:
        if exc.code != 404:
            raise
        batches = []
    for batch in batches:
        tasks = api(
            "/data-batches/" + batch["id"] + "/tasks", environment["LIGHTRAG_API_KEY"]
        )["items"]
        if batch.get("pending") or any(not task["terminal"] for task in tasks):
            raise RuntimeError(
                "Managed batch work is pending; finish it before restarting"
            )
    offset = 0
    while True:
        try:
            jobs = api(
                f"/integrations/data-work-flow/jobs?limit=100&offset={offset}",
                environment["LIGHTRAG_API_KEY"],
            )
        except HTTPError as exc:
            if exc.code == 404:
                break
            raise
        if any(
            job["status"] in {"queued", "preparing", "running"} for job in jobs["items"]
        ):
            raise RuntimeError(
                "Test distillation is active; finish it before restarting"
            )
        if len(jobs["items"]) < 100:
            break
        offset += 100
    # The legacy retrieval API has no list endpoint; inspect only this test scope.
    for process in processes:
        working = Path(process.environ().get("WORKING_DIR", environment["WORKING_DIR"]))
        if not working.is_absolute():
            working = Path(process.cwd()) / working
        database = working / WORKSPACE / "batch_retrieval/jobs.sqlite3"
        if database.is_file():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone():
                    raise RuntimeError(
                        "Test retrieval is active; finish it before restarting"
                    )
    for process in processes:
        process.terminate()
    _, alive = psutil.wait_procs(processes, timeout=45)
    if alive:
        raise RuntimeError("Test service has not stopped; no forced kill performed")


def owns_port(pid):
    return any(
        row.pid == pid and row.status == psutil.CONN_LISTEN and row.laddr.port == PORT
        for row in psutil.net_connections(kind="inet")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--release-directory", type=Path, default=RELEASE)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--manager-only", action="store_true")
    args = parser.parse_args()
    # Shared API imports must not consume this launcher's CLI arguments.
    sys.argv = ["lightrag-server"]
    if args.verify_only:
        installed = verify_candidate(
            PROJECT, args.release_directory, manager_only=args.manager_only
        )
        print("Verified test candidate: " + str(installed))
        return
    installed, environment = prepare(
        args.release_directory, manager_only=args.manager_only
    )
    if args.prepare_only:
        print("Prepared test configuration: " + str(SERVICE / "batch_control.yaml"))
        return
    if args.manager_only:
        require_test_adoption(environment)
    ensure_port(environment, args.restart)
    command = [
        str(PROJECT / ".venv/bin/python"),
        "-c",
        LEGACY_BOOTSTRAP,
        str(installed),
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
        "--workspace",
        WORKSPACE,
    ]
    if args.manager_only:
        command = [
            str(PROJECT / ".venv/bin/python"),
            "-c",
            "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('lightrag.batch_control',run_name='__main__')",
            str(installed),
            "--deployment",
            environment["LIGHTRAG_BATCH_CONTROL_CONFIG"],
            "--ui-directory",
            str(installed / "lightrag/api/webui"),
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
        ]
    log = SERVICE / "logs/server.log"
    with log.open("ab") as stream:
        log.chmod(0o600)
        child = subprocess.Popen(
            command,
            cwd=PROJECT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    write_private(SERVICE / "server.pid", str(child.pid) + "\n")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Test service exited; inspect " + str(log))
        try:
            health = api("/health", environment["LIGHTRAG_API_KEY"])
            api("/data-batches/status", environment["LIGHTRAG_API_KEY"])
            if health.get("status") == "healthy" and owns_port(child.pid):
                if args.manager_only and not health.get("batch_only"):
                    raise RuntimeError("9631 is not serving the selected batch manager")
                from lightrag.batch_control.runtime_process import (
                    process_identity,
                    process_scope,
                )

                write_private(
                    SERVICE / "server-start.json",
                    json.dumps(
                        {
                            "port": PORT,
                            "workspace": WORKSPACE,
                            "pid": child.pid,
                            "process_identity": process_identity(child.pid),
                            "process_scope": process_scope(),
                            "release_directory": str(args.release_directory.resolve()),
                            "manager_only": args.manager_only,
                            "listener_verified": True,
                            "command_sha256": hashlib.sha256(
                                json.dumps(command).encode()
                            ).hexdigest(),
                        },
                        indent=2,
                    )
                    + "\n",
                )
                print("Test service started: http://10.10.115.253:9631/webui/")
                print("Batch settings: http://10.10.115.253:9631/batch-settings/")
                print("Logs: " + str(log))
                return
        except (OSError, HTTPError, URLError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Startup is still pending; inspect " + str(log))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
