"""Journaled local-host ownership transfer; source paths come only from deployment."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx
import psutil
from pydantic import Field, model_validator

from .adoption_files import backup, verify_backup
from .adoption_snapshot import file_sha256, verify_snapshot
from .bindings import ALLOWED_ENVIRONMENT, BindingStore, StorageBinding
from .contracts import Contract, Name, Configuration, identifier
from .references import Catalog
from .resources import digest
from .runtime import RuntimeSupervisor
from .runtime_process import process_scope
from .runtime_config import ModelProfile, EmbeddingProfile, _credential, compile_startup
from .service import BatchControl
from .storage import Conflict, IntegrityError
from .writer_claim import WorkspaceClaim, managed_owner, read_json, sync_directory

RESOURCE_NAMES = {"PROMPT_DIR", "ENTITY_IDENTITY_REGISTRY_FILE"}


class LegacySource(Contract):
    url: str
    workspace: Name
    working_directory: str
    input_directory: str
    command_directory: str
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_id: Name
    key_name_of_api_key: str = "LIGHTRAG_API_KEY"
    profile_refs: dict[str, Name] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def local(self):
        url = urlsplit(self.url)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Adoption requires a credential-free local-host source URL"
            )
        for name in ("working_directory", "input_directory", "command_directory"):
            path = Path(getattr(self, name))
            if not path.is_absolute() or path != path.resolve():
                raise ValueError(
                    "Adoption source directories must be canonical absolute paths"
                )
        if set(self.resources) - RESOURCE_NAMES:
            raise ValueError("Unsupported source resource")
        for value in self.resources.values():
            path = Path(value)
            if not path.is_absolute() or path != path.resolve():
                raise ValueError(
                    "Source resource paths must be canonical absolute paths"
                )
        if set(self.profile_refs) - {
            "primary",
            "query",
            "extract",
            "keyword",
            "vlm",
            "embedding",
        }:
            raise ValueError("Unsupported source profile role")
        return self


class AdoptionStore:
    def __init__(self, registry):
        self.registry = registry

    @contextmanager
    def transaction(self):
        with self.registry.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS adoptions (id TEXT PRIMARY KEY, source TEXT NOT NULL, workspace TEXT NOT NULL, record TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS adoption_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL, record TEXT NOT NULL)"
            )
            yield db

    @staticmethod
    def write(db, record, event):
        record["updated_at"] = time.time()
        db.execute(
            "INSERT INTO adoptions VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
            (
                record["operation_id"],
                record["source_id"],
                record["workspace"],
                json.dumps(record),
            ),
        )
        db.execute(
            "INSERT INTO adoption_events(operation,record) VALUES (?,?)",
            (record["operation_id"], json.dumps({"event": event, **record})),
        )
        return record

    def get(self, operation):
        with self.transaction() as db:
            row = db.execute(
                "SELECT record FROM adoptions WHERE id=?", (operation,)
            ).fetchone()
            if not row:
                raise KeyError(operation)
            return json.loads(row[0])

    def update(self, operation, event, **values):
        with self.transaction() as db:
            row = db.execute(
                "SELECT record FROM adoptions WHERE id=?", (operation,)
            ).fetchone()
            if not row:
                raise KeyError(operation)
            record = json.loads(row[0])
            record.update(values)
            return self.write(db, record, event)


class SourceProcess:
    """An HTTP report from a different PID namespace never authorizes a signal."""

    def check(self, source, observation, *, gone_ok=False):
        try:
            current = process_scope()
        except (OSError, RuntimeError) as exc:
            raise Conflict("Source PID namespace cannot be inspected safely") from exc
        if (
            observation["pid_namespace"] != current["namespace"]
            or observation["host_boot_id"] != current["boot"]
        ):
            raise Conflict("Source process is not visible in this host/PID namespace")
        try:
            process = psutil.Process(observation["pid"])
            if str(process.create_time()) != observation["process_identity"]:
                if gone_ok:
                    return None
                raise Conflict("Source PID identity changed")
            if process.status() == psutil.STATUS_ZOMBIE:
                if gone_ok:
                    return None
                raise Conflict("Source process has exited")
            if gone_ok:
                # Existence checks retain ownership through transient exec/exit
                # metadata gaps. A signal still requires the full check below.
                return process
            if (
                digest(process.cmdline()) != source.command_sha256
                or str(Path(process.cwd()).resolve()) != source.command_directory
            ):
                raise Conflict("Source process command or directory changed")
            return process
        except psutil.NoSuchProcess:
            if gone_ok:
                return None
            raise Conflict("Source process cannot be verified") from None

    def stop(self, source, observation):
        process = self.check(source, observation, gone_ok=True)
        if process is not None:
            # Single worker only: never signal an unrelated process group.
            process = self.check(source, observation)
            process.send_signal(signal.SIGTERM)
        return process is None


class AdoptionService:
    def __init__(
        self,
        control,
        policy,
        sources,
        *,
        actor,
        client=None,
        processes=None,
        backend=None,
    ):
        self.control, self.policy, self.sources, self.actor = (
            control,
            policy,
            sources,
            actor,
        )
        self.store = AdoptionStore(control.registry)
        self.client = client
        self.processes = processes or SourceProcess()
        self.runtime = RuntimeSupervisor(control, policy, actor=actor, backend=backend)

    def authorize(self, source_id):
        self.control.allow(self.actor, "adopt_workspace", source_id)
        self.control.allow(self.actor, "create", "batches")
        if source_id not in self.sources:
            raise KeyError(source_id)

    def request(self, source_id, name, request_id):
        self.authorize(source_id)
        source = self.sources[source_id]
        if not name.strip() or len(name) > 256:
            raise ValueError("A nonblank batch name is required")
        from pydantic import TypeAdapter

        TypeAdapter(Name).validate_python(request_id, strict=True)
        fingerprint = digest({"source": source.model_dump(), "name": name})
        with self.store.transaction() as db:
            previous = self.control.registry.prior(
                db, self.actor, "adopt", source_id, request_id, fingerprint
            )
            if previous:
                row = db.execute(
                    "SELECT record FROM adoptions WHERE id=?",
                    (previous["operation_id"],),
                ).fetchone()
                if row is None:
                    raise IntegrityError("Adoption receipt has no matching journal")
                return json.loads(row[0])
            if db.execute(
                "SELECT 1 FROM batches WHERE workspace=?", (source.workspace,)
            ).fetchone():
                raise Conflict("This workspace already belongs to a registered batch")
            if any(
                json.loads(row[0])["phase"] != "aborted"
                for row in db.execute(
                    "SELECT record FROM adoptions WHERE workspace=?",
                    (source.workspace,),
                )
            ):
                raise Conflict("Resume or abort the existing workspace transfer first")
            record = {
                "operation_id": identifier("adoption"),
                "source_id": source_id,
                "source_digest": digest(source.model_dump()),
                "workspace": source.workspace,
                "name": name,
                "batch_id": identifier("batch"),
                "revision_id": identifier("rev"),
                "actor": self.actor,
                "phase": "inspect",
                "created_at": time.time(),
                "error": None,
            }
            self.store.write(db, record, "requested")
            self.control.registry.remember(
                db,
                self.actor,
                "adopt",
                source_id,
                request_id,
                fingerprint,
                {"operation_id": record["operation_id"]},
            )
            return record

    @contextmanager
    def locked(self, operation):
        directory = self.control.paths.under(
            self.control.paths.config_root, "adoptions", operation
        )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            directory / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict("This adoption step is already in progress") from exc
            yield directory
        finally:
            os.close(descriptor)

    def remote(self, source, path, operation=None):
        self.control.allow(self.actor, "use_credential", source.credential_id)
        key = dotenv_values(
            self.control.catalog.credentials[source.credential_id], interpolate=False
        ).get(source.key_name_of_api_key)
        if not key:
            raise ValueError("Source API-key credential is missing")

        def send(client):
            response = client.request(
                "POST" if operation else "GET",
                source.url.rstrip("/") + "/batch-adoption/" + path,
                headers={"X-API-Key": key},
                **({"json": {"request_id": operation}} if operation else {}),
            )
            response.raise_for_status()
            return response.json()

        if self.client is not None:
            return send(self.client)
        with httpx.Client(trust_env=False, timeout=600) as client:
            return send(client)

    def verify_source(self, source, observation, previous=None):
        if (
            observation.get("protocol_version") != 2
            or observation.get("workers") != 1
            or not observation.get("writer_claim", {}).get("held")
            or observation["writer_claim"].get("owner") is not None
        ):
            raise Conflict(
                "Update the unmanaged source to the adoption-capable single-worker service first"
            )
        for key in ("workspace", "working_directory", "input_directory"):
            if observation.get(key) != getattr(source, key):
                raise IntegrityError("Source workspace identity differs: " + key)
        if observation.get("command_digest") != source.command_sha256:
            raise IntegrityError("Source startup command changed")
        if previous and any(
            observation.get(key) != previous.get(key)
            for key in (
                "pid",
                "process_identity",
                "boot_id",
                "pid_namespace",
                "host_boot_id",
                "collections",
                "embedding",
                "storage_endpoints",
                "role_provider_options",
                "runtime_resource_digests",
            )
        ):
            raise IntegrityError("Source identity changed during adoption")
        if previous and any(
            observation.get(key) != previous.get(key)
            for key in (
                "base_model",
                "roles",
                "embedding_profile",
                "parameters",
                "environment",
                "resource_sha256",
                "chunking_function",
                "rerank_enabled",
                "retrieval_directory",
            )
        ):
            raise IntegrityError("Source configuration changed during adoption")
        self.processes.check(source, observation)
        if (
            observation.get("rerank_enabled")
            or observation.get("chunking_function") != "chunking_by_token_size"
        ):
            raise ValueError(
                "Adoption currently requires token defaults and disabled reranking"
            )
        for name in RESOURCE_NAMES:
            if name in observation.get("resource_sha256", {}) and str(
                Path(source.resources.get(name, "")).resolve()
            ) != str(Path(observation["environment"][name]).resolve()):
                raise Conflict(
                    "Register the source resource path before adoption: " + name
                )
        if set(source.resources) != set(observation.get("resource_sha256", {})):
            raise Conflict(
                "Declared resources differ from the running source resources"
            )

    def prepare(self, source, observation, owner):
        base = self.control.defaults
        primary_ref = base.rag.model_profiles.get(
            "primary", next(iter(base.rag.model_profiles.values()))
        )
        profiles, aliases = {}, {}
        for role in ("primary", "query", "extract", "keyword", "vlm", "embedding"):
            reference = source.profile_refs.get(role)
            if reference is None:
                reference = (
                    base.rag.embedding_profile
                    if role == "embedding"
                    else base.rag.model_profiles.get(
                        base.rag.role_profiles.get(role), primary_ref
                    )
                )
            self.control.allow(self.actor, "use_profile", reference)
            model_type = EmbeddingProfile if role == "embedding" else ModelProfile
            original = model_type.model_validate(
                self.control.catalog.profiles[reference]
            )
            actual = (
                observation["base_model"]
                if role == "primary"
                else observation["embedding_profile"]
                if role == "embedding"
                else observation["roles"][role]
            )
            secret = _credential(self.control, self.actor, original)
            if (
                hashlib.sha256(secret.encode()).hexdigest() if secret else None
            ) != actual["credential_sha256"]:
                raise Conflict(
                    "Source credential does not match its profile reference: " + role
                )
            values = {
                **original.model_dump(),
                "binding": actual["binding"],
                "model_name": actual["model"],
                "api_url": actual["host"],
                "max_async": actual["max_async"],
                "timeout": actual["timeout"],
            }
            if role == "embedding":
                values.update(
                    dimensions=observation["embedding"]["dimensions"],
                    batch_size=actual["batch_size"],
                    max_tokens=actual["max_tokens"],
                )
            value = model_type.model_validate(values).model_dump()
            name = "adopted-" + role + "-" + digest(value)[:20]
            profiles[name], aliases[role] = value, name
        catalog = Catalog(
            environments=self.control.catalog.environments,
            credentials=self.control.catalog.credentials,
            inputs=self.control.catalog.inputs,
            profiles={**self.control.catalog.profiles, **profiles},
        )
        isolated = BatchControl(
            self.control.paths,
            base,
            catalog,
            self.control.authorize,
            probe=self.control.probe,
        )
        isolated.provider_limits = self.control.provider_limits
        rag = {
            **base.rag.model_dump(),
            **{
                key: observation["parameters"][key]
                for key in (
                    "chunk_size",
                    "chunk_overlap_size",
                    "max_parallel_insert",
                    "max_parallel_parse_native",
                )
            },
            "chunking_strategy": "token",
            "parse_engine": "native",
            "model_profiles": {
                role: reference
                for role, reference in aliases.items()
                if role != "embedding"
            },
            "role_profiles": {
                role: role for role in ("query", "extract", "keyword", "vlm")
            },
            "role_concurrency": {
                role: observation["roles"][role]["max_async"]
                for role in ("query", "extract", "keyword", "vlm")
            },
            "embedding_profile": aliases["embedding"],
        }
        document = Configuration.model_validate({**base.model_dump(), "rag": rag})
        document, prompts, execution, bridge = isolated._prepare(
            self.actor, owner, document, {}
        )
        manifest = {
            "sources": {},
            "components": isolated.files.components(document, prompts, execution),
            "execution": execution,
        }
        startup, _ = compile_startup(
            isolated,
            self.actor,
            owner,
            owner["active"],
            document,
            manifest,
            self.policy,
        )
        mode = (
            "neo4j_milvus"
            if observation["storage_classes"]["vector_storage"]
            == "MilvusVectorDBStorage"
            else "local"
        )
        if mode != self.policy.storage_mode:
            raise Conflict("Source backend differs from the managed storage policy")
        if mode == "neo4j_milvus":
            environment = startup["environment"]
            actual = {
                "neo4j_uri": environment.get("NEO4J_URI", ""),
                "neo4j_database": environment.get("NEO4J_DATABASE", ""),
                "milvus_uri": environment.get("MILVUS_URI", ""),
                "milvus_database": environment.get("MILVUS_DB_NAME", "default"),
            }
            if not all(actual.values()) or actual != observation["storage_endpoints"]:
                raise Conflict(
                    "Managed database endpoints differ from the existing source"
                )
        return {
            "document": document.model_dump(),
            "prompts": prompts,
            "execution": execution,
            "bridge": bridge,
            "profiles": profiles,
            "storage_mode": mode,
        }

    def step(self, operation):
        record = self.store.get(operation)
        self.authorize(record["source_id"])
        source = self.sources[record["source_id"]]
        if digest(source.model_dump()) != record["source_digest"]:
            raise Conflict("The declared source changed; inspect the transfer journal")
        if record["phase"] in {"ready", "aborted"}:
            return record
        with self.locked(operation) as directory:
            record = self.store.get(operation)
            owner = {
                "id": record["batch_id"],
                "workspace": source.workspace,
                "namespace": hashlib.sha256(source.workspace.encode()).hexdigest()[:16],
                "name": record["name"],
                "active": record["revision_id"],
            }
            try:
                if record["phase"] == "inspect":
                    observation = self.remote(source, "inspect")
                    self.verify_source(source, observation)
                    prepared = self.prepare(source, observation, owner)
                    return self.store.update(
                        operation,
                        "preflight_verified",
                        phase="freeze",
                        observation=observation,
                        prepared=prepared,
                        error=None,
                    )
                if record["phase"] == "freeze":
                    observation = self.remote(source, "freeze", operation)
                    self.verify_source(source, observation, record["observation"])
                    if (
                        observation["freeze_owner"] != operation
                        or observation["busy"]
                        or any(
                            value
                            for name, value in observation["document_states"].items()
                            if name not in {"processed", "failed", "skipped"}
                        )
                    ):
                        raise Conflict(
                            "Wait for all existing document tasks to become terminal"
                        )
                    return self.store.update(
                        operation,
                        "source_frozen",
                        phase="snapshot",
                        observation=observation,
                        error=None,
                    )
                if record["phase"] == "snapshot":
                    self.processes.check(source, record["observation"])
                    self.remote(source, "snapshot", operation)
                    snapshot_dir = (
                        Path(source.working_directory)
                        / ".adoption-snapshots"
                        / source.workspace
                        / operation
                    )
                    snapshot = verify_snapshot(snapshot_dir, record["observation"])
                    return self.store.update(
                        operation,
                        "remote_data_snapshotted",
                        phase="stopping",
                        snapshot=snapshot,
                        snapshot_directory=str(snapshot_dir),
                        error=None,
                    )
                if record["phase"] == "stopping":
                    alive = self.processes.check(
                        source, record["observation"], gone_ok=True
                    )
                    if alive is not None:
                        if record.get("stop_requested_at") is not None:
                            return self.store.update(
                                operation,
                                "waiting_for_source_exit",
                                waiting_reason="Stop was requested; wait for confirmed process exit. An interrupted signal intent requires explicit retry-stop.",
                            )
                        current = self.remote(source, "inspect")
                        self.verify_source(source, current, record["observation"])
                        if current["freeze_owner"] != operation or current["busy"]:
                            raise Conflict("Source freeze was lost before shutdown")
                        self.store.update(
                            operation,
                            "source_stop_requested",
                            stop_requested_at=time.time(),
                        )
                        if not self.processes.stop(source, record["observation"]):
                            return self.store.update(
                                operation,
                                "waiting_for_source_exit",
                                waiting_reason="Waiting for confirmed source process exit",
                            )
                    return self.store.update(
                        operation,
                        "source_exit_verified",
                        phase="backup",
                        waiting_reason=None,
                        error=None,
                    )
                expected = managed_owner(owner["id"], self.control.paths.registry)
                if record["phase"] in {"backup", "register"}:
                    if (
                        self.processes.check(
                            source, record["observation"], gone_ok=True
                        )
                        is not None
                    ):
                        raise Conflict("Source process remains active")
                    with WorkspaceClaim(
                        source.working_directory,
                        source.workspace,
                        owner=expected,
                        publish=False,
                    ) as claim:
                        if record["phase"] == "backup":
                            attempts = directory / "backups"
                            attempts.mkdir(exist_ok=True, mode=0o700)
                            destination = attempts / identifier("backup")
                            copied = backup(
                                source,
                                destination,
                                record["snapshot_directory"],
                                source.resources,
                                retrieval_directory=record["observation"][
                                    "retrieval_directory"
                                ],
                            )
                            if {
                                name: value["sha256"]
                                for name, value in copied["resources"].items()
                            } != record["observation"].get("resource_sha256", {}):
                                raise IntegrityError(
                                    "Source resource changed after its frozen inspection"
                                )
                            return self.store.update(
                                operation,
                                "backup_verified",
                                phase="register",
                                backup_directory=str(destination),
                                backup=copied,
                                error=None,
                            )
                        if (
                            verify_backup(record["backup_directory"])
                            != record["backup"]
                        ):
                            raise IntegrityError("Adoption backup manifest changed")
                        checked_snapshot = verify_snapshot(
                            record["snapshot_directory"], record["observation"]
                        )
                        if checked_snapshot != record["snapshot"]:
                            raise IntegrityError("Adoption snapshot manifest changed")
                        claim.transfer(expected)
                        return self.register(record, source, owner)
                if record["phase"] == "registered":
                    lease = self.runtime.start(owner["id"], owner["active"])
                    if (
                        BindingStore(self.control.registry).get(owner["id"])["state"]
                        != "adopted"
                    ):
                        raise IntegrityError(
                            "Managed child did not verify the adopted storage"
                        )
                    return self.store.update(
                        operation,
                        "adoption_verified",
                        phase="ready",
                        lease=lease.model_dump(),
                        error=None,
                        finished_at=time.time(),
                    )
                raise IntegrityError("Unsupported adoption journal phase")
            except Exception as exc:
                from lightrag.utils import logger

                logger.exception("Workspace adoption step failed: %s", operation)
                return self.store.update(
                    operation,
                    "step_waiting",
                    error=type(exc).__name__ + ": inspect adoption logs",
                    waiting_reason="Transfer remains at its last durable phase; no data was deleted",
                )

    def register(self, record, source, owner):
        observation, prepared = record["observation"], record["prepared"]
        environment = {
            key: value
            for key, value in observation["environment"].items()
            if key in ALLOWED_ENVIRONMENT
        }
        environment.update(
            {
                name: value["path"]
                for name, value in record["backup"]["resources"].items()
            }
        )
        binding = StorageBinding(
            workspace=source.workspace,
            working_directory=source.working_directory,
            input_directory=source.input_directory,
            storage_mode=prepared["storage_mode"],
            collections=observation["collections"],
            embedding=observation["embedding"],
            graph=observation["graph"],
            vectors=record["snapshot"]["vectors"],
            document_states=observation["document_states"],
            auxiliary_collections=observation["auxiliary_collections"],
            storage_endpoints=observation["storage_endpoints"],
            role_provider_options=observation.get("role_provider_options", {}),
            runtime_resource_digests=observation.get("runtime_resource_digests", {}),
            environment=environment,
            resources=record["backup"]["resources"],
            retrieval_directory=observation["retrieval_directory"],
            source_observation_sha256=digest(observation),
        ).model_dump()
        sources = {
            "storage_binding_digest": digest(binding),
            "adoption_id": record["operation_id"],
            "backup_manifest_sha256": file_sha256(
                Path(record["backup_directory"]) / "manifest.json"
            ),
        }
        document = Configuration.model_validate(prepared["document"])
        location = self.control.paths.revision(owner["id"], owner["active"])
        if location.exists():
            manifest = read_json(location / "manifest.json")
            revision = {
                "id": owner["active"],
                "directory": str(location),
                "digest": file_sha256(location / "manifest.json"),
                "components": manifest["components"],
            }
            _, _, checked = self.control.files.read(owner, revision)
            if (
                checked["sources"] != sources
                or checked["execution"] != prepared["execution"]
                or checked["components"]
                != self.control.files.components(
                    document, prepared["prompts"], prepared["execution"]
                )
            ):
                raise IntegrityError(
                    "Existing adoption configuration differs from the journal"
                )
        else:
            revision = self.control.files.publish(
                owner,
                owner["active"],
                document,
                prepared["prompts"],
                prepared["execution"],
                prepared["bridge"],
                sources,
            )
        bindings = BindingStore(self.control.registry)
        with bindings.transaction() as db:
            existing = db.execute(
                "SELECT id,active FROM batches WHERE workspace=?", (source.workspace,)
            ).fetchone()
            if existing:
                if (
                    existing["id"] != owner["id"]
                    or existing["active"] != owner["active"]
                ):
                    raise Conflict("Workspace was registered by a different transfer")
                row = db.execute(
                    "SELECT digest FROM storage_bindings WHERE batch=?", (owner["id"],)
                ).fetchone()
                if not row or row[0] != digest(binding):
                    raise IntegrityError(
                        "Registered adoption binding differs from its journal"
                    )
            else:
                db.execute(
                    "INSERT INTO batches(id,workspace,namespace,name,active,index_state,lifecycle) VALUES (?,?,?,?,?,'unknown','registered')",
                    (
                        owner["id"],
                        owner["workspace"],
                        owner["namespace"],
                        owner["name"],
                        owner["active"],
                    ),
                )
                self.control.registry.add_revision(db, owner["id"], revision)
                bindings.register(db, owner["id"], binding, prepared["profiles"])
            record.update(phase="registered", error=None, waiting_reason=None)
            self.store.write(db, record, "workspace_registered")
        self.control.catalog.profiles.update(prepared["profiles"])
        return record

    def public(self, operation):
        record = self.store.get(operation)
        self.authorize(record["source_id"])
        return {
            key: record.get(key)
            for key in (
                "operation_id",
                "source_id",
                "workspace",
                "batch_id",
                "revision_id",
                "phase",
                "error",
                "waiting_reason",
                "created_at",
                "updated_at",
                "finished_at",
                "stop_requested_at",
            )
        }

    def retry_stop(self, operation):
        record = self.store.get(operation)
        self.authorize(record["source_id"])
        source = self.sources[record["source_id"]]
        if digest(source.model_dump()) != record["source_digest"]:
            raise Conflict("The declared source changed")
        with self.locked(operation):
            record = self.store.get(operation)
            if record["phase"] != "stopping" or record.get("stop_requested_at") is None:
                raise Conflict("There is no interrupted source stop to retry")
            if (
                self.processes.check(source, record["observation"], gone_ok=True)
                is not None
            ):
                current = self.remote(source, "inspect")
                self.verify_source(source, current, record["observation"])
                if current["freeze_owner"] != operation or current["busy"]:
                    raise Conflict("The original source freeze cannot be verified")
            return self.store.update(
                operation,
                "source_stop_retry_authorized",
                stop_requested_at=None,
                error=None,
            )

    def abort(self, operation):
        record = self.store.get(operation)
        self.authorize(record["source_id"])
        source = self.sources[record["source_id"]]
        if digest(source.model_dump()) != record["source_digest"]:
            raise Conflict("Restore the declared source before aborting its transfer")
        with self.locked(operation):
            record = self.store.get(operation)
            if record["phase"] == "aborted":
                return record
            with self.control.registry.transaction() as db:
                registered = db.execute(
                    "SELECT 1 FROM batches WHERE workspace=?", (source.workspace,)
                ).fetchone()
            if registered or record["phase"] in {"registered", "ready"}:
                raise Conflict("A registered workspace must retain its managed owner")
            observation = record.get("observation")
            if observation is not None:
                alive = self.processes.check(source, observation, gone_ok=True)
                if alive is not None:
                    current = self.remote(source, "inspect")
                    self.verify_source(source, current, observation)
                    if current["freeze_owner"] not in {None, operation}:
                        raise Conflict(
                            "A different transfer now owns the source freeze"
                        )
                    if current["freeze_owner"] == operation:
                        self.remote(source, "unfreeze", operation)
                else:
                    owner = managed_owner(
                        record["batch_id"], self.control.paths.registry
                    )
                    with WorkspaceClaim(
                        source.working_directory,
                        source.workspace,
                        owner=owner,
                        publish=False,
                    ) as claim:
                        if claim.owner_file.exists():
                            if read_json(claim.owner_file) != owner:
                                raise Conflict(
                                    "A different managed owner controls this workspace"
                                )
                            claim.owner_file.unlink()
                            sync_directory(claim.directory)
            return self.store.update(
                operation,
                "transfer_aborted",
                phase="aborted",
                error=None,
                waiting_reason=None,
                finished_at=time.time(),
            )
