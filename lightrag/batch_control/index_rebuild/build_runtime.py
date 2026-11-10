"""Reserve and observe a builder separately from normal business readiness.

Reconstructed managers observe the exact prior attempt; they never replay it.
The coordinator must confirm process exit before moving to a candidate.
See docs/design/BatchIndexRebuild.md.
"""

import hashlib
import secrets
import time

from ..resources import digest
from ..runtime_config import compile_startup
from ..runtime_process import atomic_json
from ..runtime_store import HELD
from ..storage import Conflict, IntegrityError
from .authority import VectorBinding, startup_binding
from .builder import read_verified
from .verification import compiled_transform, context


class BuilderRuntime:
    def __init__(self, supervisor):
        self.runtime = supervisor
        self.control, self.actor = supervisor.control, supervisor.actor

    def allowed(self, batch):
        self.control.allow(self.actor, "run_runtime", batch)
        self.control.allow(self.actor, "rebuild_index", batch)

    def receipt(self, batch, operation, record=None):
        self.allowed(batch)
        runtime = record or self.runtime.store.get(batch)
        if (
            not runtime
            or runtime.get("purpose") != "index_build"
            or runtime.get("application_id") != operation
        ):
            raise Conflict("No exact builder reservation owns this receipt")
        spec = self.runtime._spec(runtime)
        index = startup_binding(self.control.registry, self.control.paths, spec)
        record, root, audit = context(
            self.control.registry, self.control.paths, batch, operation
        )
        target = VectorBinding.model_validate(index["binding"]).target(
            audit.identity, record["source_evidence"]["manifest_sha256"]
        )
        result = read_verified(
            root / "build",
            root / "source",
            target,
            transform=compiled_transform(spec["startup"]),
        )
        return result, hashlib.sha256(
            (root / "build" / "verified.json").read_bytes()
        ).hexdigest()

    def _inspect(self, batch, operation):
        runtime = self.runtime
        row = runtime.store.get(batch)
        if row is None:
            return None, None
        if (
            row.get("purpose") != "index_build"
            or row.get("application_id") != operation
        ):
            raise Conflict("Another runtime owns this batch")
        ownership = runtime.backend.ownership(row)
        if ownership in {"gone", "reused"}:
            if row["state"] in HELD:
                row = runtime.store.update(
                    batch,
                    row["generation"],
                    HELD,
                    self.actor,
                    "builder_exited",
                    state="failed",
                    error="Builder exited; inspect retained receipt and log before recovery",
                )
            return row, None
        if ownership != "owned" or row["state"] not in HELD:
            return row, None
        spec = runtime._spec(row)
        if spec["startup"].get("purpose") != "index_build":
            raise IntegrityError("Builder startup purpose changed")
        proof = runtime.backend.observe(row)
        if proof is None or proof.get("state") not in {"index_building", "index_built"}:
            return row, None
        index = startup_binding(self.control.registry, self.control.paths, spec)
        current, _, audit = context(
            self.control.registry, self.control.paths, batch, operation
        )
        target = VectorBinding.model_validate(index["binding"]).target(
            audit.identity, current["source_evidence"]["manifest_sha256"]
        )
        if (
            any(
                proof.get(key) != row.get(key)
                for key in (
                    "data_batch_id",
                    "generation",
                    "lease_id",
                    "startup_digest",
                    "workspace",
                    "pid",
                    "process_identity",
                    "process_scope",
                )
            )
            or not secrets.compare_digest(
                str(proof.get("capability", "")), spec["capability"]
            )
            or proof.get("provider_admission") != spec["startup"]["provider_admission"]
            or proof.get("index_binding_id") != operation
            or proof.get("index_binding_digest") != index["digest"]
            or proof.get("target") != target.model_dump()
        ):
            raise IntegrityError(
                "Builder observation disagrees with its owned reservation"
            )
        age = time.time() - proof.get("observed_at", 0)
        if age < -2 or age > runtime.policy.health_timeout:
            return row, None
        progress = proof.get("progress", {})
        if any(
            type(progress.get(key)) is not int or progress[key] < 0
            for key in ("total", "written", "verified")
        ) or any(progress[key] > progress["total"] for key in ("written", "verified")):
            raise IntegrityError("Builder progress counts are invalid")
        if proof["state"] == "index_built":
            receipt, checksum = self.receipt(batch, operation, row)
            if checksum != proof.get("receipt_sha256") or receipt["counts"] != {
                key: progress[key] for key in ("total", "written", "verified")
            }:
                raise IntegrityError(
                    "Builder completed observation lost its sealed receipt"
                )
            if row["state"] == "starting":
                row = runtime.store.update(
                    batch,
                    row["generation"],
                    {"starting"},
                    self.actor,
                    "index_built",
                    state="built",
                    collections=target.collections,
                )
        return row, proof

    def inspect(self, batch, operation):
        self.allowed(batch)
        with self.runtime.locked(batch):
            return self._inspect(batch, operation)

    def start(self, batch, operation):
        self.allowed(batch)
        runtime = self.runtime
        with runtime.locked(batch):
            current, _, _ = context(
                self.control.registry, self.control.paths, batch, operation
            )
            if (
                current["state"] not in {"builder_starting", "building"}
                or current["cancel_requested"]
            ):
                raise Conflict("Rebuild is not admitting a builder")
            owner, _, document, _, manifest = self.control.read(
                self.actor, batch, current["config_revision_id"]
            )
            startup, environment = compile_startup(
                self.control,
                self.actor,
                owner,
                current["config_revision_id"],
                document,
                manifest,
                runtime.policy,
                index_purpose="index_build",
            )
            previous = runtime.store.get(batch)
            if previous and previous.get("purpose") == "index_build":
                if previous.get("application_id") != operation:
                    raise Conflict("Another builder attempt owns this batch")
                previous, _ = self._inspect(batch, operation)
                if previous["state"] not in HELD:
                    raise Conflict(
                        "An exited builder cannot be replayed; inspect its retained receipt"
                    )
                if runtime._spec(previous)["startup"] != startup:
                    raise Conflict("The recorded builder startup snapshot differs")
                return previous
            if not previous or runtime.backend.ownership(previous) not in {
                "gone",
                "reused",
            }:
                raise Conflict("Source process exit must be confirmed before building")
            row = runtime.store.reserve(
                batch,
                current["config_revision_id"],
                digest(startup),
                startup["rag_digest"],
                runtime.policy.max_processes,
                self.actor,
                application_id=operation,
                purpose="index_build",
            )
            generation = row["generation"]
            directory = self.control.paths.under(
                self.control.paths.runtime_root,
                "batch-runtime",
                owner["namespace"],
                f"generation-{generation}",
            )
            spec = {
                **row,
                "startup": startup,
                "capability": secrets.token_hex(32),
                "attempt_directory": str(directory),
                "spec_path": str(directory / "spec.json"),
                "python": str(runtime.policy.python),
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
                runtime.store.update(
                    batch,
                    generation,
                    {"reserved"},
                    self.actor,
                    "builder_startup_published",
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
                pid, identity = runtime.backend.launch(spec, environment)
                return runtime.store.attach(
                    batch, generation, row["startup_digest"], pid, identity
                )
            except BaseException:
                failed = runtime.store.get(batch)
                if (
                    failed
                    and failed["generation"] == generation
                    and failed["state"] in HELD
                ):
                    ownership = (
                        runtime.backend.ownership(failed)
                        if failed.get("pid")
                        else "unknown"
                    )
                    if ownership == "owned":
                        runtime.backend.terminate(failed, runtime.policy.stop_timeout)
                        ownership = runtime.backend.ownership(failed)
                    confirmed = not launch_started or ownership in {"gone", "reused"}
                    runtime.store.update(
                        batch,
                        generation,
                        HELD,
                        self.actor,
                        "builder_launch_failed",
                        state="failed" if confirmed else "unknown",
                        launch_not_started=not launch_started,
                        error="Builder launch failed; uncertain process ownership retains its reservation",
                    )
                raise
