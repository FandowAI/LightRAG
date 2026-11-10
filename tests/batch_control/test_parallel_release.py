"""Prove actual overlap, independent cancellation/failure and drained application."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time

import psutil
import pytest

from lightrag.batch_control.applications import ApplicationService
from lightrag.batch_control.chain_service import BatchDispatcher, ChainService
from lightrag.batch_control.runtime_process import atomic_json
from lightrag.batch_control.storage import Conflict
from tests.batch_control.helpers import change
from tests.batch_control.test_full_chain import chain_setup, command  # noqa: F401

pytestmark = pytest.mark.offline

GATED_CLI = """
import hashlib, json, os, sys, time
from pathlib import Path
import yaml
gate = Path(GATE_DIRECTORY)
outcome = OUTCOME
config = yaml.safe_load(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
parameters = config['workflow']['parameters']
label = parameters['scenario_instructions']
assert label in {'alpha', 'beta'}
assert 'BATCH_SECRET_MUST_NOT_LEAK' not in os.environ
rows = [json.loads(line) for line in Path(config['input']['paths'][0]).read_text().splitlines()]
assert len(rows) == 1 and set(rows[0]) == {'text'}
evidence = json.loads(rows[0]['text'])
assert set(evidence) == {'entities', 'relationships', 'text_chunks'}
assert label.upper() in rows[0]['text']
prompt = Path(parameters['scenario']['prompt_file']).read_bytes()
assert label.encode() in prompt
marker = {'label': label, 'pid': os.getpid(), 'started_at': time.time(),
          'questions_per_source': parameters['questions_per_source'],
          'input_sha256': hashlib.sha256(rows[0]['text'].encode()).hexdigest(),
          'prompt_sha256': hashlib.sha256(prompt).hexdigest()}
with (gate / (label + '.launches')).open('a') as handle:
    handle.write(json.dumps(marker) + '\\n')
temporary = gate / (label + '.tmp')
temporary.write_text(json.dumps(marker))
temporary.replace(gate / (label + '.started.json'))
deadline = time.monotonic() + 90
while not (gate / (label + '.release')).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('Parallel acceptance release gate did not open')
    time.sleep(0.05)
if label == 'alpha' and outcome == 'failed':
    print('Intentional CLI failure without a terminal receipt', file=sys.stderr)
    sys.exit(2)
directory = Path(config['output']['directory']) / ('run-' + label)
directory.mkdir(parents=True)
receipt = {'run_id': directory.name, 'directory': str(directory), 'status': 'completed',
           'summary': {'sources': len(rows), 'label': label,
                       'questions_per_source': parameters['questions_per_source']}}
(directory / 'run.json').write_text(json.dumps(receipt))
(gate / (label + '.finished.json')).write_text(json.dumps({'finished_at': time.time()}))
print(json.dumps(receipt))
"""


def wait_until(read, predicate, *, seconds=30):
    deadline = time.monotonic() + seconds
    value = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.1)
    raise AssertionError(f"Parallel acceptance condition timed out: {value!r}")


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_parallel_workers_keep_results_and_configuration_application_independent(
    chain_setup,  # noqa: F811
    tmp_path,
    monkeypatch,
    outcome,
):
    control, policy, *_ = chain_setup
    gate = tmp_path / "parallel-gate"
    gate.mkdir()
    project = Path(control.catalog.environments["test"].project_directory)
    (project / "src/data_studio/__main__.py").write_text(
        GATED_CLI.replace("GATE_DIRECTORY", repr(str(gate))).replace(
            "OUTCOME", repr(outcome)
        )
    )
    monkeypatch.setenv("BATCH_SECRET_MUST_NOT_LEAK", "not-for-a-child")
    batches = []
    for index, label in enumerate(("alpha", "beta")):
        source = tmp_path / ("source-" + label)
        source.mkdir()
        (source / "source.json").write_text(
            json.dumps(
                {
                    "status": "success",
                    "data": {
                        "entities": [{"entity_name": label.upper()}],
                        "relationships": [],
                        "chunks": [{"content": label.upper() + " only evidence"}],
                    },
                }
            )
        )
        control.catalog.inputs[label] = [source]
        defaults = change(control.defaults, "bridge", input_ids=[label])
        recipe = defaults.model_dump()["recipe"]
        parameters = recipe["workflow"]["parameters"]
        parameters["questions_per_source"] = 3 + index
        parameters["scenario_instructions"] = label
        defaults = defaults.model_copy(update={"recipe": recipe})
        control.defaults = change(defaults, "rag", max_parallel_insert=2 + index)
        batch = control.create("user", "Same display name", label)
        document = control.read("user", batch["id"])[2]
        saved = control.save(
            "user",
            batch["id"],
            batch["active"],
            document,
            "inline-prompt",
            prompt_contents={"scenario": label + ": $payload"},
        )
        batch = control.registry.get(batch["id"])
        assert batch["active"] == saved["revision"]
        batches.append(batch)

    service = ChainService(control, policy, actor="user")
    requests = [
        command(
            batch,
            kind="distillation",
            source={
                "data_batch_id": batch["id"],
                "type": "directory",
                "input_ids": [label],
            },
        )
        for batch, label in zip(batches, ("alpha", "beta"))
    ]
    # Same button identity is retried concurrently, scoped independently per batch.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(service.submit, batch["id"], request)
            for batch, request in zip(batches * 2, requests * 2)
        ]
        submitted = [future.result() for future in futures]
    assert submitted[0]["task_id"] == submitted[2]["task_id"]
    assert submitted[1]["task_id"] == submitted[3]["task_id"]
    assert submitted[0]["task_id"] != submitted[1]["task_id"]
    tasks = submitted[:2]
    dispatcher = BatchDispatcher(control, policy)
    proof = None
    try:
        dispatcher.tick()
        markers = wait_until(
            lambda: sorted(gate.glob("*.started.json")), lambda paths: len(paths) == 2
        )
        starts = [
            json.loads((gate / (label + ".started.json")).read_text())
            for label in ("alpha", "beta")
        ]
        assert len(markers) == 2 and starts[0]["pid"] != starts[1]["pid"]
        assert all(psutil.pid_exists(marker["pid"]) for marker in starts)
        running = [
            service.get(batch["id"], task["task_id"])
            for batch, task in zip(batches, tasks)
        ]
        assert all(record["state"] == "running" for record in running)
        assert starts[0]["input_sha256"] != starts[1]["input_sha256"]
        assert starts[0]["prompt_sha256"] != starts[1]["prompt_sha256"]
        assert [marker["questions_per_source"] for marker in starts] == [3, 4]
        leases = [service.runtime.inspect(batch["id"]) for batch in batches]
        assert leases[0].pid != leases[1].pid
        assert not set(leases[0].collections.values()) & set(
            leases[1].collections.values()
        )
        # A reconstructed manager recognizes both owners without another CLI launch.
        recovered = BatchDispatcher(control, policy)
        recovered.tick()
        recovered.tick()
        assert [service.runtime.inspect(batch["id"]) for batch in batches] == leases
        with pytest.raises(KeyError):
            service.get(batches[1]["id"], tasks[0]["task_id"])
        with pytest.raises(KeyError):
            service.artifact_path(
                batches[1]["id"], tasks[0]["task_id"], "workflow-input"
            )
        third = control.create("user", "Capacity waiting", "third")
        with pytest.raises(Conflict, match="capacity"):
            service.runtime.start(third["id"], third["active"])
        assert service.runtime.store.get(third["id"]) is None

        document = control.read("user", batches[0]["id"])[2]
        pending = control.save(
            "user",
            batches[0]["id"],
            batches[0]["active"],
            change(document, "rag", max_parallel_insert=7),
            "pending-rag",
        )
        application = ApplicationService(
            control, policy, actor="user", backend=dispatcher.backend
        )
        application.request(
            batches[0]["id"], pending["operation"], pending["revision"], "apply"
        )
        draining = application.step(batches[0]["id"], pending["operation"])
        assert draining["state"] == "draining"
        assert service.runtime.inspect(batches[0]["id"]) == leases[0]
        assert all(psutil.pid_exists(marker["pid"]) for marker in starts)
        last_simultaneous_observation = time.time()
        if outcome == "cancelled":
            service.cancel(batches[0]["id"], tasks[0]["task_id"])
        else:
            (gate / "alpha.release").touch()
        first = wait_until(
            lambda: service.get(batches[0]["id"], tasks[0]["task_id"]),
            lambda record: record["terminal"],
        )
        assert first["state"] == outcome, first
        assert service.get(batches[1]["id"], tasks[1]["task_id"])["state"] == "running"
        assert psutil.pid_exists(starts[1]["pid"])
        if outcome == "cancelled":
            assert not psutil.pid_exists(starts[0]["pid"])
        applied = application.step(batches[0]["id"], pending["operation"])
        assert applied["state"] == "applied", applied
        assert applied["lease"]["generation"] == leases[0].generation + 1
        assert service.runtime.inspect(batches[1]["id"]) == leases[1]
        assert service.get(batches[1]["id"], tasks[1]["task_id"])["state"] == "running"
        (gate / "beta.release").touch()
        second = wait_until(
            lambda: service.get(batches[1]["id"], tasks[1]["task_id"]),
            lambda record: record["terminal"],
        )
        assert second["state"] == "completed", second
        assert second["result"]["distillation"]["result"]["summary"]["label"] == "beta"
        assert second["config_revision_id"] == batches[1]["active"]
        recovered.tick()
        assert service.get(batches[0]["id"], tasks[0]["task_id"])["state"] == outcome
        assert all(
            len((gate / (label + ".launches")).read_text().splitlines()) == 1
            for label in ("alpha", "beta")
        )
        hashes = [
            hashlib.sha256(
                service.artifact_path(
                    batch["id"], task["task_id"], "workflow-input"
                ).read_bytes()
            ).hexdigest()
            for batch, task in zip(batches, tasks)
        ]
        assert hashes[0] != hashes[1]
        proof = {
            "schema_version": 1,
            "kind": "real_parallel_workers_and_drained_application_with_gated_cli_fixture",
            "scenario": outcome,
            "simultaneously_running": running,
            "cli_starts": starts,
            "both_started_before_any_release": True,
            "last_simultaneous_observation_at": last_simultaneous_observation,
            "overlap_seconds_at_least": last_simultaneous_observation
            - max(marker["started_at"] for marker in starts),
            "original_leases": [lease.model_dump() for lease in leases],
            "application": applied,
            "final_tasks": [first, second],
            "manager_reconstruction_verified": True,
            "capacity_verified": True,
            "cross_batch_access_rejected": True,
            "one_cli_launch_per_task": True,
            "external_model_requests": 0,
        }
    finally:
        for label in ("alpha", "beta"):
            (gate / (label + ".release")).touch()
        for batch, task in zip(batches, tasks):
            record = service.get(batch["id"], task["task_id"])
            if not record["terminal"]:
                service.cancel(batch["id"], task["task_id"])
            service.runtime.stop(batch["id"])
    if proof is not None:
        proof["all_children_stopped"] = all(
            service.runtime.store.get(batch["id"])["state"] == "stopped"
            for batch in batches
        )
        assert proof["all_children_stopped"]
        proof["all_cli_children_stopped"] = all(
            not psutil.pid_exists(marker["pid"]) for marker in starts
        )
        assert proof["all_cli_children_stopped"]
        archive = os.getenv("LIGHTRAG_PARALLEL_VALIDATION_DIRECTORY")
        if archive:
            atomic_json(Path(archive) / ("parallel-" + outcome + ".json"), proof)
