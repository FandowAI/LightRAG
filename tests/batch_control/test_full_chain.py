"""Exercise frozen commands, business-stage adapters, CLI receipts and scoped APIs."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import yaml
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from lightrag.batch_control.chain_service import ChainService, BatchDispatcher
from lightrag.batch_control.contracts import Environment
from lightrag.batch_control.deployment import BatchDeployment
from lightrag.batch_control.routes import create_control_routes
from lightrag.batch_control.routes import authenticated_principal
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_config import RuntimePolicy
from lightrag.batch_control.runtime_process import atomic_json
from lightrag.batch_control.storage import Conflict, IntegrityError
from lightrag.batch_control.task_contracts import TaskCommand
from lightrag.batch_control.task_runner import TaskEngine
from lightrag.batch_control.task_store import TaskStore
from tests.batch_control.helpers import change
from tests.batch_control.test_runtime import FakeBackend

pytestmark = pytest.mark.offline

CLI_FIXTURE = """
import json, sys, os
from pathlib import Path
import yaml
args = sys.argv
config = yaml.safe_load(Path(args[args.index('--config')+1]).read_text())
assert 'BATCH_SECRET_MUST_NOT_LEAK' not in os.environ
rows = [json.loads(x) for x in Path(config['input']['paths'][0]).read_text().splitlines()]
assert rows and all(set(row) == {'text'} for row in rows)
assert all(set(json.loads(row['text'])) == {'entities','relationships','text_chunks'} for row in rows)
directory = Path(config['output']['directory']) / 'run-fixture'
directory.mkdir(parents=True)
receipt = {'run_id': directory.name, 'directory': str(directory), 'status': 'completed', 'summary': {'sources': len(rows), 'qa': len(rows)*3}}
(directory / 'run.json').write_text(json.dumps(receipt))
(directory / 'qa.jsonl').write_text(''.join(json.dumps({'question': 'fixture', 'answer': 'fixture'})+'\\n' for row in rows))
print(json.dumps(receipt))
"""


@pytest.fixture
def chain_setup(factory):
    catalog = factory[3]
    project = Path(catalog.environments["test"].project_directory)
    package = project / "src/data_studio"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "__main__.py").write_text(CLI_FIXTURE)
    (project / "uv.lock").write_text("offline fixture dependency pin")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OfflineTest",
            "-c",
            "user.email=offline@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "Offline fixture",
        ],
        cwd=project,
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip()
    catalog.environments["test"] = Environment(
        str(project), sys.executable, commit, "fixture-lock"
    )
    catalog.profiles["llm-v1"] = {
        "model_name": "offline-chat",
        "api_url": "http://127.0.0.1:9/v1",
    }
    catalog.profiles["embed-v1"] = {
        "model_name": "offline-embedding",
        "api_url": "http://127.0.0.1:9/v1",
        "dimensions": 8,
    }
    documents = project.parent / "documents"
    documents.mkdir()
    (documents / "policy.md").write_text(
        "ALPHA requires invoice approval. OFFLINE_SOURCE_ONLY."
    )
    results = project.parent / "results"
    results.mkdir()
    query = {
        "status": "success",
        "data": {
            "entities": [],
            "relationships": [],
            "chunks": [{"content": "Original frozen evidence."}],
        },
    }
    (results / "source.json").write_text(json.dumps(query))
    catalog.inputs.update(documents=[documents], results=[results])
    defaults = change(factory[2], "bridge", input_ids=["documents", "results"])
    defaults = change(
        defaults,
        "retrieval",
        batch={"graph_depth": 2, "rag_top_k": 10, "near_dedup": False},
    )
    control = factory[0](defaults_override=defaults)
    probe = control.probe
    control.probe = lambda settings, operation, recipe=None: {
        **probe(settings, operation, recipe),
        **({"installed_commit": commit} if operation == "describe" else {}),
    }
    policy = RuntimePolicy(
        Path(__file__).resolve().parents[2],
        Path(sys.executable),
        storage_mode="local",
        startup_timeout=60,
        stop_timeout=15,
        heartbeat_seconds=0.1,
    )
    return control, policy, documents, results


def command(batch, *, kind="chain", key="button", **updates):
    source = (
        {
            "data_batch_id": batch["id"],
            "type": "directory",
            "input_ids": ["results" if kind == "distillation" else "documents"],
        }
        if kind in {"ingest", "chain", "distillation"}
        else {"data_batch_id": batch["id"], "type": "index"}
    )
    body = {
        "expected_revision": batch["active"],
        "request_id": key,
        "kind": kind,
        "source": source,
    }
    if kind in {"chain", "retrieval"}:
        body["centers"] = ["ALPHA"]
    body.update(updates)
    return TaskCommand.model_validate(body)


def prepared(chain_setup, monkeypatch, *, kind="chain"):
    control, policy, *_ = chain_setup
    monkeypatch.setattr(
        "lightrag.batch_control.runtime_config.selected_fingerprint",
        lambda policy: {"source": "unit"},
    )
    batch = control.create("user", "Batch", "create")
    service = ChainService(control, policy, actor="user")
    task = service.submit(batch["id"], command(batch, kind=kind))
    backend = FakeBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    lease = runtime.start(batch["id"], batch["active"])
    store = TaskStore(control.registry)
    store.admit(batch["id"], task["task_id"], lease)
    record = store.claim(
        batch["id"],
        lease.generation,
        lease.startup_digest,
        lease.pid,
        lease.process_identity,
    )
    return service, batch, record, backend.specs[lease.pid], store


def test_submission_is_atomic_idempotent_and_never_refreezes_changed_inputs(
    chain_setup,
):
    control, policy, documents, _ = chain_setup
    batch = control.create("user", "Batch", "create")
    service = ChainService(control, policy, actor="user")
    request = command(batch, kind="ingest")
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(lambda _: service.submit(batch["id"], request), range(2)))
    assert tasks[0]["task_id"] == tasks[1]["task_id"]
    (documents / "policy.md").unlink()
    assert service.submit(batch["id"], request)["task_id"] == tasks[0]["task_id"]
    assert len(service.list(batch["id"])) == 1
    with pytest.raises(Conflict):
        service.submit(batch["id"], command(batch, kind="retrieval"))


def test_admission_and_claim_are_generation_bound_and_cancelled_work_never_runs(
    chain_setup, monkeypatch
):
    service, batch, task, spec, store = prepared(chain_setup, monkeypatch)
    assert (
        store.claim(
            batch["id"],
            999,
            spec["startup_digest"],
            spec["pid"],
            spec["process_identity"],
        )
        is None
    )
    assert store.get(batch["id"], task["task_id"])["state"] == "running"
    second = service.submit(batch["id"], command(batch, kind="ingest", key="second"))
    cancelled = service.cancel(batch["id"], second["task_id"])
    assert cancelled["state"] == "cancelled" and cancelled["terminal"]
    assert (
        store.events(batch["id"], second["task_id"])[-1]["event"] == "cancel_requested"
    )


def test_command_schema_rejects_cross_stage_arguments(chain_setup):
    control = chain_setup[0]
    batch = control.create("user", "Batch", "create")
    with pytest.raises(ValueError, match="explicitly"):
        command(batch, centers=[])
    with pytest.raises(ValueError, match="Source type"):
        command(
            batch,
            kind="retrieval",
            source={
                "data_batch_id": batch["id"],
                "type": "directory",
                "input_ids": ["documents"],
            },
        )


def test_source_drift_and_foreign_inputs_are_rejected(chain_setup):
    control, policy, documents, _ = chain_setup
    batch = control.create("user", "Batch", "create")
    service = ChainService(control, policy, actor="user")
    control.catalog.inputs["documents"] = [documents.parent]
    with pytest.raises(IntegrityError, match="changed"):
        service.submit(batch["id"], command(batch, kind="ingest"))
    with pytest.raises(ValueError, match="another data batch"):
        service.submit(
            batch["id"],
            command(
                batch,
                source={
                    "data_batch_id": "batch-other",
                    "type": "directory",
                    "input_ids": ["documents"],
                },
            ),
        )


def test_oversized_supported_input_rejects_the_whole_submission(chain_setup):
    from fastapi import HTTPException

    control, policy, documents, _ = chain_setup
    (documents / "small.md").write_text("ok")
    (documents / "large.md").write_text("x" * 200)
    control.defaults = change(control.defaults, "bridge", max_input_bytes=100)
    batch = control.create("user", "Batch", "create")
    service = ChainService(control, policy, actor="user")
    with pytest.raises(HTTPException) as failure:
        service.submit(batch["id"], command(batch, kind="ingest"))
    assert failure.value.status_code == 413
    assert service.list(batch["id"]) == []


@pytest.mark.asyncio
async def test_directory_distillation_runs_real_cli_fixture_with_frozen_recipe(
    chain_setup, monkeypatch
):
    service, batch, task, spec, store = prepared(
        chain_setup, monkeypatch, kind="distillation"
    )
    monkeypatch.setenv("BATCH_SECRET_MUST_NOT_LEAK", "private-value")
    _, _, old, _, _ = service.control.read("user", batch["id"])
    edited = old.model_dump()
    edited["recipe"]["workflow"]["parameters"]["questions_per_source"] = 9
    from lightrag.batch_control.contracts import Configuration

    service.control.save(
        "user",
        batch["id"],
        batch["active"],
        Configuration.model_validate(edited),
        "new-recipe",
    )
    app = FastAPI()
    app.state.rag = SimpleNamespace(workspace=batch["workspace"])
    await asyncio.wait_for(TaskEngine(app, spec, task).execute(), timeout=15)
    result = service.get(batch["id"], task["task_id"])
    assert result["state"] == "completed", result
    workflow = result["result"]["distillation"]
    assert workflow["progress"]["accepted_sources"] == 1
    rows = [
        json.loads(row)
        for row in Path(workflow["artifacts"]["input"]).read_text().splitlines()
    ]
    assert set(rows[0]) == {"text"}
    assert set(json.loads(rows[0]["text"])) == {
        "entities",
        "relationships",
        "text_chunks",
    }
    assert workflow["result"]["summary"]["sources"] == 1
    assert service.artifact_path(
        batch["id"], task["task_id"], "workflow-input"
    ).is_file()
    assert (
        service.artifact_path(batch["id"], task["task_id"], "workflow-run").name
        == "run.json"
    )
    assert (
        yaml.safe_load(Path(workflow["artifacts"]["config"]).read_text())["workflow"][
            "parameters"
        ]["questions_per_source"]
        == 3
    )
    assert service.result_path(batch["id"], task["task_id"]).is_file()
    assert store.events(batch["id"], task["task_id"])[-1]["event"] == "finished"


@pytest.mark.asyncio
async def test_changed_file_fails_before_workflow_execution(chain_setup, monkeypatch):
    service, batch, task, spec, _ = prepared(
        chain_setup, monkeypatch, kind="distillation"
    )
    (chain_setup[3] / "source.json").write_text("changed")
    app = FastAPI()
    app.state.rag = SimpleNamespace(workspace=batch["workspace"])
    await asyncio.wait_for(TaskEngine(app, spec, task).execute(), timeout=15)
    assert service.get(batch["id"], task["task_id"])["state"] == "failed"
    assert not list(service.control.paths.runtime_root.rglob("stdout.log"))


@pytest.mark.asyncio
async def test_headless_routes_keep_submission_and_progress_separate(chain_setup):
    control, policy, *_ = chain_setup
    app = FastAPI(root_path="/test")
    deployment = BatchDeployment(control, policy)
    app.include_router(create_control_routes(deployment, lambda: "user"))
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/data-batches", json={"name": "Batch", "request_id": "new"}
        )
        assert created.status_code == 201
        batch = created.json()
        start = await client.post(
            f"/data-batches/{batch['id']}/tasks",
            json=command(batch, kind="ingest").model_dump(),
        )
        assert start.status_code == 202
        body = start.json()
        assert body["state"] == "queued" and body["poll_after_ms"] == 2000
        assert body["links"]["status"].startswith("/test/data-batches/")
        task = body["task_id"]
        status = await client.get(f"/data-batches/{batch['id']}/tasks/{task}")
        assert status.status_code == 200
        assert (
            await client.get(f"/data-batches/{batch['id']}/tasks/{task}/result")
        ).status_code == 409
        cancelled = await client.post(
            f"/data-batches/{batch['id']}/tasks/{task}/cancel"
        )
        assert cancelled.json()["state"] == "cancelled"
        other = control.create("user", "Other", "other")
        assert (
            await client.get(f"/data-batches/{other['id']}/tasks/{task}")
        ).status_code == 404


def test_existing_runtime_exit_interrupts_tasks_without_replaying_paid_work(
    chain_setup, monkeypatch
):
    service, batch, task, spec, store = prepared(chain_setup, monkeypatch)
    service.runtime.store.update(
        batch["id"], spec["generation"], {"ready"}, "user", "test_exit", state="failed"
    )
    BatchDispatcher(service.control, service.policy).tick()
    assert store.get(batch["id"], task["task_id"])["state"] == "interrupted"
    BatchDispatcher(service.control, service.policy).tick()
    assert store.get(batch["id"], task["task_id"])["state"] == "interrupted"


def test_real_workers_consume_durable_tasks_and_keep_batches_isolated(chain_setup):
    control, policy, *_ = chain_setup
    batches = [control.create("user", "Batch", key) for key in ("first", "second")]
    service = ChainService(control, policy, actor="user")
    tasks = [
        service.submit(batch["id"], command(batch, kind="distillation"))
        for batch in batches
    ]
    dispatcher = BatchDispatcher(control, policy)
    try:
        dispatcher.tick()
        # Reconstruct the manager while the durable tasks retain their ownership.
        recovered = BatchDispatcher(control, policy)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            records = [
                service.get(batch["id"], task["task_id"])
                for batch, task in zip(batches, tasks)
            ]
            if all(record["terminal"] for record in records):
                break
            recovered.tick()
            time.sleep(0.1)
        assert all(record["state"] == "completed" for record in records), records
        directories = [
            record["result"]["distillation"]["artifacts"]["directory"]
            for record in records
        ]
        assert directories[0] != directories[1]
        assert all(
            record["result"]["distillation"]["progress"]["accepted_sources"] == 1
            for record in records
        )
        leases = [service.runtime.inspect(batch["id"]) for batch in batches]
        assert leases[0].pid != leases[1].pid
        service.runtime.stop(batches[0]["id"])
        assert service.runtime.inspect(batches[1]["id"]) is not None
        archive = os.getenv("LIGHTRAG_CHAIN_WORKER_RECORD")
        if archive:
            atomic_json(
                Path(archive),
                {
                    "schema_version": 1,
                    "kind": "real_rag_workers_with_directory_distillation_cli_protocol_fixture",
                    "tasks": records,
                    "leases": [lease.model_dump() for lease in leases],
                    "manager_reconstruction_verified": True,
                    "independent_stop_verified": True,
                    "external_model_requests": 0,
                },
            )
    finally:
        for batch in batches:
            service.runtime.stop(batch["id"])
    if archive:
        proof = json.loads(Path(archive).read_text())
        proof["all_children_stopped"] = all(
            service.runtime.store.get(batch["id"])["state"] == "stopped"
            for batch in batches
        )
        atomic_json(Path(archive), proof)


@pytest.mark.asyncio
async def test_batch_auth_never_accepts_whitelist_or_anonymous_guest(
    chain_setup, monkeypatch
):
    from lightrag.api import utils_api

    monkeypatch.setattr(utils_api, "auth_configured", False)
    monkeypatch.setattr(utils_api, "path_is_whitelisted", lambda scope: True)
    app = FastAPI()
    app.include_router(
        create_control_routes(
            BatchDeployment(*chain_setup[:2]),
            authenticated_principal("test-private-key"),
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        body = {"name": "Batch", "request_id": "click"}
        assert (await client.post("/data-batches", json=body)).status_code == 403
        assert (
            await client.post(
                "/data-batches", json=body, headers={"X-API-Key": "wrong"}
            )
        ).status_code == 403
        assert (
            await client.post(
                "/data-batches", json=body, headers={"X-API-Key": "test-private-key"}
            )
        ).status_code == 201


@pytest.mark.asyncio
async def test_denied_submit_and_cancel_permissions_are_enforced(chain_setup):
    control, policy, *_ = chain_setup
    batch = control.create("user", "Batch", "create")
    service = ChainService(control, policy, actor="user")
    control.authorize = lambda actor, action, resource: action != "run_runtime"
    with pytest.raises(PermissionError):
        service.submit(batch["id"], command(batch, kind="ingest"))
    control.authorize = lambda actor, action, resource: action != "cancel_task"
    task = service.submit(batch["id"], command(batch, kind="ingest"))
    with pytest.raises(PermissionError):
        service.cancel(batch["id"], task["task_id"])


@pytest.mark.asyncio
async def test_retrieval_source_requires_matching_batch_and_untampered_publication(
    chain_setup,
):
    from lightrag.batch_retrieval import BatchManager
    from lightrag.batch_control.chain_service import retrieval_root
    from lightrag.entity_packages import PackageSettings
    from tests.extraction.test_batch_retrieval import package

    control, policy, *_ = chain_setup
    a, b = [control.create("user", "Batch", key) for key in ("a", "b")]

    async def retrieve(rag, center, settings):
        return package(center)

    manager = BatchManager(
        SimpleNamespace(
            workspace=a["workspace"], tokenizer=SimpleNamespace(encode=list)
        ),
        root=retrieval_root(control.paths, a["workspace"]),
        retrieve=retrieve,
    )
    job = await manager.start(["ALPHA"], PackageSettings(near_dedup=False))
    await asyncio.gather(*list(manager.tasks))
    service = ChainService(control, policy, actor="user")
    source = {
        "type": "retrieval",
        "data_batch_id": b["id"],
        "retrieval_job_id": job["id"],
    }
    with pytest.raises(KeyError):
        service.submit(b["id"], command(b, kind="distillation", source=source))
    source["data_batch_id"] = a["id"]
    accepted = service.submit(a["id"], command(a, kind="distillation", source=source))
    assert accepted["state"] == "queued"
    path = manager.root / job["id"] / "packages.jsonl"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="checksum"):
        service.submit(
            a["id"], command(a, kind="distillation", key="tampered", source=source)
        )


def test_catalog_file_is_pinned_in_revision_and_unchanged_save_stays_unchanged(
    factory, tmp_path
):
    catalog = tmp_path / "categories.yaml"
    catalog.write_text("categories: []")
    recipe = factory[2].model_dump()
    recipe["recipe"]["workflow"]["parameters"]["topic_catalog_file"] = str(catalog)
    from lightrag.batch_control.contracts import Configuration

    control = factory[0](defaults_override=Configuration.model_validate(recipe))
    batch = control.create("user", "Batch", "create")
    _, _, document, _, _ = control.read("user", batch["id"])
    pinned = Path(document.recipe["workflow"]["parameters"]["topic_catalog_file"])
    catalog.write_text("categories: changed")
    assert pinned.read_text() == "categories: []"
    saved = control.save("user", batch["id"], batch["active"], document, "save")
    assert saved["revision"] == batch["active"]


def test_changed_admitted_payload_never_reaches_execution(chain_setup, monkeypatch):
    service, batch, task, spec, store = prepared(chain_setup, monkeypatch)
    # Simulate an admitted row whose command bytes changed on disk.
    task["state"] = "admitted"
    task["payload"]["retrieval"]["rag_top_k"] = 99
    with service.control.registry.transaction() as db:
        db.execute(
            "UPDATE tasks SET record=? WHERE id=?", (json.dumps(task), task["task_id"])
        )
    runtime = service.runtime.store.get(batch["id"])
    with pytest.raises(IntegrityError, match="payload changed"):
        store.claim(
            batch["id"],
            runtime["generation"],
            runtime["startup_digest"],
            runtime["pid"],
            runtime["process_identity"],
        )


def test_workflow_cancellation_after_spawn_cleans_up_cli_and_records_interruption(
    chain_setup, monkeypatch
):
    from lightrag.integrations.data_workflow.config import load_settings
    from lightrag.batch_control.task_workflow import WorkflowRun

    service, batch, task, spec, _ = prepared(
        chain_setup, monkeypatch, kind="distillation"
    )
    _, _, document, _, _ = service.control.read(
        "user", batch["id"], task["config_revision_id"]
    )
    settings = load_settings(
        service.control.paths.revision(batch["id"], task["config_revision_id"])
        / "data_workflow.yaml"
    )
    workflow = WorkflowRun(
        service.control.paths, batch, task, settings, document.recipe
    )
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) >= 3

    with pytest.raises(InterruptedError):
        workflow.run(cancelled=cancelled)
    assert workflow.status()["status"] == "interrupted"
    assert len(calls) >= 3


def test_deployment_configuration_is_opt_in_and_keeps_explicit_paths(
    chain_setup, tmp_path, monkeypatch
):
    from dataclasses import asdict
    from lightrag.batch_control.deployment import load_deployment
    from lightrag.batch_control.routes import install_batch_control

    control, policy, *_ = chain_setup
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(yaml.safe_dump(control.defaults.recipe))
    deployment_file = tmp_path / "deployment.yaml"
    deployment_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "config_root": str(tmp_path / "explicit-config"),
                "runtime_root": str(tmp_path / "explicit-runs"),
                "recipe_file": str(recipe),
                "rag": control.defaults.rag.model_dump(),
                "retrieval": control.defaults.retrieval,
                "bridge": control.defaults.bridge.model_dump(),
                "runtime": {
                    "project_directory": str(policy.project_directory),
                    "python": str(policy.python),
                    "storage_mode": "local",
                },
                "environments": {
                    key: asdict(value)
                    for key, value in control.catalog.environments.items()
                },
                "inputs": {
                    key: [str(value) for value in values]
                    for key, values in control.catalog.inputs.items()
                },
                "profiles": control.catalog.profiles,
                "grants": [
                    {"actor": "api-key", "actions": ["read"], "resources": ["*"]}
                ],
            }
        )
    )
    loaded = load_deployment(deployment_file)
    assert loaded.control.paths.config_root == tmp_path / "explicit-config"
    assert loaded.policy.python == policy.python
    assert loaded.service("api-key").runtime.backend is loaded.dispatcher.backend
    assert loaded.control.authorize("api-key", "read", "batches")
    assert not loaded.control.authorize("api-key", "create", "batches")
    assert not loaded.control.paths.registry.exists()
    from lightrag.batch_control.app import create_manager_app
    import lightrag

    def reject_default_rag(*args, **kwargs):
        raise AssertionError("Manager must not instantiate a default-workspace RAG")

    monkeypatch.setattr(lightrag, "LightRAG", reject_default_rag)
    standalone = create_manager_app(deployment_file, api_key="test-private-key")
    assert not hasattr(standalone.state, "rag")
    assert hasattr(standalone.state, "batch_deployment")
    app = FastAPI()
    monkeypatch.delenv("LIGHTRAG_BATCH_CONTROL_CONFIG", raising=False)
    install_batch_control(app, "key")
    assert not hasattr(app.state, "batch_deployment")
    monkeypatch.setenv("LIGHTRAG_BATCH_CONTROL_CONFIG", str(deployment_file))
    install_batch_control(app, "key")
    assert hasattr(app.state, "batch_deployment")
    assert not loaded.control.paths.registry.exists()


@pytest.mark.asyncio
async def test_dispatcher_survives_transient_registry_errors(chain_setup, monkeypatch):
    dispatcher = BatchDispatcher(*chain_setup[:2])
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("offline simulated failure")
        dispatcher.stop_event.set()

    monkeypatch.setattr(dispatcher, "tick", tick)
    await asyncio.wait_for(dispatcher.run(), timeout=3)
    assert len(calls) == 2 and dispatcher.last_error is None


@pytest.mark.asyncio
async def test_native_ingest_to_retrieval_to_cli_receipt_uses_one_batch_rag(
    chain_setup, monkeypatch
):
    import numpy as np
    from lightrag import LightRAG
    from lightrag.api.routers.document_routes import (
        create_document_routes,
        DocumentManager,
    )
    from lightrag.kg.shared_storage import initialize_share_data, finalize_share_data
    from lightrag.utils import EmbeddingFunc, Tokenizer

    service, batch, task, spec, store = prepared(chain_setup, monkeypatch)
    # Pin the parser in this in-process fixture as the actual child compiler does.
    monkeypatch.setenv("LIGHTRAG_PARSER", "*:native")
    monkeypatch.setenv("EMBEDDING_BINDING_HOST", "")
    monkeypatch.setenv("RERANK_BINDING", "null")
    monkeypatch.setenv("ENTITY_TYPE_PROMPT_FILE", "")
    import lightrag.api.utils_api as auth

    monkeypatch.setattr(auth, "auth_configured", False)
    initialize_share_data()

    class CharacterTokenizer:
        def encode(self, text):
            return list(text.encode())

        def decode(self, tokens):
            return bytes(tokens).decode(errors="replace")

    calls = {"embedding": 0, "llm": 0}

    async def embedding(texts, **kwargs):
        calls["embedding"] += 1
        return np.ones((len(texts), 8))

    async def llm(*args, **kwargs):
        calls["llm"] += 1
        return json.dumps(
            {
                "entities": [
                    {
                        "name": "ALPHA",
                        "type": "CONCEPT",
                        "description": "Invoice approval policy",
                    }
                ],
                "relationships": [],
            }
        )

    rag = LightRAG(
        working_dir=str(service.control.paths.runtime_root / "actual-rag"),
        workspace=batch["workspace"],
        llm_model_func=llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=8, max_token_size=8192, func=embedding
        ),
        tokenizer=Tokenizer("offline-character", CharacterTokenizer()),
        entity_extract_max_gleaning=0,
        max_parallel_insert=1,
    )
    await rag.initialize_storages()
    app = FastAPI()
    app.state.rag = rag
    app.state.background_tasks = set()
    manager = DocumentManager(
        str(service.control.paths.runtime_root / "actual-inputs"),
        workspace=batch["workspace"],
    )
    rag.input_dir = str(manager.base_input_dir)
    app.include_router(create_document_routes(rag, manager, api_key=None))
    try:
        await asyncio.wait_for(TaskEngine(app, spec, task).execute(), timeout=30)
        result = service.get(batch["id"], task["task_id"])
        assert result["state"] == "completed", result
        assert result["result"]["ingest"]["documents"][0]["status"] == "processed"
        assert result["result"]["retrieval"]["package_count"] == 1
        workflow = result["result"]["distillation"]
        assert workflow["result"]["summary"]["sources"] == 1
        line = json.loads(Path(workflow["artifacts"]["input"]).read_text().strip())
        context = json.loads(line["text"])
        assert "ALPHA requires invoice approval." in "".join(context["text_chunks"])
        assert calls["embedding"] > 0 and calls["llm"] > 0
        assert (
            (chain_setup[2] / "policy.md").read_text().endswith("OFFLINE_SOURCE_ONLY.")
        )
        archive = os.getenv("LIGHTRAG_CHAIN_VALIDATION_RECORD")
        if archive:
            atomic_json(
                Path(archive),
                {
                    "schema_version": 1,
                    "kind": "real_local_rag_with_deterministic_model_functions_and_cli_protocol_fixture",
                    "task": result,
                    "model_fixture_calls": calls,
                    "external_model_requests": 0,
                    "input_text_only": set(line) == {"text"},
                    "context_fields": sorted(context),
                    "events": store.events(batch["id"], task["task_id"]),
                },
            )
    finally:
        if app.state.background_tasks:
            await asyncio.gather(
                *list(app.state.background_tasks), return_exceptions=True
            )
        for func in rag.role_llm_funcs.values():
            await rag._shutdown_llm_wrapper(func)
        await rag.finalize_storages()
        finalize_share_data()
