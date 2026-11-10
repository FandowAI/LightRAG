"""Authenticated rebuild requests and progress, separate from configuration apply.

Never accept physical storage paths or replay a failed paid build. Explicit
recovery only retries restoration of the prior index. See docs/design/BatchIndexRebuild.md.
"""

from contextlib import contextmanager
import fcntl
from types import SimpleNamespace

from pydantic import TypeAdapter

from ..contracts import Configuration, Name, identifier
from ..resources import digest
from ..runtime import RuntimeSupervisor
from ..runtime_config import EmbeddingProfile
from ..storage import Conflict, IntegrityError
from .authority import VectorBinding
from .builder import target_namespace
from .source import SourceIdentity
from .store import RebuildStore


class IndexRebuilds:
    def __init__(self, control, policy, *, actor, backend=None):
        self.control, self.policy, self.actor = control, policy, actor
        self.runtime = RuntimeSupervisor(control, policy, actor=actor, backend=backend)
        self.store = RebuildStore(control.registry)

    def authorize(self, batch):
        for action in ("read", "edit", "rebuild_index", "run_runtime", "stop_runtime"):
            self.control.allow(self.actor, action, batch)

    @contextmanager
    def locked(self, batch):
        self.control.allow(self.actor, "read", batch)
        self.control.registry.get(batch)
        path = self.control.paths.under(
            self.control.paths.runtime_root,
            "batch-control",
            "locks",
            batch + ".index.lock",
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path != path.resolve():
            raise IntegrityError("Index operation lock was redirected")
        with path.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict("Another index operation step is in progress") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def request(self, batch, expected_revision, embedding_profile, request_id):
        self.authorize(batch)
        for value in (expected_revision, embedding_profile, request_id):
            TypeAdapter(Name).validate_python(value, strict=True)
        fingerprint = digest(
            {"revision": expected_revision, "embedding_profile": embedding_profile}
        )
        with self.locked(batch):
            prior = self.control.registry.previous(
                self.actor, "index_rebuild", batch, request_id, fingerprint
            )
            if prior:
                return self.store.get(batch, prior["operation_id"])
            owner, _, before, prompts, manifest = self.control.read(self.actor, batch)
            if owner["pending"] or owner["active"] != expected_revision:
                raise Conflict(
                    "Active revision changed or another operation is pending"
                )
            self.control.allow(self.actor, "use_profile", embedding_profile)
            value = before.model_dump()
            value["rag"]["embedding_profile"] = embedding_profile
            document, prompts, execution, bridge = self.control._prepare(
                self.actor,
                owner,
                Configuration.model_validate(value),
                self.control._inherit(before, prompts),
            )
            profile = EmbeddingProfile.model_validate(
                execution["rag_profiles"][embedding_profile]
            )
            lease = self.runtime.start(batch, expected_revision)
            with self.runtime.locked(batch):
                actual = self.runtime._inspect(batch)
                if actual != lease:
                    raise Conflict(
                        "Source generation changed before rebuild reservation"
                    )
                proof = self.runtime._proof(self.runtime.store.get(batch))
                auxiliary = proof.get("auxiliary_collections", {})
                if set(auxiliary) - {"name_entities"}:
                    raise IntegrityError(
                        "Unsupported auxiliary vector role in the source"
                    )
                operation, revision_id = identifier("op"), identifier("rev")
                namespace = target_namespace(batch, operation)
                suffix = ""
                if self.policy.storage_mode == "neo4j_milvus":
                    from lightrag.base import BaseVectorStorage

                    suffix = "_" + BaseVectorStorage._generate_collection_suffix(
                        SimpleNamespace(
                            embedding_func=SimpleNamespace(
                                model_name=profile.model_name,
                                embedding_dim=profile.dimensions,
                            )
                        )
                    )
                roles = ["entities", "relationships", "chunks", *auxiliary]
                binding = VectorBinding(
                    data_batch_id=batch,
                    operation_id=operation,
                    config_revision_id=revision_id,
                    source_workspace=owner["workspace"],
                    vector_workspace=namespace,
                    storage_mode=self.policy.storage_mode,
                    embedding_profile=embedding_profile,
                    embedding_profile_sha256=digest(
                        execution["rag_profiles"][embedding_profile]
                    ),
                    embedding_model=profile.model_name,
                    dimensions=profile.dimensions,
                    collections={
                        role: namespace + "_" + role + suffix for role in roles
                    },
                )
                revision = self.control.files.publish(
                    owner,
                    revision_id,
                    document,
                    prompts,
                    execution,
                    bridge,
                    {
                        **manifest["sources"],
                        "parent_revision": expected_revision,
                        "index_binding_id": operation,
                        "index_binding_digest": digest(binding.model_dump()),
                    },
                )
                source = SourceIdentity(
                    data_batch_id=batch,
                    operation_id=operation,
                    config_revision_id=expected_revision,
                    workspace=owner["workspace"],
                    generation=lease.generation,
                    lease_id=lease.lease_id,
                    startup_digest=lease.startup_digest,
                )
                return self.store.reserve(
                    self.actor,
                    request_id,
                    fingerprint,
                    revision,
                    binding,
                    source,
                    lease,
                )

    def public(self, batch, operation):
        self.control.allow(self.actor, "read", batch)
        record = self.store.get(batch, operation)
        binding = self.store.binding(batch, operation)
        base = f"/data-batches/{batch}/index-rebuilds/{operation}"
        executable = all(
            self.control.authorize(self.actor, action, batch)
            for action in ("edit", "rebuild_index", "run_runtime", "stop_runtime")
        )
        from ..provider_quota.core import QuotaStore
        from ..provider_quota.wiring import registry_path

        quota = registry_path(self.control.paths)
        return {
            **{
                key: record.get(key)
                for key in (
                    "operation_id",
                    "data_batch_id",
                    "config_revision_id",
                    "previous_revision",
                    "state",
                    "version",
                    "terminal",
                    "created_at",
                    "updated_at",
                    "finished_at",
                    "error",
                    "rollback_error",
                    "waiting_reason",
                    "cancel_requested",
                    "progress",
                )
            },
            "target": {
                key: binding["binding"][key]
                for key in (
                    "embedding_profile",
                    "embedding_model",
                    "dimensions",
                    "vector_workspace",
                    "collections",
                )
            },
            "binding_state": binding["state"],
            "provider_admission": QuotaStore(quota).summary(batch, operation)
            if quota.is_file()
            else {"items": []},
            "can_cancel": executable
            and not record["terminal"]
            and not record["state"].startswith("rollback_"),
            "can_recover": executable and record["state"] == "rollback_failed",
            "urls": {
                "status": base,
                "cancel": base + "/cancel",
                "recover": base + "/recover",
                "events": base + "/events",
            },
        }

    def list(self, batch, limit=20):
        self.control.allow(self.actor, "read", batch)
        return {
            "items": [
                self.public(batch, row["operation_id"])
                for row in self.store.list(batch, limit)
            ]
        }

    def cancel(self, batch, operation):
        self.authorize(batch)
        return self.store.request_cancel(batch, operation)

    def recover(self, batch, operation, version):
        self.authorize(batch)
        with self.locked(batch):
            record = self.store.get(batch, operation)
            if record["state"] != "rollback_failed":
                raise Conflict("Only failed prior-index restoration can be recovered")
            return self.store.transition(
                batch,
                operation,
                version,
                event="explicit_recovery_requested",
                state="rollback_stopping",
                executor_actor=self.actor,
                rollback_error=None,
                waiting_reason=None,
            )

    def step(self, batch, operation):
        from .controller import RebuildController

        return RebuildController(self).step(batch, operation)
