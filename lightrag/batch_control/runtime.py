"""Batch-owned RAG process supervision; existing live services are never adopted."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import secrets
import time

from .contracts import Name, RuntimeLease
from pydantic import TypeAdapter
from .resources import digest
from .runtime_config import compile_startup
from .runtime_process import ProcessBackend, atomic_json
from .runtime_store import HELD, RuntimeStore
from .storage import Conflict, IntegrityError


class RuntimeSupervisor:
    """Bind actor to the authenticated principal before exposing this supervisor."""

    def __init__(self, control, policy, *, actor, backend=None):
        TypeAdapter(Name).validate_python(actor, strict=True)
        self.control, self.policy, self.actor = control, policy, actor
        self.store = RuntimeStore(control.registry)
        self.backend = backend or ProcessBackend()

    @contextmanager
    def locked(self, batch):
        self.control.allow(self.actor, "read", batch)
        self.control.registry.get(batch)
        path = self.control.paths.under(
            self.control.paths.runtime_root, "batch-control", "locks", batch + ".lock"
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise IntegrityError("Runtime lock must not be a symlink")
        with path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _spec(self, record):
        path = Path(record["spec_path"])
        expected = self.control.paths.under(
            self.control.paths.runtime_root,
            "batch-runtime",
            self.control.registry.get(record["data_batch_id"])["namespace"],
            "generation-" + str(record["generation"]),
            "spec.json",
        )
        if (
            path != expected
            or path.is_symlink()
            or record["attempt_directory"] != str(expected.parent)
        ):
            raise IntegrityError(
                "Runtime specification does not belong to this generation"
            )
        spec = json.loads(path.read_text(encoding="utf-8"))
        if (
            digest(spec["startup"]) != record["startup_digest"]
            or spec["generation"] != record["generation"]
            or spec["data_batch_id"] != record["data_batch_id"]
            or spec.get("process_scope") != record.get("process_scope")
            or hashlib.sha256(spec["capability"].encode()).hexdigest()
            != record["capability_sha256"]
            or spec.get("control_paths")
            != {
                "config_root": str(self.control.paths.config_root),
                "runtime_root": str(self.control.paths.runtime_root),
            }
            or spec.get("registry") != str(self.control.paths.registry)
        ):
            raise IntegrityError("Runtime startup snapshot changed")
        return spec

    def _proof(self, record):
        if self.backend.ownership(record) != "owned":
            return None
        proof = self.backend.observe(record)
        if proof is None or proof.get("state") != "ready":
            return None
        spec = self._spec(record)
        from .index_rebuild.authority import (
            runtime_index_proof,
            effective_storage_binding,
        )

        if spec["startup"].get("purpose", "rag") != "rag":
            raise IntegrityError(
                "A builder/preflight cannot establish full RAG readiness"
            )
        index = runtime_index_proof(
            self.control.registry, self.control.paths, spec, proof
        )
        vector_workspace = (
            index["binding"]["vector_workspace"] if index else record["workspace"]
        )
        if (
            not spec["startup"].get("provider_admission")
            or proof.get("provider_admission") != spec["startup"]["provider_admission"]
        ):
            raise IntegrityError("Runtime provider admission was not installed")
        for key in (
            "data_batch_id",
            "generation",
            "startup_digest",
            "workspace",
            "pid",
            "process_identity",
            "process_scope",
        ):
            if proof.get(key) != record.get(key):
                raise IntegrityError(
                    "Runtime observation disagrees with process ownership: " + key
                )
        if not secrets.compare_digest(
            str(proof.get("capability", "")), spec["capability"]
        ):
            raise IntegrityError("Runtime observation capability does not match")
        age = time.time() - proof.get("observed_at", 0)
        if age < -2 or age > self.policy.health_timeout:
            return None
        environment = spec["startup"]["environment"]
        if (
            proof.get("working_directory") != environment["WORKING_DIR"]
            or proof.get("input_directory") != environment["INPUT_DIR"]
        ):
            raise IntegrityError("Runtime directory binding does not match its batch")
        scopes = proof.get("storage_workspaces", {})
        if scopes != {
            "kv_storage": record["workspace"],
            "doc_status_storage": record["workspace"],
            "graph_storage": record["workspace"],
            "vector_storage": vector_workspace,
        }:
            raise IntegrityError("A storage backend escaped its batch workspace")
        expected_classes = {
            key: environment["LIGHTRAG_" + key.upper()] for key in scopes
        }
        if proof.get("storage_classes") != expected_classes:
            raise IntegrityError(
                "Runtime storage classes do not match the managed policy"
            )
        if proof.get("embedding") != {
            "model": environment["EMBEDDING_MODEL"],
            "dimensions": int(environment["EMBEDDING_DIM"]),
        }:
            raise IntegrityError("Runtime embedding space does not match its profile")
        parameters = {
            "chunk_size": int(environment["CHUNK_SIZE"]),
            "chunk_overlap_size": int(environment["CHUNK_OVERLAP_SIZE"]),
            "max_parallel_insert": int(environment["MAX_PARALLEL_INSERT"]),
            "max_parallel_parse_native": int(environment["MAX_PARALLEL_PARSE_NATIVE"]),
        }
        if proof.get("parameters") != parameters:
            raise IntegrityError(
                "Runtime chunk or concurrency configuration was ignored"
            )
        for role in ("query", "extract", "keyword", "vlm"):
            prefix = role.upper()
            expected_role = {
                "model": environment[f"{prefix}_LLM_MODEL"],
                "binding": environment[f"{prefix}_LLM_BINDING"],
                "host": environment[f"{prefix}_LLM_BINDING_HOST"],
                "max_async": int(environment[f"{prefix}_MAX_ASYNC_LLM"]),
            }
            if proof.get("roles", {}).get(role) != expected_role:
                raise IntegrityError("Runtime role configuration was ignored: " + role)
        collections = proof.get("collections", {})
        if set(collections) != {"graph", "entities", "relationships", "chunks"}:
            raise IntegrityError("Runtime did not report every storage binding")
        binding = spec["startup"].get("storage_binding")
        binding = effective_storage_binding(
            binding, index["binding"] if index else None
        )
        if binding and (
            collections != binding["collections"]
            or proof.get("storage_binding_digest")
            != spec["startup"]["storage_binding_digest"]
        ):
            raise IntegrityError(
                "Runtime did not reuse the exact adopted storage bindings"
            )
        if binding:
            from .bindings import BindingStore, verify_storage_binding

            verify_storage_binding(proof, spec["startup"])

            adopted = BindingStore(self.control.registry).get(record["data_batch_id"])
            if (
                not adopted
                or adopted["digest"] != spec["startup"]["storage_binding_digest"]
            ):
                raise IntegrityError("Adopted registry and runtime binding differ")
            if (
                adopted["state"] == "registered"
                and proof.get("adoption_graph") != binding["graph"]
            ):
                raise IntegrityError("Adopted graph does not match its frozen baseline")
            if adopted["state"] == "registered":
                if (
                    binding["runtime_resource_digests"]
                    and proof.get("runtime_resource_digests")
                    != binding["runtime_resource_digests"]
                ):
                    raise IntegrityError(
                        "Initial adopted runtime resources were not verified"
                    )
                for field, evidence in (
                    ("vectors", "adoption_vectors"),
                    ("document_states", "adoption_document_states"),
                ):
                    if binding[field] and proof.get(evidence) != binding[field]:
                        raise IntegrityError(
                            "Adopted storage baseline was not verified: " + field
                        )
        if spec["startup"]["storage_mode"] == "neo4j_milvus":
            if collections["graph"] != record["workspace"] or any(
                not collections[key].startswith(vector_workspace + "_" + key + "_")
                for key in ("entities", "relationships", "chunks")
            ):
                raise IntegrityError(
                    "Runtime graph label or vector collection is not isolated"
                )
        else:
            storage_root = Path(environment["WORKING_DIR"]) / record["workspace"]
            vector_root = Path(environment["WORKING_DIR"]) / vector_workspace
            files = proof.get("storage_files", {})
            if (
                any(
                    not Path(value)
                    .resolve()
                    .is_relative_to(
                        (storage_root if key == "graph" else vector_root).resolve()
                    )
                    for key, value in files.items()
                )
                or set(files) != set(collections)
                or any(
                    not value.startswith(
                        (record["workspace"] if key == "graph" else vector_workspace)
                        + "_"
                    )
                    for key, value in collections.items()
                )
            ):
                raise IntegrityError("Local storage file escaped its batch directory")
        return proof

    def _inspect(self, batch):
        record = self.store.get(batch)
        if not record or record["state"] not in HELD:
            return None
        ownership = self.backend.ownership(record)
        if ownership in {"gone", "reused"}:
            self.store.update(
                batch,
                record["generation"],
                HELD,
                self.actor,
                "process_lost",
                state="failed",
                error="Recorded process exited or its PID was reused; no signal sent",
            )
            return None
        if record.get("purpose") == "index_build":
            return None
        proof = self._proof(record)
        if proof is None:
            return None
        if record["state"] in {"reserved", "starting"}:
            record = self.store.update(
                batch,
                record["generation"],
                {"reserved", "starting"},
                self.actor,
                "ready_recovered",
                state="ready",
                collections=proof["collections"],
            )
        if record["state"] != "ready":
            return None
        lease = RuntimeLease.model_validate(
            {
                key: record[key]
                for key in RuntimeLease.model_fields
                if key != "collections"
            }
            | {"collections": proof["collections"]}
        )
        if self._spec(record)["startup"].get("storage_binding"):
            from .bindings import BindingStore

            BindingStore(self.control.registry).complete(batch, lease, proof)
        return lease

    def inspect(self, data_batch_id):
        with self.locked(data_batch_id):
            return self._inspect(data_batch_id)

    def start(
        self,
        batch,
        revision,
        *,
        application_id=None,
        rollback_id=None,
        rebuild_rollback_id=None,
    ):
        self.control.allow(self.actor, "run_runtime", batch)
        if rebuild_rollback_id is not None:
            self.control.allow(self.actor, "rebuild_index", batch)
        elif application_id is not None:
            from .index_rebuild.authority import is_rebuild

            with self.control.registry.transaction() as db:
                index_operation = is_rebuild(db, batch, application_id)
            self.control.allow(
                self.actor,
                "rebuild_index" if index_operation else "apply_config",
                batch,
            )
        elif rollback_id is not None:
            self.control.allow(self.actor, "apply_config", batch)
        with self.locked(batch):
            owner, _, document, _, manifest = self.control.read(
                self.actor, batch, revision
            )
            with self.control.registry.transaction() as db:
                current = self.control.registry.row(db, batch)
                if not self.store.revision_allowed(
                    db,
                    current,
                    revision,
                    application_id,
                    rollback_id,
                    rebuild_rollback_id,
                ):
                    raise Conflict(
                        "Runtime requires the active revision or its pending application"
                    )
            startup, environment = compile_startup(
                self.control,
                self.actor,
                owner,
                revision,
                document,
                manifest,
                self.policy,
            )
            existing = self._inspect(batch)
            if existing is not None:
                spec = self._spec(self.store.get(batch))["startup"]
                # Recipe-only revisions do not change the RAG process, but a
                # deployment-policy or storage-endpoint change does.
                ignored = {"config_revision_id", "bridge"}
                if {k: v for k, v in spec.items() if k not in ignored} != {
                    k: v for k, v in startup.items() if k not in ignored
                }:
                    raise Conflict(
                        "The running startup snapshot differs; stop it explicitly before restarting"
                    )
                return existing
            previous = self.store.get(batch)
            if (
                previous
                and previous["state"] not in HELD
                and previous.get("pid") is not None
                and self.backend.ownership(previous) not in {"gone", "reused"}
            ):
                raise Conflict(
                    "Previous process exit cannot be confirmed; reservation cannot be replaced"
                )
            record = self.store.reserve(
                batch,
                revision,
                digest(startup),
                startup["rag_digest"],
                self.policy.max_processes,
                self.actor,
                application_id=application_id,
                rollback_id=rollback_id,
                rebuild_rollback_id=rebuild_rollback_id,
            )
            generation = record["generation"]
            directory = self.control.paths.under(
                self.control.paths.runtime_root,
                "batch-runtime",
                owner["namespace"],
                f"generation-{generation}",
            )
            spec = {
                **record,
                "startup": startup,
                "capability": secrets.token_hex(32),
                "attempt_directory": str(directory),
                "spec_path": str(directory / "spec.json"),
                "python": str(self.policy.python),
                "registry": str(self.control.paths.registry),
                "control_paths": {
                    "config_root": str(self.control.paths.config_root),
                    "runtime_root": str(self.control.paths.runtime_root),
                },
            }
            launch_started = False
            try:
                directory.mkdir(parents=True, exist_ok=False, mode=0o700)
                atomic_json(directory / "spec.json", spec)
                atomic_json(directory / "bridge.json", startup["bridge"])
                record = self.store.update(
                    batch,
                    generation,
                    {"reserved"},
                    self.actor,
                    "startup_published",
                    attempt_directory=str(directory),
                    spec_path=spec["spec_path"],
                    python=spec["python"],
                    capability_sha256=hashlib.sha256(
                        spec["capability"].encode()
                    ).hexdigest(),
                )
                environment["LIGHTRAG_DATA_WORKFLOW_CONFIG"] = str(
                    directory / "bridge.json"
                )
                launch_started = True
                pid, identity = self.backend.launch(spec, environment)
                self.store.attach(
                    batch, generation, record["startup_digest"], pid, identity
                )
                deadline = time.monotonic() + self.policy.startup_timeout
                while time.monotonic() < deadline:
                    ready = self._inspect(batch)
                    if ready is not None:
                        return ready
                    record = self.store.get(batch)
                    if record["state"] == "failed":
                        raise RuntimeError(
                            "Managed RAG startup failed; inspect its private worker.log"
                        )
                    time.sleep(0.05)
                raise TimeoutError(
                    "Managed RAG did not become ready before its deadline"
                )
            except BaseException:
                record = self.store.get(batch)
                if (
                    record
                    and record["generation"] == generation
                    and record["state"] in HELD
                ):
                    if (
                        record.get("pid") is not None
                        and self.backend.ownership(record) == "owned"
                    ):
                        try:
                            self.backend.terminate(record, self.policy.stop_timeout)
                        except Exception:
                            self.store.update(
                                batch,
                                generation,
                                HELD,
                                self.actor,
                                "startup_cleanup_unconfirmed",
                                state="unknown",
                                error="Child shutdown could not be confirmed; reservation remains held",
                            )
                            raise
                    confirmed = (
                        not launch_started
                        or record.get("pid") is not None
                        and self.backend.ownership(record)
                        in {
                            "gone",
                            "reused",
                        }
                    )
                    self.store.update(
                        batch,
                        generation,
                        HELD,
                        self.actor,
                        "startup_failed",
                        state="failed" if confirmed else "unknown",
                        launch_not_started=not launch_started,
                        error="Startup failed; uncertain child identity retains its reservation",
                    )
                raise

    def stop(self, batch):
        self.control.allow(self.actor, "stop_runtime", batch)
        with self.locked(batch):
            record = self.store.get(batch)
            if not record or record["state"] not in HELD:
                return record
            if not record.get("pid"):
                raise Conflict(
                    "No process identity is recorded; reservation cannot be safely released"
                )
            self._spec(record)
            ownership = self.backend.ownership(record)
            if ownership not in {"owned", "gone"}:
                raise Conflict("Process ownership cannot be verified; no signal sent")
            record = self.store.update(
                batch,
                record["generation"],
                HELD,
                self.actor,
                "stop_requested",
                state="stopping",
            )
            self.backend.terminate(record, self.policy.stop_timeout)
            return self.store.update(
                batch,
                record["generation"],
                {"stopping"},
                self.actor,
                "stopped",
                state="stopped",
            )

    def events(self, batch):
        self.control.allow(self.actor, "audit", batch)
        self.control.registry.get(batch)
        return self.store.events(batch)
