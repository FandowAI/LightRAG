"""Produce live candidate/restoration attestations before full RAG readiness.

Archived receipts alone never authorize a switch. Re-read canonical source and
actual persisted target vectors while admission is still fenced.
See docs/design/BatchIndexRebuild.md.
"""

import hashlib
from pathlib import Path

from ..adoption_snapshot import vector_stores
from ..storage import Conflict, IntegrityError
from .audit import read_audit, verify_state
from .builder import TargetIdentity, check_binding, read_verified, snapshot_target
from .projection import bind_plan, project, verify_rows
from .source import SourceIdentity, SourceLimits, iter_file_rows, read_source
from .store import RebuildStore
from .worker import operation_directory


def content_transform(rag):
    from lightrag.operate import _truncate_vdb_content

    config = rag.entities_vdb.global_config

    def transform(role, key, content):
        return (
            _truncate_vdb_content(content, config, role + ":" + key)
            if role in {"entities", "relationships"}
            else content
        )

    return transform


def compiled_transform(startup):
    """Reproduce the managed server's fixed tokenizer without constructing a RAG."""
    from lightrag.utils import TiktokenTokenizer

    from lightrag.operate import _truncate_vdb_content

    config = {
        "tokenizer": TiktokenTokenizer("gpt-4o-mini"),
        "embedding_token_limit": int(startup["environment"]["EMBEDDING_TOKEN_LIMIT"]),
    }
    return lambda role, key, content: (
        _truncate_vdb_content(content, config, role + ":" + key)
        if role in {"entities", "relationships"}
        else content
    )


def context(registry, paths, batch, operation):
    store = RebuildStore(registry)
    record = store.get(batch, operation)
    with registry.transaction() as db:
        store.fenced(db, registry, record)
    root = operation_directory(paths, registry, batch, operation)
    source = SourceIdentity.model_validate(record["source"])
    evidence = record.get("source_evidence")
    if not evidence:
        raise IntegrityError("Index generation has no completed source capture")
    audit = read_audit(root / "source" / "audit.json", source, evidence["audit_sha256"])
    marker = root / "source" / "source.json"
    if (
        marker != marker.resolve()
        or hashlib.sha256(marker.read_bytes()).hexdigest()
        != evidence["manifest_sha256"]
    ):
        raise IntegrityError("Index source capture marker changed")
    return record, root, audit


async def verify_candidate(rag, spec, index, registry, paths):
    binding = index["binding"]
    batch, operation = binding["data_batch_id"], binding["operation_id"]
    record, root, audit = context(registry, paths, batch, operation)
    if (
        record["state"] not in {"candidate_starting", "candidate_verifying"}
        or record["cancel_requested"]
        or index["state"] != "verified"
        or spec.get("application_id") != operation
        or spec["config_revision_id"] != record["config_revision_id"]
    ):
        raise Conflict("This generation does not own initial index verification")
    limits = SourceLimits()
    receipt = index["evidence"]["receipt"]
    target = TargetIdentity.model_validate(receipt["identity"])
    transform = content_transform(rag)
    manifest = root / "build" / "verified.json"
    if (
        read_verified(
            root / "build", root / "source", target, transform=transform, limits=limits
        )
        != receipt
        or hashlib.sha256(manifest.read_bytes()).hexdigest()
        != index["evidence"]["receipt_sha256"]
    ):
        raise IntegrityError("Live index candidate lost its verified build receipt")
    source_manifest, rows = read_source(root / "source", target.source, limits=limits)
    if source_manifest["graph"] != audit.graph.model_dump():
        raise IntegrityError(
            "Canonical source graph audit disagrees with its captured rows"
        )
    await verify_state(rag, audit, limits=limits)
    plan = bind_plan(
        project(**rows, names=source_manifest["name_index"]),
        transform=transform,
        names=source_manifest["name_index"],
    )
    stores = vector_stores(rag)
    if set(stores) != set(plan) or set(stores) != set(binding["collections"]):
        raise IntegrityError("Live index candidate omitted a vector collection")
    directory = Path(spec["attempt_directory"]) / "index-verification"
    if directory != directory.resolve():
        raise IntegrityError("Live verification directory was redirected")
    directory.mkdir(exist_ok=False, mode=0o700)
    total = 0
    for role, storage in stores.items():
        check_binding(role, storage, target)
        path = directory / (role + ".jsonl")
        artifact = await snapshot_target(
            storage,
            path,
            {
                "max_bytes": limits.max_evidence_bytes - total,
                "max_rows": limits.max_rows,
                "max_row_bytes": limits.max_row_bytes,
            },
        )
        total += artifact["bytes"]
        proof = verify_rows(
            role, plan[role], iter_file_rows(path, artifact, limits), target.dimensions
        )
        if proof != receipt["proofs"][role]:
            raise IntegrityError(
                "Live target vectors changed after their verified build"
            )
    await verify_state(rag, audit, limits=limits)
    _, _, final = context(registry, paths, batch, operation)
    if final != audit:
        raise IntegrityError("Source audit changed during candidate verification")
    return {
        "binding_digest": index["digest"],
        "receipt_sha256": index["evidence"]["receipt_sha256"],
        "source_audit_sha256": record["source_evidence"]["audit_sha256"],
    }


async def verify_restoration(rag, spec, registry, paths):
    operation = spec.get("rebuild_rollback_id")
    if operation is None:
        return None
    record = RebuildStore(registry).get(spec["data_batch_id"], operation)
    if (
        record["state"] not in {"rollback_starting", "rollback_verifying"}
        or record["previous_revision"] != spec["config_revision_id"]
    ):
        raise Conflict("This generation does not own source restoration verification")
    if not record.get("source_evidence"):
        return None
    record, _, audit = context(registry, paths, spec["data_batch_id"], operation)
    await verify_state(rag, audit)
    return record["source_evidence"]["audit_sha256"]
