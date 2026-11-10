"""Actual isolated Nano targets, paid-call failure boundaries and preserved sources."""

# ruff: noqa: F811

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from lightrag.batch_control.index_rebuild.builder import (
    TargetIdentity,
    build_target,
    read_verified,
    target_namespace,
)
from lightrag.batch_control.index_rebuild.projection import (
    META_FIELDS,
    bind_plan,
    project,
    verify_rows,
)
from lightrag.batch_control.index_rebuild.source import SourceLimits, write_source
from lightrag.batch_control.storage import Conflict, IntegrityError
from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage
from lightrag.kg.shared_storage import finalize_share_data, initialize_share_data
from lightrag.utils import EmbeddingFunc
from .test_index_rebuild_projection import canonical, identity  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def shared_data():
    finalize_share_data()
    initialize_share_data()
    yield
    finalize_share_data()


class Embeddings:
    def __init__(self, fail_at=None):
        self.calls = 0
        self.texts = []
        self.fail_at = fail_at

    async def __call__(self, texts, **kwargs):
        self.calls += 1
        self.texts.extend(texts)
        if self.calls == self.fail_at:
            raise RuntimeError("injected model failure")
        return np.array(
            [[float(i + 1 + len(text) % 7) for i in range(8)] for text in texts],
            dtype=np.float32,
        )


def setup_target(tmp_path, identity, canonical, *, fail_at=None):
    source = tmp_path / "source"
    write_source(source, identity, **canonical)
    target = TargetIdentity(
        source=identity,
        config_revision_id="rev-new",
        embedding_profile="embed-new",
        embedding_profile_sha256="b" * 64,
        embedding_model="new-offline-model",
        dimensions=8,
        vector_workspace=target_namespace(
            identity.data_batch_id, identity.operation_id
        ),
        storage_mode="local",
        collections={
            role: target_namespace(identity.data_batch_id, identity.operation_id)
            + "_"
            + role
            for role in ("entities", "relationships", "chunks")
        },
        source_manifest_sha256=hashlib.sha256(
            (source / "source.json").read_bytes()
        ).hexdigest(),
    )
    embed = Embeddings(fail_at)
    function = EmbeddingFunc(
        embedding_dim=8,
        max_token_size=512,
        func=embed,
        model_name=target.embedding_model,
    )
    stores = {
        role: NanoVectorDBStorage(
            namespace=role,
            workspace=target.vector_workspace,
            global_config={
                "working_dir": str(tmp_path / "vectors"),
                "embedding_batch_num": 32,
                "vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.2},
            },
            embedding_func=function,
            meta_fields=fields,
        )
        for role, fields in META_FIELDS.items()
        if role != "name_entities"
    }
    return source, target, embed, stores


async def test_real_shadow_build_is_complete_reopenable_and_preserves_old_vectors(
    tmp_path, identity, canonical
):
    source, target, embed, stores = setup_target(tmp_path, identity, canonical)
    old = tmp_path / "vectors" / identity.workspace / "vdb_entities.json"
    old.parent.mkdir()
    old.write_text('{"retained":"original vector index"}')
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    progress = []
    output = tmp_path / "build"
    result = await build_target(
        source, output, target, stores, batch_size=1, on_progress=progress.append
    )
    assert result["counts"] == {"total": 4, "written": 4, "verified": 4}
    assert len(embed.texts) == 4
    assert progress[-1]["state"] == "verified"
    assert json.loads((output / "verified.json").read_text()) == result
    assert read_verified(output, source, target) == result
    assert {path.name: path.read_bytes() for path in source.iterdir()} == original
    assert old.read_text() == '{"retained":"original vector index"}'
    expected = bind_plan(project(**canonical))
    for role, previous in stores.items():
        reopened = NanoVectorDBStorage(
            namespace=role,
            workspace=target.vector_workspace,
            global_config=previous.global_config,
            embedding_func=previous.embedding_func,
            meta_fields=previous.meta_fields,
        )
        await reopened.initialize()
        live = deepcopy(await reopened.client_storage)
        rows = [
            {**row, "vector": live["matrix"][index]}
            for index, row in enumerate(live["data"])
        ]
        assert verify_rows(role, expected[role], rows, 8) == result["proofs"][role]
        await reopened.finalize()
    assert embed.calls == 4


async def test_model_failure_discards_pending_requests_without_a_second_paid_attempt(
    tmp_path, identity, canonical
):
    source, target, embed, stores = setup_target(
        tmp_path, identity, canonical, fail_at=2
    )
    output = tmp_path / "build"
    with pytest.raises(RuntimeError, match="injected model failure"):
        await build_target(source, output, target, stores, batch_size=1)
    assert embed.calls == 2
    assert not (output / "verified.json").exists()
    assert (output / "attempt.json").exists()
    assert all(not store._pending_upserts for store in stores.values())
    with pytest.raises(FileExistsError):
        await build_target(source, output, target, stores, batch_size=1)
    assert embed.calls == 2


async def test_cancel_after_a_committed_batch_retains_partial_target_without_publishing(
    tmp_path, identity, canonical
):
    source, target, embed, stores = setup_target(tmp_path, identity, canonical)
    output = tmp_path / "build"
    with pytest.raises(InterruptedError):
        await build_target(
            source,
            output,
            target,
            stores,
            batch_size=1,
            cancelled=lambda: embed.calls >= 1,
        )
    assert embed.calls == 1
    assert not (output / "verified.json").exists()
    assert json.loads((output / "progress.json").read_text())["written"] == 1


async def test_foreign_storage_binding_is_rejected_before_any_model_or_store_mutation(
    tmp_path, identity, canonical
):
    source, target, embed, stores = setup_target(tmp_path, identity, canonical)
    stores["entities"].workspace = identity.workspace
    with pytest.raises(IntegrityError):
        await build_target(source, tmp_path / "build", target, stores)
    assert embed.calls == 0
    assert all(store._client is None for store in stores.values())


async def test_existing_target_is_not_cleared_flushed_or_adopted_by_a_new_attempt(
    tmp_path, identity, canonical
):
    source, target, embed, stores = setup_target(tmp_path, identity, canonical)
    existing = stores["entities"]
    await existing.initialize()
    await existing.upsert({"prior-attempt": {"content": "uncommitted prior work"}})
    with pytest.raises(Conflict):
        await build_target(source, tmp_path / "build", target, stores)
    assert embed.calls == 0
    assert "prior-attempt" in existing._pending_upserts
    await existing.drop_pending_index_ops()


async def test_target_snapshot_limits_fail_without_a_verified_subset(
    tmp_path, identity, canonical
):
    source, target, _, stores = setup_target(tmp_path, identity, canonical)
    manifest = json.loads((source / "source.json").read_text())
    budget = sum(info["bytes"] for info in manifest["files"].values())
    output = tmp_path / "build"
    with pytest.raises(ValueError, match="limit"):
        await build_target(
            source,
            output,
            target,
            stores,
            limits=SourceLimits(max_evidence_bytes=budget),
        )
    assert not (output / "verified.json").exists()
    assert sum(path.stat().st_size for path in output.glob("*.jsonl")) <= budget


async def test_source_drift_during_build_prevents_completion(
    tmp_path, identity, canonical
):
    source, target, _, stores = setup_target(tmp_path, identity, canonical)
    output = tmp_path / "build"

    def change_after_write(progress):
        if progress["written"]:
            (source / "chunks.jsonl").write_text("{}\n")

    with pytest.raises(IntegrityError):
        await build_target(
            source, output, target, stores, batch_size=1, on_progress=change_after_write
        )
    assert not (output / "verified.json").exists()


async def test_empty_source_publishes_empty_model_bound_indexes_without_calling_a_model(
    tmp_path, identity
):
    canonical = {"nodes": [], "edges": [], "chunks": []}
    source, target, embed, stores = setup_target(tmp_path, identity, canonical)
    output = tmp_path / "build"
    result = await build_target(source, output, target, stores)
    assert result["counts"] == {"total": 0, "written": 0, "verified": 0}
    assert embed.calls == 0
    assert all(Path(store._client_file_name).is_file() for store in stores.values())
    assert read_verified(output, source, target) == result


@pytest.mark.parametrize(
    "corruption", ["file", "binding", "counts", "proof", "catalog"]
)
async def test_verified_build_receipt_requires_exact_artifacts_and_bindings(
    tmp_path, identity, canonical, corruption
):
    source, target, _, stores = setup_target(tmp_path, identity, canonical)
    output = tmp_path / "build"
    await build_target(source, output, target, stores)
    marker = output / "verified.json"
    result = json.loads(marker.read_text())
    if corruption == "file":
        (output / "entities.jsonl").write_text("{}\n")
    else:
        if corruption == "binding":
            result["collections"]["entities"] = "foreign_entities"
        elif corruption == "counts":
            result["counts"]["verified"] -= 1
        elif corruption == "proof":
            result["proofs"]["entities"]["vectors_sha256"] = "f" * 64
        else:
            result["files"].pop("entities.jsonl")
        marker.write_text(json.dumps(result))
    with pytest.raises(IntegrityError):
        read_verified(output, source, target)
