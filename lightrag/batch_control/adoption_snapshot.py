"""Export existing rows without embedding or changing a storage collection."""

from copy import deepcopy
import hashlib
import json
import os
import uuid
from pathlib import Path

from .bindings import GraphBaseline, VectorBaseline, rows_digest
from .runtime_process import atomic_json
from .storage import Conflict, IntegrityError
from .writer_claim import sync_directory


def json_value(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError("Unsupported vector snapshot value: " + type(value).__name__)


def vector_stores(rag):
    stores = {
        name: getattr(rag, attribute)
        for name, attribute in (
            ("entities", "entities_vdb"),
            ("relationships", "relationships_vdb"),
            ("chunks", "chunks_vdb"),
        )
    }
    merge = getattr(rag, "entity_merge", None)
    if merge is not None:
        stores["name_entities"] = merge.name_vdb
    return stores


def write_rows(
    rows, destination=None, *, max_bytes=None, max_rows=None, max_row_bytes=None
):
    for limit in (max_bytes, max_rows, max_row_bytes):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("Vector snapshot limits must be nonnegative integers")
    hashes, ids = [], set()
    total = 0
    handle = None
    try:
        if destination is not None:
            handle = Path(destination).open("xb")
            os.chmod(destination, 0o600)
        for row in rows:
            identity = row.get("id", row.get("__id__"))
            if not isinstance(identity, str) or identity in ids:
                raise IntegrityError(
                    "Vector snapshot contains duplicate or missing primary IDs"
                )
            ids.add(identity)
            raw = json.dumps(
                row,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                default=json_value,
            ).encode()
            total += len(raw) + 1
            if (
                max_bytes is not None
                and total > max_bytes
                or max_rows is not None
                and len(ids) > max_rows
                or max_row_bytes is not None
                and len(raw) + 1 > max_row_bytes
            ):
                raise ValueError(
                    "Vector snapshot exceeds its configured evidence limit"
                )
            hashes.append(hashlib.sha256(raw).hexdigest())
            if handle is not None:
                handle.write(raw + b"\n")
        if handle is not None:
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if handle is not None:
            handle.close()
    return {
        "rows": len(ids),
        "sha256": hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest(),
    }


def milvus_snapshot(storage, destination, *, limits=None):
    client = storage._client
    iterator = client.query_iterator(
        collection_name=storage.final_namespace,
        batch_size=256,
        output_fields=["*", "vector"],
        consistency_level="Strong",
    )
    try:

        def rows():
            while batch := iterator.next():
                yield from batch

        result = write_rows(rows(), destination, **(limits or {}))
    finally:
        iterator.close()
    actual = client.query(
        collection_name=storage.final_namespace,
        filter="",
        output_fields=["count(*)"],
        consistency_level="Strong",
    )
    if len(actual) != 1 or actual[0].get("count(*)") != result["rows"]:
        raise IntegrityError("Vector snapshot did not read the complete collection")
    return result


async def vector_baselines(rag, directory=None):
    results = {}
    for name, storage in vector_stores(rag).items():
        for attribute in (
            "_pending_vector_docs",
            "_pending_vector_deletes",
            "_pending_upserts",
            "_pending_deletes",
        ):
            if getattr(storage, attribute, None):
                raise Conflict("Finish pending vector writes before workspace adoption")
        destination = Path(directory) / (name + ".jsonl") if directory else None
        if type(storage).__name__ == "MilvusVectorDBStorage":
            from lightrag.kg.milvus_impl import (
                get_collection_gate,
                run_in_milvus_executor_uninterruptible,
            )

            gate = get_collection_gate(storage.final_namespace)
            await gate.acquire_read()
            try:
                value = await run_in_milvus_executor_uninterruptible(
                    milvus_snapshot, args=(storage, destination)
                )
            finally:
                gate.release_read()
        elif type(storage).__name__ == "NanoVectorDBStorage":
            # Copy immediately: client_storage must not be retained across awaits.
            live = deepcopy(await storage.client_storage)
            value = write_rows(
                (
                    {**row, "vector": live["matrix"][index]}
                    for index, row in enumerate(live["data"])
                ),
                destination,
            )
        else:
            raise ValueError("Unsupported vector backend for workspace adoption")
        results[name] = value
    return results


async def snapshot(rag, directory, observation):
    directory = Path(directory)
    if directory != directory.resolve():
        raise IntegrityError("Snapshot directory was redirected")
    if directory.exists():
        if (directory / "manifest.json").exists():
            return verify_snapshot(directory, observation)
        directory.rename(
            directory.with_name(directory.name + ".partial-" + uuid.uuid4().hex)
        )
    directory.mkdir(parents=True, mode=0o700)
    graph = rag.chunk_entity_relation_graph
    nodes, edges = await graph.get_all_nodes(), await graph.get_all_edges()
    baseline = GraphBaseline(
        nodes=len(nodes),
        edges=len(edges),
        nodes_sha256=rows_digest(nodes),
        edges_sha256=rows_digest(edges),
    ).model_dump()
    if baseline != observation["graph"]:
        raise IntegrityError("Frozen graph changed before snapshot publication")
    for filename, rows in (("nodes.jsonl", nodes), ("edges.jsonl", edges)):
        with (directory / filename).open("xb") as stream:
            os.chmod(directory / filename, 0o600)
            for row in rows:
                stream.write(
                    (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode()
                )
            stream.flush()
            os.fsync(stream.fileno())
    vectors = await vector_baselines(rag, directory)
    artifacts = {
        path.name: {"bytes": path.stat().st_size, "sha256": file_sha256(path)}
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
    value = {
        "schema_version": 1,
        "workspace": rag.workspace,
        "boot_id": observation["boot_id"],
        "freeze_owner": observation["freeze_owner"],
        "graph": baseline,
        "vectors": vectors,
        "artifacts": artifacts,
    }
    atomic_json(directory / "manifest.json", value)
    sync_directory(directory)
    return value


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_snapshot(directory, observation):
    from .writer_claim import read_json

    directory = Path(directory)
    if directory != directory.resolve():
        raise IntegrityError("Snapshot directory was redirected")
    value = read_json(directory / "manifest.json")
    if any(
        value.get(key) != observation.get(key)
        for key in ("workspace", "boot_id", "freeze_owner", "graph")
    ):
        raise IntegrityError("Snapshot belongs to a different frozen source")
    names = set(value["vectors"])
    if not {"entities", "relationships", "chunks"} <= names or names - {
        "entities",
        "relationships",
        "chunks",
        "name_entities",
    }:
        raise IntegrityError(
            "Snapshot does not declare every supported vector collection"
        )
    for baseline in value["vectors"].values():
        VectorBaseline.model_validate(baseline)
    if set(value["artifacts"]) != {"nodes.jsonl", "edges.jsonl"} | {
        name + ".jsonl" for name in names
    }:
        raise IntegrityError("Snapshot artifact catalog is incomplete")
    for name, artifact in value["artifacts"].items():
        if Path(name).name != name:
            raise IntegrityError("Invalid snapshot artifact name")
        path = directory / name
        if (
            path.is_symlink()
            or path.stat().st_size != artifact["bytes"]
            or file_sha256(path) != artifact["sha256"]
        ):
            raise IntegrityError("Workspace snapshot artifact changed")
    return value
