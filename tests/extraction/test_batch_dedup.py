"""Batch-wide exact/near dedup, source ownership and conservative conflicts."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from lightrag.batch_dedup import deduplicate_result
from lightrag.batch_retrieval import BatchManager
from lightrag.entity_packages import PackageSettings, assemble

pytestmark = [pytest.mark.offline, pytest.mark.asyncio]


async def result(center, chunks):
    return await assemble(
        center,
        {center: {}},
        [],
        {
            cid: {"content": text, "file_path": "source", "full_doc_id": cid}
            for cid, text in chunks.items()
        },
        {center: list(chunks)},
        {},
        [],
        PackageSettings(),
        len,
    )


async def test_cross_package_same_id_and_exact_text_are_removed_before_rendering():
    first = await result("a", {"shared": "shared knowledge", "one": "unique alpha"})
    second = await result(
        "b",
        {"shared": "shared knowledge", "copy": "unique alpha", "two": "unique beta"},
    )
    original = deepcopy(first)
    cleaned = await deduplicate_result(second, [first], PackageSettings(), len)
    package = cleaned["packages"][0]
    assert [c["chunk_id"] for c in package["chunks"]] == ["two"]
    assert "shared knowledge" not in package["distillation_input"]["text"]
    assert "unique alpha" not in package["text"]
    assert "unique beta" in package["text"]
    assert len(cleaned["shared_evidence"]) == 2
    assert {r["owner_center"] for r in cleaned["shared_evidence"]} == {"a"}
    assert first == original


async def test_all_duplicate_center_has_no_empty_or_reference_only_teacher_record():
    first = await result("a", {"one": "shared knowledge"})
    second = await result("b", {"one": "shared knowledge"})
    cleaned = await deduplicate_result(second, [first], PackageSettings(), len)
    assert cleaned["packages"] == []
    assert cleaned["summary"]["cross_package_duplicates"] == 1
    assert cleaned["shared_evidence"][0]["chunk_id"] == "one"


async def test_near_duplicate_requires_positive_equivalence_and_keeps_changed_numbers():
    left = "Recruiting methodology " * 40 + "finish review."
    right = "Recruiting methodology " * 40 + "complete review."
    first = await result("a", {"one": left})
    second = await result("b", {"two": right})
    without_judge = await deduplicate_result(second, [first], PackageSettings(), len)
    assert without_judge["packages"]

    async def approve(a, b):
        return {
            **{
                k: True
                for k in (
                    "equivalent",
                    "same_scope",
                    "same_time",
                    "same_conditions",
                    "same_numbers_units",
                    "same_conclusions",
                    "no_unique_information",
                )
            },
            "quotes": {"left": a["content"][:30], "right": b["content"][:30]},
        }

    cleaned = await deduplicate_result(second, [first], PackageSettings(), len, approve)
    assert not cleaned["packages"]
    assert cleaned["shared_evidence"][0]["reason"] == "near_duplicate"
    changed = await result("c", {"three": left + " budget 200 dollars"})
    preserved = await deduplicate_result(
        changed, [first], PackageSettings(), len, approve
    )
    assert preserved["packages"]


async def test_batch_commits_dedup_before_export_and_skips_fully_repeated_center(
    tmp_path,
):
    async def retrieve(rag, center, settings):
        return await result(
            center,
            {
                "shared": "shared knowledge",
                **({"own": "unique beta"} if center == "b" else {}),
            },
        )

    rag = SimpleNamespace(tokenizer=SimpleNamespace(encode=list))
    manager = BatchManager(rag, root=tmp_path, focus=False, retrieve=retrieve)
    job = await manager.start(["a", "b", "c"], PackageSettings())
    import asyncio

    await asyncio.gather(*list(manager.tasks))
    final = manager.get(job["id"])
    assert (
        final["package_count"],
        final["duplicate_chunks"],
        final["skipped"],
        final["failed"],
    ) == (2, 2, 1, 0)
    assert final["items"][2]["status"] == "skipped_duplicate"
    records = list(manager.records(job["id"]))
    assert records[0]["metadata"]["related_centers"] == ["a", "b", "c"]
    assert sum("shared knowledge" in r["text"] for r in records) == 1
    assert "unique beta" in records[1]["text"]


async def test_failed_center_cannot_own_or_suppress_later_evidence(tmp_path):
    async def retrieve(rag, center, settings):
        if center == "a":
            raise RuntimeError()
        return await result(center, {"shared": "shared knowledge"})

    manager = BatchManager(
        SimpleNamespace(tokenizer=SimpleNamespace(encode=list)),
        root=tmp_path,
        focus=False,
        retrieve=retrieve,
    )
    job = await manager.start(["a", "b"], PackageSettings())
    import asyncio

    await asyncio.gather(*list(manager.tasks))
    final = manager.get(job["id"])
    assert final["package_count"] == 1 and final["duplicate_chunks"] == 0
    assert "shared knowledge" in list(manager.records(job["id"]))[0]["text"]
