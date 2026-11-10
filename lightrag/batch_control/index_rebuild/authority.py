"""Immutable vector bindings are revision-owned; a head never overrides old revisions.

Declared bindings are usable only by an explicitly authorized build. A normal
RAG candidate requires a verified binding; activated and retained prior bindings
keep their exact model and collection identities. See docs/design/BatchIndexRebuild.md.
"""

from copy import deepcopy
import json
from typing import Literal

from pydantic import Field, model_validator

from ..contracts import Contract, Name
from ..resources import digest
from ..storage import Conflict, IntegrityError
from .builder import TargetIdentity, target_namespace


class VectorBinding(Contract):
    schema_version: Literal[1] = 1
    data_batch_id: Name
    operation_id: Name
    config_revision_id: Name
    source_workspace: Name
    vector_workspace: Name
    storage_mode: Literal["local", "neo4j_milvus"]
    embedding_profile: Name
    embedding_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str = Field(min_length=1)
    dimensions: int = Field(gt=0)
    collections: dict[Name, str]

    @model_validator(mode="after")
    def scoped(self):
        if (
            self.vector_workspace
            != target_namespace(self.data_batch_id, self.operation_id)
            or self.source_workspace == self.vector_workspace
        ):
            raise ValueError("Vector binding must own a separate operation namespace")
        if set(self.collections) not in (
            {"entities", "relationships", "chunks"},
            {"entities", "relationships", "chunks", "name_entities"},
        ):
            raise ValueError("Vector binding must declare every supported index role")
        for role, name in self.collections.items():
            prefix = self.vector_workspace + "_" + role
            if name != prefix and not name.startswith(prefix + "_"):
                raise ValueError("Vector binding escaped its operation namespace")
        return self

    def target(self, source, source_manifest_sha256):
        if (
            source.data_batch_id != self.data_batch_id
            or source.operation_id != self.operation_id
            or source.workspace != self.source_workspace
        ):
            raise IntegrityError(
                "Captured source belongs to a different vector binding"
            )
        return TargetIdentity.model_validate(
            {
                "source": source.model_dump(),
                "source_manifest_sha256": source_manifest_sha256,
                **{
                    key: value
                    for key, value in self.model_dump().items()
                    if key
                    not in {
                        "schema_version",
                        "data_batch_id",
                        "operation_id",
                        "source_workspace",
                    }
                },
            }
        )


def table_exists(db, table):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def is_rebuild(db, batch, operation):
    return (
        table_exists(db, "index_rebuilds")
        and db.execute(
            "SELECT 1 FROM index_rebuilds WHERE batch=? AND id=?", (batch, operation)
        ).fetchone()
        is not None
    )


def read_binding(db, batch, identity):
    if not table_exists(db, "index_vector_bindings"):
        raise KeyError(identity)
    row = db.execute(
        "SELECT * FROM index_vector_bindings WHERE batch=? AND id=?", (batch, identity)
    ).fetchone()
    if row is None:
        raise KeyError(identity)
    value = json.loads(row["record"])
    if digest(value) != row["digest"]:
        raise IntegrityError("Immutable vector binding changed")
    binding = VectorBinding.model_validate(value)
    if (
        binding.data_batch_id != batch
        or binding.operation_id != identity
        or binding.config_revision_id != row["revision"]
    ):
        raise IntegrityError("Vector binding ownership changed")
    if row["state"] not in {"declared", "verified", "active", "retired", "abandoned"}:
        raise IntegrityError("Unknown vector binding state")
    evidence = None
    if row["evidence"]:
        sealed = json.loads(row["evidence"])
        if (
            set(sealed) != {"digest", "value"}
            or digest(sealed["value"]) != sealed["digest"]
        ):
            raise IntegrityError("Verified vector evidence changed")
        evidence = sealed["value"]
    if row["state"] in {"verified", "active", "retired"} and evidence is None:
        raise IntegrityError("Verified vector binding lost its build evidence")
    return {
        "binding": binding.model_dump(),
        "digest": row["digest"],
        "state": row["state"],
        "evidence": evidence,
    }


def head(db, batch):
    if not table_exists(db, "index_binding_heads"):
        return None
    row = db.execute(
        "SELECT binding FROM index_binding_heads WHERE batch=?", (batch,)
    ).fetchone()
    return row[0] if row else None


def for_manifest(registry, owner, revision, manifest, *, purpose="rag"):
    """Resolve the immutable revision reference; never silently use a current head."""
    if purpose not in {"rag", "index_build", "index_preflight"}:
        raise ValueError("Unsupported vector binding purpose")
    reference = manifest["sources"].get("index_binding_id")
    checksum = manifest["sources"].get("index_binding_digest")
    with registry.transaction() as db:
        current_head = head(db, owner["id"])
        if reference is None:
            if (
                checksum is not None
                or owner["active"] == revision
                and current_head is not None
            ):
                raise IntegrityError(
                    "Active revision lost its vector binding reference"
                )
            return None
        record = read_binding(db, owner["id"], reference)
        binding = VectorBinding.model_validate(record["binding"])
        if (
            binding.source_workspace != owner["workspace"]
            or checksum != record["digest"]
        ):
            raise IntegrityError("Revision vector binding identity changed")
        if owner["active"] == revision and (
            current_head != reference or record["state"] != "active"
        ):
            raise IntegrityError("Active revision and vector binding head disagree")
        if record["state"] in {"active", "retired"}:
            return record
        if record["state"] == "abandoned":
            raise Conflict("Abandoned vector binding cannot start a runtime")
        row = (
            db.execute(
                "SELECT record FROM index_rebuilds WHERE batch=? AND id=?",
                (owner["id"], reference),
            ).fetchone()
            if table_exists(db, "index_rebuilds")
            else None
        )
        operation = json.loads(row[0]) if row else {}
        if (
            owner["pending"] != reference
            or owner["desired"] != revision
            or operation.get("config_revision_id") != revision
        ):
            raise Conflict("Vector candidate does not own this pending revision")
        allowed = (
            {"builder_starting", "building", "stopping_builder"}
            if purpose == "index_build"
            else {"candidate_starting", "candidate_verifying"}
        )
        if purpose == "index_preflight":
            allowed = {
                "draining",
                "source_capturing",
                "source_captured",
                "stopping_source",
                "builder_starting",
                "building",
                "stopping_builder",
                "candidate_starting",
                "candidate_verifying",
            }
        if (
            operation.get("state") not in allowed
            or purpose not in {"index_build", "index_preflight"}
            and record["state"] != "verified"
        ):
            raise Conflict(
                "Vector candidate has no verified build for this runtime purpose"
            )
        return record


def effective_storage_binding(original, index):
    """Preserve the adopted base authority and replace only declared vector fields."""
    if original is None or index is None:
        return original
    binding = VectorBinding.model_validate(index)
    if (
        original["workspace"] != binding.source_workspace
        or original["storage_mode"] != binding.storage_mode
    ):
        raise IntegrityError("Vector replacement does not own the adopted source")
    result = deepcopy(original)
    result["embedding"] = {
        "model": binding.embedding_model,
        "dimensions": binding.dimensions,
    }
    result["collections"] = {
        "graph": original["collections"]["graph"],
        **{
            role: binding.collections[role]
            for role in ("entities", "relationships", "chunks")
        },
    }
    result["auxiliary_collections"] = (
        {"name_entities": binding.collections["name_entities"]}
        if "name_entities" in binding.collections
        else {}
    )
    return result


def startup_binding(registry, paths, spec):
    """Resolve worker/supervisor startup against its frozen revision and registry."""
    from ..resources import VersionFiles

    owner = registry.get(spec["data_batch_id"])
    revision = registry.revision(owner["id"], spec["config_revision_id"])
    document, _, manifest = VersionFiles(paths).read(owner, revision)
    startup = spec["startup"]
    purpose = startup.get("purpose", "rag")
    selected = for_manifest(registry, owner, revision["id"], manifest, purpose=purpose)
    if selected is None:
        if (
            startup.get("index_binding") is not None
            or startup.get("index_binding_digest") is not None
        ):
            raise IntegrityError("Startup invented an unregistered vector binding")
        return None
    value = selected["binding"]
    if (
        startup.get("index_binding") != value
        or startup.get("index_binding_digest") != selected["digest"]
        or document.rag.embedding_profile != value["embedding_profile"]
        or digest(manifest["execution"]["rag_profiles"][value["embedding_profile"]])
        != value["embedding_profile_sha256"]
        or startup["storage_mode"] != value["storage_mode"]
    ):
        raise IntegrityError(
            "Startup vector binding disagrees with its immutable revision"
        )
    if selected["evidence"] is not None:
        target = TargetIdentity.model_validate(
            selected["evidence"]["receipt"]["identity"]
        )
        if (
            VectorBinding.model_validate(value).target(
                target.source, target.source_manifest_sha256
            )
            != target
        ):
            raise IntegrityError(
                "Verified build identity disagrees with its registered binding"
            )
    return selected


def runtime_index_proof(registry, paths, spec, proof):
    """Verify the exact registered vector identity before widening workspace checks."""
    selected = startup_binding(registry, paths, spec)
    if selected is None:
        if (
            proof.get("index_binding_id") is not None
            or proof.get("index_binding_digest") is not None
        ):
            raise IntegrityError("Runtime invented an unregistered vector binding")
        return None
    binding = selected["binding"]
    expected_auxiliary = (
        {"name_entities": binding["collections"]["name_entities"]}
        if "name_entities" in binding["collections"]
        else {}
    )
    if (
        proof.get("index_binding_id") != binding["operation_id"]
        or proof.get("index_binding_digest") != selected["digest"]
        or any(
            proof.get("collections", {}).get(role) != binding["collections"][role]
            for role in ("entities", "relationships", "chunks")
        )
        or proof.get("auxiliary_collections", {}) != expected_auxiliary
    ):
        raise IntegrityError(
            "Runtime vector collections differ from their immutable binding"
        )
    if selected["state"] == "verified":
        evidence = selected["evidence"]
        expected = {
            "binding_digest": selected["digest"],
            "receipt_sha256": evidence["receipt_sha256"],
            "source_audit_sha256": evidence["source_audit_sha256"],
        }
        if proof.get("index_initial_verified") != expected:
            raise IntegrityError(
                "Index candidate has no live initial verification attestation"
            )
    return selected
