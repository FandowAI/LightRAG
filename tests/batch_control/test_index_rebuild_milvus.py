"""Exercise actual Milvus storage batching/strong iteration against private transports."""

# ruff: noqa: F811

from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from lightrag.batch_control.index_rebuild.builder import (
    TargetIdentity,
    build_target,
    read_verified,
    target_namespace,
)
from lightrag.batch_control.index_rebuild.projection import META_FIELDS
from lightrag.batch_control.index_rebuild.source import write_source
from lightrag.batch_control.storage import Conflict, IntegrityError
from lightrag.kg.milvus_impl import MilvusVectorDBStorage
from lightrag.utils import EmbeddingFunc
from .test_index_rebuild_builder import Embeddings, shared_data  # noqa: F401
from .test_index_rebuild_projection import canonical, identity  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "outcome", ["complete", "truncated", "wrong_metadata", "existing_empty"]
)
async def test_milvus_build_requires_fresh_targets_complete_strong_reads_and_all_name_rows(
    tmp_path, canonical, identity, monkeypatch, outcome
):
    source = tmp_path / "source"
    write_source(source, identity, **canonical, names=True)
    namespace = target_namespace(identity.data_batch_id, identity.operation_id)
    embed = Embeddings()
    function = EmbeddingFunc(
        embedding_dim=8, max_token_size=512, func=embed, model_name="offline-milvus"
    )
    stores = {
        role: MilvusVectorDBStorage(
            namespace=role,
            workspace=namespace,
            global_config={
                "embedding_batch_num": 32,
                "vector_db_storage_cls_kwargs": {
                    "cosine_better_than_threshold": 0.2,
                    "index_type": "HNSW",
                },
            },
            embedding_func=function,
            meta_fields=set(fields),
        )
        for role, fields in META_FIELDS.items()
    }
    target = TargetIdentity(
        source=identity,
        config_revision_id="rev-new",
        embedding_profile="embed-new",
        embedding_profile_sha256="b" * 64,
        embedding_model="offline-milvus",
        dimensions=8,
        vector_workspace=namespace,
        storage_mode="neo4j_milvus",
        collections={role: store.final_namespace for role, store in stores.items()},
        source_manifest_sha256=hashlib.sha256(
            (source / "source.json").read_bytes()
        ).hexdigest(),
    )
    database, clients, reads = {}, [], []

    class Client:
        def __init__(self):
            self.closed = False
            clients.append(self)

        def has_collection(self, collection_name):
            return collection_name in database

        def upsert(self, collection_name, data):
            for row in data:
                database[collection_name][row["id"]] = deepcopy(row)
            return {"upsert_count": len(data)}

        def load_collection(self, collection_name):
            assert collection_name in database

        def query_iterator(self, collection_name, **kwargs):
            reads.append(kwargs["consistency_level"])
            rows = deepcopy(list(database[collection_name].values()))
            if outcome == "truncated":
                rows = rows[:-1]
            elif outcome == "wrong_metadata" and rows:
                rows[0]["content"] = "foreign content with the same count"
            chunks = iter([rows, []])
            return SimpleNamespace(
                next=lambda: next(chunks), close=lambda: reads.append("iterator closed")
            )

        def query(self, collection_name, **kwargs):
            reads.append(kwargs["consistency_level"])
            return [{"count(*)": len(database[collection_name])}]

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        MilvusVectorDBStorage, "_create_milvus_client", lambda self: Client()
    )
    monkeypatch.setattr(
        MilvusVectorDBStorage,
        "_create_collection_if_not_exist",
        lambda self: database.setdefault(self.final_namespace, {}),
    )
    if outcome == "existing_empty":
        database[stores["entities"].final_namespace] = {}
    output = tmp_path / "build"
    if outcome == "complete":
        result = await build_target(source, output, target, stores, batch_size=1)
        assert result["counts"] == {"total": 5, "written": 5, "verified": 5}
        assert result["proofs"]["name_entities"]["rows"] == 1
        assert read_verified(output, source, target) == result
        assert all(key.startswith(namespace + "_") for key in database)
        assert reads.count("Strong") == 8
        assert reads.count("iterator closed") == 4
    else:
        with pytest.raises(Conflict if outcome == "existing_empty" else IntegrityError):
            await build_target(source, output, target, stores, batch_size=1)
        assert not (output / "verified.json").exists()
        if outcome == "existing_empty":
            assert embed.calls == 0 and len(database) == 1
    assert clients and all(client.closed for client in clients)
