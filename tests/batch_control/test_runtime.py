"""Managed runtimes verify real process identity and batch-owned storage bindings."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

import pytest

from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_config import RuntimePolicy, clean_environment
from lightrag.batch_control.runtime_process import (
    ProcessBackend,
    atomic_json,
    process_identity,
    process_scope,
)
from lightrag.batch_control.storage import Conflict, IntegrityError

pytestmark = pytest.mark.offline


class FakeBackend:
    def __init__(self):
        self.specs = {}
        self.alive = {}
        self.launches = 0
        self.signals = []
        self.change = lambda proof: proof

    def launch(self, spec, environment):
        self.launches += 1
        pid = 10000000 + self.launches
        self.specs[pid] = spec
        self.alive[pid] = True
        return pid, "test-birth-" + str(pid)

    def ownership(self, record):
        return "owned" if self.alive.get(record.get("pid")) else "gone"

    def terminate(self, record, timeout):
        self.signals.append(record["pid"])
        self.alive[record["pid"]] = False

    def observe(self, record):
        spec = self.specs[record["pid"]]
        environment = spec["startup"]["environment"]
        root = Path(environment["WORKING_DIR"]) / record["workspace"]
        proof = {
            key: record[key]
            for key in (
                "data_batch_id",
                "generation",
                "startup_digest",
                "workspace",
                "pid",
                "process_identity",
                "process_scope",
            )
        }
        proof.update(
            state="ready",
            provider_admission=spec["startup"]["provider_admission"],
            capability=spec["capability"],
            observed_at=time.time(),
            working_directory=environment["WORKING_DIR"],
            input_directory=environment["INPUT_DIR"],
            storage_workspaces={
                key: record["workspace"]
                for key in (
                    "kv_storage",
                    "doc_status_storage",
                    "graph_storage",
                    "vector_storage",
                )
            },
            storage_classes={
                key: environment["LIGHTRAG_" + key.upper()]
                for key in (
                    "kv_storage",
                    "doc_status_storage",
                    "graph_storage",
                    "vector_storage",
                )
            },
            collections={
                key: record["workspace"] + "_" + key
                for key in ("graph", "entities", "relationships", "chunks")
            },
            storage_files={
                key: str(root / (key + ".json"))
                for key in ("graph", "entities", "relationships", "chunks")
            },
            embedding={
                "model": environment["EMBEDDING_MODEL"],
                "dimensions": int(environment["EMBEDDING_DIM"]),
            },
            parameters={
                "chunk_size": int(environment["CHUNK_SIZE"]),
                "chunk_overlap_size": int(environment["CHUNK_OVERLAP_SIZE"]),
                "max_parallel_insert": int(environment["MAX_PARALLEL_INSERT"]),
                "max_parallel_parse_native": int(
                    environment["MAX_PARALLEL_PARSE_NATIVE"]
                ),
            },
            roles={
                role: {
                    "model": environment[f"{role.upper()}_LLM_MODEL"],
                    "binding": environment[f"{role.upper()}_LLM_BINDING"],
                    "host": environment[f"{role.upper()}_LLM_BINDING_HOST"],
                    "max_async": int(environment[f"{role.upper()}_MAX_ASYNC_LLM"]),
                }
                for role in ("query", "extract", "keyword", "vlm")
            },
        )
        return self.change(proof)


@pytest.fixture
def runtime_setup(factory):
    catalog = factory[3]
    catalog.profiles["llm-v1"] = {
        "model_name": "offline-chat",
        "api_url": "http://127.0.0.1:9/v1",
    }
    catalog.profiles["embed-v1"] = {
        "model_name": "offline-embedding",
        "api_url": "http://127.0.0.1:9/v1",
        "dimensions": 8,
    }
    control = factory[0]()
    policy = RuntimePolicy(
        Path(__file__).resolve().parents[2],
        Path(sys.executable),
        storage_mode="local",
        startup_timeout=60,
        stop_timeout=15,
    )
    return control, policy


@pytest.fixture
def managed(runtime_setup, monkeypatch):
    control, policy = runtime_setup
    monkeypatch.setattr(
        "lightrag.batch_control.runtime_config.selected_fingerprint",
        lambda policy: {"source": "offline-test"},
    )
    backend = FakeBackend()
    return (
        control,
        RuntimeSupervisor(control, policy, actor="user", backend=backend),
        backend,
    )


def create(control, key="first"):
    return control.create("user", "Same display name", key)


def test_running_process_does_not_silently_reuse_changed_deployment_policy(managed):
    control, runtime, backend = managed
    batch = create(control)
    original = runtime.start(batch["id"], batch["active"])
    changed = RuntimeSupervisor(
        control,
        replace(runtime.policy, heartbeat_seconds=2),
        actor="user",
        backend=backend,
    )
    with pytest.raises(Conflict, match="startup snapshot differs"):
        changed.start(batch["id"], batch["active"])
    assert runtime.inspect(batch["id"]) == original
    assert backend.launches == 1 and backend.signals == []


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_nonfinite_or_nonpositive_policy_deadlines_are_rejected(runtime_setup, value):
    with pytest.raises(ValueError, match="positive"):
        replace(runtime_setup[1], startup_timeout=value)


def test_two_batches_have_distinct_resources_and_recoverable_runtime_leases(managed):
    control, runtime, backend = managed
    first, second = create(control), create(control, "second")
    a = runtime.start(first["id"], first["active"])
    b = runtime.start(second["id"], second["active"])
    assert a.workspace != b.workspace and a.pid != b.pid
    assert not set(a.collections.values()) & set(b.collections.values())
    recovered = RuntimeSupervisor(
        control, runtime.policy, actor="user", backend=backend
    )
    assert recovered.inspect(first["id"]) == a
    assert recovered.start(first["id"], first["active"]) == a
    assert backend.launches == 2
    recovered.stop(first["id"])
    assert recovered.inspect(first["id"]) is None
    assert recovered.inspect(second["id"]) == b
    again = recovered.start(first["id"], first["active"])
    assert again.generation == 2 and again.collections == a.collections
    assert control.registry.get(first["id"])["lifecycle"] == "ready"
    assert [row["event"] for row in runtime.events(first["id"])] == [
        "reserved",
        "startup_published",
        "process_attached",
        "ready_recovered",
        "stop_requested",
        "stopped",
        "reserved",
        "startup_published",
        "process_attached",
        "ready_recovered",
    ]


@pytest.mark.parametrize(
    "field",
    [
        "workspace",
        "storage_workspaces",
        "collections",
        "embedding",
        "capability",
        "generation",
        "parameters",
        "roles",
        "provider_admission",
    ],
)
def test_wrong_runtime_proof_never_grants_a_ready_lease(managed, field):
    control, runtime, backend = managed
    batch = create(control)

    def corrupt(proof):
        if field == "storage_workspaces":
            proof[field]["graph_storage"] = "production"
        elif field == "collections":
            proof[field]["chunks"] = "/production/vectors.json"
        elif field == "embedding":
            proof[field]["dimensions"] = 4096
        elif field == "generation":
            proof[field] += 1
        elif field == "parameters":
            proof[field]["chunk_size"] = 99
        elif field == "roles":
            proof[field]["query"]["model"] = "wrong-model"
        else:
            proof[field] = "foreign"
        return proof

    backend.change = corrupt
    with pytest.raises(IntegrityError):
        runtime.start(batch["id"], batch["active"])
    assert runtime.store.get(batch["id"])["state"] == "failed"
    assert backend.signals and runtime.inspect(batch["id"]) is None


def test_capacity_is_shared_between_supervisors_and_released_only_after_stop(managed):
    control, runtime, backend = managed
    policy = RuntimePolicy(
        runtime.policy.project_directory,
        runtime.policy.python,
        storage_mode="local",
        max_processes=1,
    )
    a, b = create(control), create(control, "second")
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    runtime.start(a["id"], a["active"])
    other = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    with pytest.raises(Conflict, match="capacity"):
        other.start(b["id"], b["active"])
    runtime.stop(a["id"])
    assert other.start(b["id"], b["active"]).data_batch_id == b["id"]


def test_concurrent_start_of_one_batch_launches_one_process(managed):
    control, runtime, backend = managed
    batch = create(control)
    other = RuntimeSupervisor(control, runtime.policy, actor="user", backend=backend)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(item.start, batch["id"], batch["active"])
            for item in (runtime, other)
        ]
        leases = [future.result() for future in futures]
    assert leases[0] == leases[1] and backend.launches == 1


def test_pending_configuration_and_unsupported_startup_values_do_not_launch(managed):
    control, runtime, backend = managed
    batch = create(control)
    _, _, document, _, _ = control.read("user", batch["id"])
    changed = document.model_copy(
        update={
            "rag": document.rag.model_copy(
                update={"chunk_size": document.rag.chunk_size + 100}
            )
        }
    )
    control.save("user", batch["id"], batch["active"], changed, "change")
    with pytest.raises(Conflict, match="pending"):
        runtime.start(batch["id"], batch["active"])
    other = create(control, "unsupported")
    control.catalog.profiles["llm-v1"]["binding"] = "unsupported"
    with pytest.raises(ValueError, match="profile changed"):
        runtime.start(other["id"], other["active"])
    assert backend.launches == 0


def test_permission_denial_cannot_start_or_stop_processes(managed):
    control, runtime, backend = managed
    batch = create(control)
    control.authorize = lambda actor, action, resource: (
        action not in {"run_runtime", "stop_runtime"}
    )
    with pytest.raises(PermissionError):
        runtime.start(batch["id"], batch["active"])
    with pytest.raises(PermissionError):
        runtime.stop(batch["id"])
    assert backend.launches == 0 and not backend.signals


def test_parent_environment_cannot_override_workspace_or_credentials(monkeypatch):
    monkeypatch.setenv("NEO4J_WORKSPACE", "production")
    monkeypatch.setenv("MILVUS_WORKSPACE", "production")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("PYTHONSTARTUP", "/untrusted/startup.py")
    environment = clean_environment(Path("/server/checkout"))
    assert (
        not {"NEO4J_WORKSPACE", "MILVUS_WORKSPACE", "OPENAI_API_KEY", "PYTHONSTARTUP"}
        & environment.keys()
    )


def test_pid_reuse_never_authorizes_a_signal(monkeypatch):
    backend = ProcessBackend()
    record = {
        "pid": os.getpid(),
        "process_identity": str(float(process_identity(os.getpid())) - 1),
        "process_scope": process_scope(),
    }
    monkeypatch.setattr(
        "os.killpg",
        lambda *args: pytest.fail("A foreign process must not be signalled"),
    )
    assert backend.ownership(record) == "reused"
    with pytest.raises(RuntimeError, match="no signal"):
        backend.terminate(record, 0.1)


def test_real_local_rag_workers_are_isolated_and_stoppable(runtime_setup):
    control, policy = runtime_setup
    backend = ProcessBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    batches = [create(control), create(control, "second")]
    try:
        a, b = [runtime.start(batch["id"], batch["active"]) for batch in batches]
        assert a.pid != b.pid and a.workspace != b.workspace
        assert not set(a.collections.values()) & set(b.collections.values())
        # A fresh manager verifies existing children instead of starting duplicates.
        recovered = RuntimeSupervisor(control, policy, actor="user")
        assert recovered.inspect(batches[0]["id"]) == a
        spec = runtime._spec(runtime.store.get(batches[0]["id"]))
        assert "LLM_BINDING_API_KEY" not in json.dumps(spec)
        proof = backend.observe(runtime.store.get(batches[0]["id"]))
        assert proof["parameters"]["max_parallel_insert"] == 3
        assert proof["parameters"]["max_parallel_parse_native"] == 5
        recovered.stop(batches[0]["id"])
        assert recovered.inspect(batches[0]["id"]) is None
        assert recovered.inspect(batches[1]["id"]) == b
        proof_b = backend.observe(runtime.store.get(batches[1]["id"]))
        assert not set(proof["storage_files"].values()) & set(
            proof_b["storage_files"].values()
        )
        result = {
            "schema_version": 1,
            "verified_at": time.time(),
            "storage_mode": "local",
            "model_requests": 0,
            "workflow_execution": False,
            "batches": [a.model_dump(), b.model_dump()],
            "observations": [
                {key: value for key, value in p.items() if key != "capability"}
                for p in (proof, proof_b)
            ],
            "manager_recovery_verified": True,
            "independent_stop_verified": True,
        }
    finally:
        for batch in batches:
            record = runtime.store.get(batch["id"])
            if record and backend.ownership(record) == "owned":
                runtime.stop(batch["id"])
        for child in list(backend.children.values()):
            child.wait(timeout=5)
    result["all_children_stopped"] = all(
        backend.ownership(runtime.store.get(batch["id"])) == "gone" for batch in batches
    )
    result["events"] = {batch["id"]: runtime.events(batch["id"]) for batch in batches}
    if os.getenv("LIGHTRAG_RUNTIME_VALIDATION_RECORD"):
        atomic_json(Path(os.environ["LIGHTRAG_RUNTIME_VALIDATION_RECORD"]), result)


@pytest.mark.parametrize(
    "field,value",
    [("chunking_strategy", "unsupported_chunker")],
)
def test_unsupported_parser_or_chunker_is_rejected_without_launch(
    runtime_setup, field, value
):
    control, policy = runtime_setup
    control.defaults = control.defaults.model_copy(
        update={"rag": control.defaults.rag.model_copy(update={field: value})}
    )
    batch = create(control)
    backend = FakeBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    with pytest.raises(ValueError, match="Unsupported managed chunking strategy"):
        runtime.start(batch["id"], batch["active"])
    assert backend.launches == 0


def test_missing_profile_credentials_do_not_start_a_process(runtime_setup, tmp_path):
    control, policy = runtime_setup
    credentials = tmp_path / "keys.env"
    credentials.write_text("OTHER_KEY=unused\n")
    control.catalog.credentials["key-v1"] = credentials
    control.catalog.profiles["llm-v1"].update(
        credential_id="key-v1", key_name_of_api_key="MISSING_KEY"
    )
    batch = create(control)
    backend = FakeBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    with pytest.raises(ValueError, match="missing or empty"):
        runtime.start(batch["id"], batch["active"])
    assert backend.launches == 0


def test_model_secret_values_never_enter_runtime_snapshots_or_events(
    runtime_setup, tmp_path, monkeypatch
):
    control, policy = runtime_setup
    credentials = tmp_path / "keys.env"
    credentials.write_text("TEST_KEY=private-test-credential\n")
    control.catalog.credentials["key-v1"] = credentials
    control.catalog.profiles["llm-v1"].update(
        credential_id="key-v1", key_name_of_api_key="TEST_KEY"
    )
    monkeypatch.setattr(
        "lightrag.batch_control.runtime_config.selected_fingerprint",
        lambda policy: {"source": "test"},
    )
    batch = create(control)
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=FakeBackend())
    runtime.start(batch["id"], batch["active"])
    record = runtime.store.get(batch["id"])
    assert "private-test-credential" not in Path(record["spec_path"]).read_text()
    assert "private-test-credential" not in json.dumps(runtime.events(batch["id"]))
    runtime.stop(batch["id"])


def test_storage_scope_overrides_are_rejected(runtime_setup, tmp_path):
    control, policy = runtime_setup
    path = tmp_path / "storage.env"
    path.write_text("NEO4J_WORKSPACE=production\n")
    policy = RuntimePolicy(
        policy.project_directory,
        policy.python,
        storage_mode="local",
        storage_credentials_file=path,
    )
    batch = create(control)
    backend = FakeBackend()
    runtime = RuntimeSupervisor(control, policy, actor="user", backend=backend)
    with pytest.raises(ValueError, match="workspace overrides"):
        runtime.start(batch["id"], batch["active"])
    assert backend.launches == 0


def test_unconfirmed_stop_keeps_the_process_slot_reserved(managed):
    control, runtime, backend = managed
    batch = create(control)
    runtime.start(batch["id"], batch["active"])

    def refuse_shutdown(record, timeout):
        raise TimeoutError("still stopping")

    backend.terminate = refuse_shutdown
    with pytest.raises(TimeoutError):
        runtime.stop(batch["id"])
    assert runtime.store.get(batch["id"])["state"] == "stopping"
    assert runtime.inspect(batch["id"]) is None
    with pytest.raises(Conflict, match="already holds"):
        runtime.start(batch["id"], batch["active"])


def test_symlink_cannot_redirect_one_batch_into_another(managed, tmp_path):
    control, runtime, backend = managed
    batch = create(control)
    parent = control.paths.runtime_root / "rag"
    parent.mkdir(parents=True)
    other = tmp_path / "other-batch-storage"
    other.mkdir()
    (parent / batch["namespace"]).symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        runtime.start(batch["id"], batch["active"])
    assert backend.launches == 0 and list(other.iterdir()) == []


@pytest.mark.parametrize(
    "strategy,selector",
    [
        ("paragraph_semantic", "P"),
        ("semantic_vector", "V"),
        ("recursive_character", "R"),
        ("fixed_token", "F"),
    ],
)
def test_batch_chunking_configuration_reaches_the_existing_document_router(
    runtime_setup, monkeypatch, strategy, selector
):
    from lightrag.batch_control.runtime_config import compile_startup
    from lightrag.parser.routing import resolve_parser_directives

    control, policy = runtime_setup
    control.defaults = control.defaults.model_copy(
        update={
            "rag": control.defaults.rag.model_copy(
                update={
                    "chunking_strategy": strategy,
                    "chunk_size": 2000,
                    "chunk_overlap_size": 100,
                }
            )
        }
    )
    batch = create(control)
    owner, _, document, _, manifest = control.read("user", batch["id"])
    startup, _ = compile_startup(
        control, "user", owner, batch["active"], document, manifest, policy
    )
    for key, value in startup["environment"].items():
        monkeypatch.setenv(key, value)
    directives = resolve_parser_directives("batch-document.md")
    assert selector in directives.process_options
    assert startup["environment"]["CHUNK_P_SIZE"] == "2000"
    assert startup["environment"]["CHUNK_P_OVERLAP_SIZE"] == "100"
    assert "F" in resolve_parser_directives("explicit.[native-F].md").process_options
    assert startup["environment"]["LIGHTRAG_DATA_WORKFLOW_CONFIG_ROOT"] == str(
        control.paths.config_root / "business"
    )
