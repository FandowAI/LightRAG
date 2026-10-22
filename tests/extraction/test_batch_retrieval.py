"""Durable progress, isolated failures, complete exports and truthful handoff."""

import asyncio
import json

import httpx
import pytest

from lightrag.batch_retrieval import BatchConflict, BatchManager, ModelUnavailable
from lightrag.entity_packages import PackageSettings

pytestmark = [pytest.mark.offline, pytest.mark.asyncio]


def package(center):
    text = center + "完整原文" * 10000
    return {
        "packages": [
            {
                "center": center,
                "package_id": "package-" + center,
                "entities": [{"name": center, "depth": 0, "path": [center]}],
                "relationships": [],
                "chunks": [
                    {
                        "chunk_id": center,
                        "content": text,
                        "provenance": [{"chunk_id": center, "file_path": center}],
                        "via_entities": [center],
                        "origins": ["entity"],
                        "memberships": [[center]],
                        "variants": [],
                    }
                ],
                "length_notice": {"review_threshold": 12000, "was_truncated": False},
                "distillation_input": {
                    "text": text,
                    "created_at": None,
                    "updated_at": None,
                    "metadata": {"title": center},
                },
            }
        ]
    }


async def done(manager):
    await asyncio.gather(*list(manager.tasks))


async def test_disconnected_local_embedding_does_not_create_a_batch(
    tmp_path, monkeypatch
):
    async def refused(*args, **kwargs):
        raise ConnectionRefusedError()

    monkeypatch.setenv("EMBEDDING_BINDING_HOST", "http://127.0.0.1:19622/v1")
    monkeypatch.setattr("lightrag.batch_retrieval.asyncio.open_connection", refused)
    manager = BatchManager(object(), root=tmp_path, focus=False)
    with pytest.raises(ModelUnavailable, match="embedding_unavailable"):
        await manager.start(["产品供应部"], PackageSettings())
    with manager.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert not manager.tasks


async def test_sequential_progress_survives_one_failed_center_and_exports_full_records(
    tmp_path,
):
    gate, entered = asyncio.Event(), asyncio.Event()
    calls = []

    async def retrieve(rag, center, settings):
        calls.append(center)
        if center == "bad":
            entered.set()
            await gate.wait()
            raise RuntimeError("private provider detail")
        return package(center)

    manager = BatchManager(object(), root=tmp_path, focus=False, retrieve=retrieve)
    job = await manager.start(["first", "bad", "last"], PackageSettings())
    await entered.wait()
    status = manager.get(job["id"])
    assert status["processed"] == 1 and status["package_count"] == 1
    assert status["current_center"] == "bad"
    gate.set()
    await done(manager)
    status = manager.get(job["id"])
    assert status["status"] == "completed_with_errors"
    assert (status["processed"], status["failed"], status["package_count"]) == (3, 1, 2)
    assert calls == ["first", "bad", "last"]
    assert "private provider detail" not in json.dumps(status)
    rows = [json.loads(line) for line in manager.jsonl(job["id"])]
    assert [r["metadata"]["title"] for r in rows] == ["first", "last"]
    assert len(rows[0]["text"]) > 12000
    reopened = BatchManager(object(), root=tmp_path, focus=False)
    assert reopened.get(job["id"])["package_count"] == 2


async def test_stop_finishes_current_center_and_preserves_packages_and_global_single_job(
    tmp_path,
):
    gate, entered = asyncio.Event(), asyncio.Event()

    async def retrieve(rag, center, settings):
        entered.set()
        await gate.wait()
        return package(center)

    manager = BatchManager(object(), root=tmp_path, focus=False, retrieve=retrieve)
    job = await manager.start(["a", "b"], PackageSettings())
    await entered.wait()
    another = BatchManager(object(), root=tmp_path, focus=False)
    with pytest.raises(BatchConflict):
        await another.start(["c"], PackageSettings())
    manager.cancel(job["id"])
    gate.set()
    await done(manager)
    status = manager.get(job["id"])
    assert status["status"] == "cancelled"
    assert status["processed"] == status["package_count"] == 1
    assert status["items"][1]["status"] == "queued"
    assert len(list(manager.records(job["id"]))) == 1


async def test_empty_evidence_and_duplicate_centers_are_counted_truthfully(tmp_path):
    async def retrieve(rag, center, settings):
        return {"packages": []}

    manager = BatchManager(object(), root=tmp_path, focus=False, retrieve=retrieve)
    job = await manager.start([" a ", "a", "b"], PackageSettings())
    await done(manager)
    status = manager.get(job["id"])
    assert status["total"] == status["processed"] == status["empty"] == 2
    assert status["package_count"] == 0 and list(manager.records(job["id"])) == []


async def test_dead_owner_is_interrupted_without_erasing_completed_files(tmp_path):
    async def retrieve(rag, center, settings):
        return package(center)

    manager = BatchManager(object(), root=tmp_path, focus=False, retrieve=retrieve)
    job = await manager.start(["a"], PackageSettings())
    await done(manager)
    with manager.connect() as db:
        stored = manager._read(db, job["id"])
        stored["status"] = "running"
        manager._save(db, stored)
        db.execute("UPDATE jobs SET pid=?,birth=? WHERE id=?", (99999999, 0, job["id"]))
    assert manager.get(job["id"])["status"] == "interrupted"
    assert (tmp_path / job["id"] / "entity-0000.json").exists()


async def test_handoff_requires_configuration_and_explicit_ack_and_retries_idempotently(
    tmp_path, monkeypatch
):
    async def retrieve(rag, center, settings):
        return package(center)

    manager = BatchManager(object(), root=tmp_path, focus=False, retrieve=retrieve)
    job = await manager.start(["a"], PackageSettings())
    await done(manager)
    monkeypatch.delenv("DISTILLATION_INGEST_URL", raising=False)
    with pytest.raises(ValueError):
        await manager.handoff(job["id"])
    monkeypatch.setenv("DISTILLATION_INGEST_URL", "https://distill.example/ingest")
    calls = []
    responses = [False, True]

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            calls.append(kwargs)
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"accepted": responses.pop(0), "job_id": "remote-job"},
            )

    monkeypatch.setattr("lightrag.batch_retrieval.httpx.AsyncClient", Client)
    with pytest.raises(ValueError):
        await manager.handoff(job["id"])
    assert manager.get(job["id"])["handoff"]["status"] == "unconfirmed"
    assert manager.get(job["id"])["package_count"] == 1
    accepted = await manager.handoff(job["id"])
    assert accepted["handoff"]["status"] == "accepted"
    await manager.handoff(job["id"])
    assert len(calls) == 2
    assert (
        calls[0]["headers"]["Idempotency-Key"] == calls[1]["headers"]["Idempotency-Key"]
    )
    assert (
        calls[1]["json"]["records"][0]["text"]
        == package("a")["packages"][0]["distillation_input"]["text"]
    )


async def test_invalid_and_multiple_packages_cannot_be_reported_as_success(tmp_path):
    async def retrieve(rag, center, settings):
        return {"packages": package(center)["packages"] * 2}

    manager = BatchManager(object(), root=tmp_path, focus=False, retrieve=retrieve)
    with pytest.raises(ValueError):
        await manager.start([" "], PackageSettings())
    job = await manager.start(["a"], PackageSettings())
    await done(manager)
    assert manager.get(job["id"])["failed"] == 1
    assert manager.get(job["id"])["package_count"] == 0
