"""Retrieved originals go straight to dedup and export, without source review."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.batch_retrieval import BatchManager
from lightrag.entity_packages import (
    PackageSettings,
    assemble,
    deduplicate,
    equivalence_judge,
)

pytestmark = [pytest.mark.offline, pytest.mark.asyncio]


async def test_default_batch_keeps_every_original_and_never_selects_topics(tmp_path):
    async def retrieve(rag, center, settings):
        return await assemble(
            center,
            {center: {}},
            [],
            {
                "a": {
                    "content": "Transcript meaning is unclear, but keep this original.",
                    "file_path": "meeting.md",
                    "full_doc_id": "doc",
                },
                "b": {
                    "content": "A different business matter from the same meeting.",
                    "file_path": "meeting.md",
                    "full_doc_id": "doc",
                },
            },
            {center: ["a", "b"]},
            {},
            [],
            settings,
            len,
        )

    selector = AsyncMock(side_effect=AssertionError("No relevance-model calls"))
    manager = BatchManager(
        SimpleNamespace(tokenizer=SimpleNamespace(encode=list)),
        root=tmp_path,
        retrieve=retrieve,
        selector=selector,
    )
    job = await manager.start(["department"], PackageSettings())
    await asyncio.gather(*manager.tasks)
    final = manager.get(job["id"])
    assert final["status"] == "completed"
    assert final["policy"] == "retrieval_dedup_v2"
    assert "review_pending" not in final
    assert final["dedup_llm_calls"] == 0
    selector.assert_not_awaited()
    row = json.loads(
        (tmp_path / job["id"] / "packages.jsonl").read_text(encoding="utf-8")
    )
    assert len(row["original_texts"]) == 2
    assert (
        "meaning is unclear" in row["text"]
        and "different business matter" in row["text"]
    )
    assert not list((tmp_path / job["id"]).glob("focus-*.json"))


async def test_ordinary_similarity_does_not_trigger_dedup_model():
    def record(cid, text):
        return {
            "chunk_id": cid,
            "content": text,
            "provenance": [{"file_path": "notes.md"}],
        }

    left = record("a", "Common business introduction. " * 8 + "Choose candidate Alpha.")
    right = record("b", "Common business introduction. " * 8 + "Choose candidate Beta.")
    judge = AsyncMock(side_effect=AssertionError("Not sufficiently close"))
    # A meaningful conclusion difference is preserved even with a long common prefix.
    settings = PackageSettings()
    kept, _ = await deduplicate([left, right], settings, judge)
    assert len(kept) == 2
    judge.assert_not_awaited()
    assert PackageSettings().near_threshold == 0.995


async def test_dedup_confirmation_has_one_shared_hard_budget_and_no_review_queue():
    role = AsyncMock(return_value='{"equivalent":false,"reason":"Different context"}')
    judge = equivalence_judge(
        SimpleNamespace(role_llm_funcs={"extract": role}), max_checks=2
    )
    left = {"content": "A" * 100, "provenance": []}
    right = {"content": "A" * 99 + ".", "provenance": []}
    for _ in range(5):
        result = await judge(left, right)
        assert result["equivalent"] is False
    assert role.await_count == 2 and judge.llm_calls == 2
    assert result["reason"] == "confirmation_budget_exhausted"


async def test_model_failure_preserves_near_duplicates_without_review():
    left = {
        "chunk_id": "a",
        "content": "Business explanation. " * 80,
        "provenance": [{"file_path": "notes.md"}],
    }
    right = {
        "chunk_id": "b",
        "content": left["content"] + "!",
        "provenance": [{"file_path": "notes.md"}],
    }
    kept, audit = await deduplicate(
        [left, right], PackageSettings(), AsyncMock(side_effect=TimeoutError())
    )
    assert len(kept) == 2
    assert audit[0]["reason"] == "equivalence_check_failed"
