"""Build fresh vector namespaces and seal persisted evidence without activation.

Caller owns the source fence, process reservation and provider admission. Never
finalize a failed attempt in order to retry buffered paid embeddings.
See docs/design/BatchIndexRebuild.md for the remaining coordinator boundary.
"""

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..adoption_snapshot import write_rows
from ..contracts import Contract, Name
from ..runtime_process import atomic_json
from ..storage import Conflict, IntegrityError
from ..writer_claim import sync_directory
from .projection import META_FIELDS, bind_plan, plan_digest, project, sha, verify_rows
from .source import SourceIdentity, SourceLimits, iter_file_rows, load_json, read_source


def target_namespace(batch, operation):
    return "idx_" + sha({"batch": batch, "operation": operation})[:32]


class TargetIdentity(Contract):
    source: SourceIdentity
    config_revision_id: Name
    embedding_profile: Name
    embedding_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str = Field(min_length=1)
    dimensions: int = Field(gt=0)
    vector_workspace: Name
    storage_mode: Literal["local", "neo4j_milvus"]
    collections: dict[Name, str]
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def owned(self):
        if (
            self.vector_workspace
            != target_namespace(self.source.data_batch_id, self.source.operation_id)
            or self.vector_workspace == self.source.workspace
        ):
            raise ValueError(
                "Rebuild requires its fresh operation-owned vector workspace"
            )
        if set(self.collections) not in (
            {"entities", "relationships", "chunks"},
            {"entities", "relationships", "chunks", "name_entities"},
        ):
            raise ValueError("Rebuild must bind every supported target collection")
        for role, value in self.collections.items():
            prefix = self.vector_workspace + "_" + role
            if value != prefix and not value.startswith(prefix + "_"):
                raise ValueError(
                    "Rebuild collection escaped its operation-owned workspace"
                )
        return self


def check_binding(role, storage, identity):
    backend = type(storage).__name__
    if (
        backend
        != {"local": "NanoVectorDBStorage", "neo4j_milvus": "MilvusVectorDBStorage"}[
            identity.storage_mode
        ]
    ):
        raise ValueError("Unsupported managed vector rebuild backend")
    if (
        storage.workspace != identity.vector_workspace
        or storage.namespace != role
        or storage.embedding_func.model_name != identity.embedding_model
        or storage.embedding_func.embedding_dim != identity.dimensions
        or not META_FIELDS[role] <= storage.meta_fields
        or storage.final_namespace != identity.collections.get(role)
    ):
        raise IntegrityError("Rebuild target storage binding differs from its identity")
    if backend == "NanoVectorDBStorage":
        expected = (
            Path(storage.global_config["working_dir"])
            / identity.vector_workspace
            / ("vdb_" + role + ".json")
        )
        if (
            Path(storage._client_file_name) != expected
            or expected != expected.resolve()
        ):
            raise IntegrityError("Rebuild vector file escaped its fresh workspace")
    elif not storage.final_namespace.startswith(
        identity.vector_workspace + "_" + role + "_"
    ):
        raise IntegrityError("Rebuild vector collection escaped its fresh workspace")


async def require_fresh(stores, identity, owned):
    """Check every target before the first upsert or embedding request."""
    for role, storage in stores.items():
        check_binding(role, storage, identity)
    for role, storage in stores.items():
        if type(storage).__name__ == "NanoVectorDBStorage":
            if storage._client is not None or any(
                Path(storage._client_file_name).parent.iterdir()
            ):
                raise Conflict("Rebuild target directory already contains an attempt")
            owned[role] = storage
        else:
            if storage._client is not None or storage._initialized:
                raise Conflict(
                    "Rebuild requires uninitialized, unclaimed target stores"
                )
            from lightrag.kg.milvus_impl import run_in_milvus_executor_uninterruptible

            owned[role] = storage
            storage._client = await run_in_milvus_executor_uninterruptible(
                storage._create_milvus_client
            )
            present = await run_in_milvus_executor_uninterruptible(
                storage._client.has_collection,
                kwargs={"collection_name": storage.final_namespace},
            )
            if present:
                raise Conflict("Rebuild target collection already exists")


async def snapshot_target(storage, path, limits):
    if type(storage).__name__ == "NanoVectorDBStorage":
        # Verify the saved file through the normal loader. Serialization and
        # load-time normalization can differ from the writer's live matrix.
        if not Path(storage._client_file_name).is_file():
            raise IntegrityError("Rebuild target has no durable vector file")
        reader = type(storage)(
            namespace=storage.namespace,
            workspace=storage.workspace,
            global_config=storage.global_config,
            embedding_func=storage.embedding_func,
            meta_fields=storage.meta_fields,
        )
        await reader.initialize()
        # Copy on the event loop; never retain a live client view across awaits.
        live = deepcopy(await reader.client_storage)
        result = write_rows(
            (
                {**row, "vector": live["matrix"][index]}
                for index, row in enumerate(live["data"])
            ),
            path,
            **limits,
        )
    else:
        from lightrag.kg.milvus_impl import (
            get_collection_gate,
            run_in_milvus_executor_uninterruptible,
        )
        from ..adoption_snapshot import milvus_snapshot

        gate = get_collection_gate(storage.final_namespace)
        await gate.acquire_read()
        try:
            result = await run_in_milvus_executor_uninterruptible(
                milvus_snapshot, args=(storage, path), kwargs={"limits": limits}
            )
        finally:
            gate.release_read()
    # The adoption digest hashes rows independently; source.file_rows checks the
    # bytes and count of this exact artifact instead.
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "rows": result["rows"],
        "bytes": path.stat().st_size,
        "sha256": checksum,
    }


async def discard_pending(stores):
    errors = []
    for storage in stores.values():
        try:
            initialized = (
                storage._client is not None
                if type(storage).__name__ == "NanoVectorDBStorage"
                else storage._initialized
            )
            if initialized:
                await storage.drop_pending_index_ops()
        except Exception as exc:
            errors.append(type(exc).__name__)
        if (
            type(storage).__name__ == "MilvusVectorDBStorage"
            and storage._client is not None
        ):
            try:
                storage._client.close()
                storage._client = None
            except Exception as exc:
                errors.append(type(exc).__name__)
    if errors:
        raise RuntimeError(
            "Rebuild cleanup could not be confirmed: " + ", ".join(errors)
        )


async def build_target(
    source,
    directory,
    identity: TargetIdentity,
    stores,
    *,
    transform=lambda role, key, content: content,
    batch_size=256,
    limits=None,
    cancelled=lambda: False,
    on_progress=lambda value: None,
):
    """Return a verified private build; this never updates a registry/runtime binding.

    The caller must initialize shared-storage support and install provider
    admission in this owned process before calling. A new request/namespace is
    required after any failure; there is no paid request or target replay here.
    """
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("Rebuild batch size must be positive")
    limits = limits or SourceLimits()
    source, directory = Path(source), Path(directory)
    if directory != directory.resolve():
        raise IntegrityError("Rebuild output directory was redirected")
    manifest, canonical = read_source(source, identity.source, limits=limits)

    def unchanged():
        if (
            hashlib.sha256((source / "source.json").read_bytes()).hexdigest()
            != identity.source_manifest_sha256
        ):
            raise IntegrityError("Rebuild source marker changed")
        read_source(source, identity.source, limits=limits)

    unchanged()
    plan = bind_plan(
        project(**canonical, names=manifest["name_index"]),
        transform=transform,
        names=manifest["name_index"],
    )
    if set(stores) != set(plan) or set(identity.collections) != set(plan):
        raise IntegrityError("Rebuild did not bind every expected vector collection")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    atomic_json(directory / "attempt.json", identity.model_dump())
    counts = {
        "total": sum(len(rows) for rows in plan.values()),
        "written": 0,
        "verified": 0,
    }

    def progress(state, **values):
        value = {"state": state, **counts, **values}
        atomic_json(directory / "progress.json", value)
        on_progress(value)

    def check_cancelled():
        if cancelled():
            raise InterruptedError("Index rebuild cancellation requested")

    owned = {}
    try:
        check_cancelled()
        await require_fresh(stores, identity, owned)
        for storage in stores.values():
            await storage.initialize()
        progress("building")
        for role, rows in plan.items():
            entries = list(rows.items())
            if not entries:
                # Seal the empty model/dimension too, without a synthetic row or
                # model request. Nano's callback publishes an empty snapshot.
                await stores[role].index_done_callback()
            for offset in range(0, len(entries), batch_size):
                check_cancelled()
                portion = dict(entries[offset : offset + batch_size])
                await stores[role].upsert(portion)
                await stores[role].index_done_callback()
                counts["written"] += len(portion)
                progress("building", collection=role)
        proofs, artifacts = {}, {}
        for role, expected in plan.items():
            check_cancelled()
            path = directory / (role + ".jsonl")
            artifact = await snapshot_target(
                stores[role],
                path,
                {
                    "max_bytes": limits.max_evidence_bytes
                    - sum(info["bytes"] for info in artifacts.values()),
                    "max_rows": limits.max_rows,
                    "max_row_bytes": limits.max_row_bytes,
                },
            )
            rows = iter_file_rows(path, artifact, limits)
            proofs[role] = verify_rows(role, expected, rows, identity.dimensions)
            artifacts[path.name] = artifact
            counts["verified"] += len(expected)
            progress("verifying", collection=role)
        unchanged()
        check_cancelled()
        for storage in stores.values():
            await storage.finalize()
        result = {
            "schema_version": 1,
            "identity": identity.model_dump(),
            "source_graph": manifest["graph"],
            "plan": plan_digest(plan),
            "collections": {
                role: store.final_namespace for role, store in stores.items()
            },
            "proofs": proofs,
            "files": artifacts,
            "counts": counts,
        }
        progress("verified")
        atomic_json(directory / "verified.json", result)
        sync_directory(directory)
        sync_directory(directory.parent)
        return result
    except BaseException:
        # finalize() can replay buffered paid work. Explicitly discard the pending
        # requests and close channels without another index_done_callback instead.
        await discard_pending(owned)
        raise


def read_verified(
    directory,
    source,
    identity: TargetIdentity,
    *,
    transform=lambda role, key, content: content,
    limits=None,
):
    """Validate the sealed build receipt/artifacts, without trusting a status row.

    This validates archived build evidence. Runtime activation must additionally
    re-read the actual bound stores and prove source/old-writer ownership.
    """
    limits = limits or SourceLimits()
    directory, source = Path(directory), Path(source)
    if directory != directory.resolve():
        raise IntegrityError("Rebuild receipt directory was redirected")
    marker = directory / "verified.json"
    if marker.is_symlink() or marker.stat().st_size > 65536:
        raise IntegrityError("Invalid rebuild completion marker")
    result = load_json(marker.read_bytes())
    if (
        not isinstance(result, dict)
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
        or result.get("identity") != identity.model_dump()
    ):
        raise IntegrityError("Rebuild receipt belongs to another request")
    if source != source.resolve() or (source / "source.json").is_symlink():
        raise IntegrityError("Rebuild source marker was redirected")
    if (
        hashlib.sha256((source / "source.json").read_bytes()).hexdigest()
        != identity.source_manifest_sha256
    ):
        raise IntegrityError("Rebuild source marker changed")
    manifest, canonical = read_source(source, identity.source, limits=limits)
    plan = bind_plan(
        project(**canonical, names=manifest["name_index"]),
        transform=transform,
        names=manifest["name_index"],
    )
    if (
        result.get("plan") != plan_digest(plan)
        or result.get("collections") != identity.collections
        or result.get("source_graph") != manifest["graph"]
    ):
        raise IntegrityError("Rebuild plan, source or target binding changed")
    files = result.get("files")
    if not isinstance(files, dict) or set(files) != {role + ".jsonl" for role in plan}:
        raise IntegrityError("Rebuild receipt has an incomplete artifact catalog")
    for info in files.values():
        if not isinstance(info, dict) or any(
            type(info.get(key)) is not int or info[key] < 0 for key in ("bytes", "rows")
        ):
            raise IntegrityError("Rebuild artifact metadata is invalid")
    if sum(info["bytes"] for info in files.values()) > limits.max_evidence_bytes:
        raise ValueError("Rebuild artifacts exceed aggregate evidence limits")
    proofs = {
        role: verify_rows(
            role,
            expected,
            iter_file_rows(
                directory / (role + ".jsonl"), files[role + ".jsonl"], limits
            ),
            identity.dimensions,
        )
        for role, expected in plan.items()
    }
    count = sum(len(rows) for rows in plan.values())
    if not isinstance(result.get("counts"), dict) or any(
        type(value) is not int for value in result["counts"].values()
    ):
        raise IntegrityError("Rebuild completion counts are invalid")
    if result.get("proofs") != proofs or result.get("counts") != {
        "total": count,
        "written": count,
        "verified": count,
    }:
        raise IntegrityError("Rebuild row verification or completion counts changed")
    return result
