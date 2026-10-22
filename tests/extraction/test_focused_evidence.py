"""Grounded topic extraction, complete contexts and order-independent ownership."""

import asyncio
from types import SimpleNamespace

import pytest

from lightrag.batch_retrieval import BatchManager
from lightrag.entity_packages import PackageSettings, assemble
from lightrag.focused_evidence import (
    build_packages,
    merge_topics,
    source_units,
    validate_selection,
    model_selector,
)

pytestmark = [pytest.mark.offline, pytest.mark.asyncio]


def legacy_manager(*args, **kwargs):
    return BatchManager(*args, focus=True, **kwargs)


async def test_model_selector_does_not_accept_repaired_truncated_json():
    seen = {}

    async def role(prompt, **kwargs):
        seen.update(kwargs)
        return '{"review":false,"topics":['

    select = model_selector(
        SimpleNamespace(role_llm_funcs={"extract": role}, llm_model_name="glm-5.3")
    )
    import json

    with pytest.raises(json.JSONDecodeError):
        await select({})
    assert seen["reasoning_effort"] == "low" and seen["max_tokens"] >= 12000


async def retrieved(center, text):
    return await assemble(
        center,
        {center: {}},
        [],
        {"source": {"content": text, "file_path": "meeting", "full_doc_id": "doc"}},
        {center: ["source"]},
        {},
        [],
        PackageSettings(),
        len,
    )


def response(units, centers, chosen=None):
    return {
        "review": False,
        "exclusion_reason": "Other topics do not explain this decision.",
        "topics": [
            {
                "unit_ids": chosen or [u["id"] for u in units],
                "scores": {
                    c: {
                        "score": 3 if c == "supply" else 1,
                        "reason": "procurement decision",
                    }
                    for c in centers
                },
            }
        ],
    }


async def test_units_preserve_literal_offsets_and_validator_rejects_invented_or_disjoint_content():
    chunk = {
        "chunk_id": "source",
        "content": "Demand is uncertain.\n\nDo not purchase equipment.\n\nRecruit hosts.",
        "provenance": [],
    }
    units = source_units(chunk)
    assert all(
        chunk["content"][u["start_char"] : u["end_char"]] == u["text"] for u in units
    )
    for ids in [["invented"], [units[0]["id"], units[2]["id"]]]:
        with pytest.raises(ValueError):
            validate_selection(response(units, ["supply"], ids), units, ["supply"])
    assert (
        len(
            validate_selection(
                response(units, ["supply"], [u["id"] for u in units[:2]]),
                units,
                ["supply"],
            )
        )
        == 1
    )


async def test_numbered_meeting_matters_split_even_without_blank_lines():
    chunk = {
        "chunk_id": "source",
        "content": "1. Recruit hosts.  \n2. Keep inventory below 1.5 million.  \n3. Use the ERP system.",
        "provenance": [],
    }
    units = source_units(chunk)
    assert len(units) == 3
    assert "1.5 million" in units[1]["text"]
    assert all(
        chunk["content"][u["start_char"] : u["end_char"]] == u["text"] for u in units
    )


async def test_relation_candidates_do_not_pull_all_lifetime_evidence_of_neighbor():
    nodes = {"supply": {"source_id": "own"}, "brand": {"source_id": "advertising"}}
    edges = [{"source": "supply", "target": "brand", "source_id": "equipment"}]
    chunks = {
        cid: {"content": text, "file_path": "meeting"}
        for cid, text in {
            "own": "department responsibilities",
            "advertising": "brand advertising campaign",
            "equipment": "sales uncertainty delays equipment purchase",
        }.items()
    }
    result = await assemble(
        "supply",
        nodes,
        edges,
        chunks,
        {},
        {},
        [],
        PackageSettings(),
        len,
        relation_evidence_only=True,
    )
    assert {c["chunk_id"] for c in result["packages"][0]["chunks"]} == {
        "own",
        "equipment",
    }
    assert {e["name"] for e in result["entities"]} == {"supply", "brand"}


async def test_shared_topic_owner_uses_relevance_and_preserves_reason_condition_and_negation():
    text = "Demand is uncertain.\n\nDo not purchase equipment.\n\nRecruit hosts."
    results = [await retrieved(c, text) for c in ["marketing", "supply"]]
    units = source_units(results[0]["packages"][0]["chunks"][0])
    verdict = response(units, ["marketing", "supply"], [u["id"] for u in units[:2]])
    topics = validate_selection(verdict, units, ["marketing", "supply"])
    audits = [{"status": "selected", "units": units, "topics": topics}]
    outputs, _ = await build_packages(results, audits, PackageSettings(), len)
    reversed_outputs, _ = await build_packages(
        results[::-1], audits, PackageSettings(), len
    )
    supply = next(r for r in outputs if r["center"] == "supply")["packages"][0]
    text = supply["distillation_input"]["text"]
    assert "Demand is uncertain." in text and "Do not purchase equipment." in text
    assert "Recruit hosts." not in text
    assert {
        r["center"]: [c["chunk_id"] for p in r["packages"] for c in p["chunks"]]
        for r in outputs
    } == {
        r["center"]: [c["chunk_id"] for p in r["packages"] for c in p["chunks"]]
        for r in reversed_outputs
    }


async def test_overlapping_context_selections_merge_without_repeating_source_units():
    chunk = {
        "chunk_id": "source",
        "content": "Reason.\n\nCondition.\n\nDecision.",
        "provenance": [{"chunk_id": "source", "file_path": "meeting"}],
    }
    units = source_units(chunk)
    a = response(units, ["supply"], [u["id"] for u in units[:2]])["topics"][0]
    b = response(units, ["supply"], [u["id"] for u in units[1:]])["topics"][0]
    passages = merge_topics([{"status": "selected", "units": units, "topics": [a, b]}])
    assert len(passages) == 1
    assert passages[0]["content"] == chunk["content"]
    assert len(passages[0]["source_units"]) == 3


async def test_focused_production_pipeline_selects_once_and_never_falls_back_to_raw_text(
    tmp_path,
):
    calls = []

    async def retrieve(rag, center, settings):
        return await retrieved(
            center,
            "Demand is uncertain.\n\nDo not purchase equipment.\n\nRecruit hosts.",
        )

    async def select(payload):
        calls.append(payload)
        return response(
            payload["units"],
            payload["centers"],
            [u["id"] for u in payload["units"][:2]],
        )

    manager = legacy_manager(
        SimpleNamespace(tokenizer=SimpleNamespace(encode=list)),
        root=tmp_path,
        retrieve=retrieve,
        selector=select,
    )
    job = await manager.start(["marketing", "supply"], PackageSettings())
    await asyncio.gather(*list(manager.tasks))
    final = manager.get(job["id"])
    assert final["status"] == "completed" and final["package_count"] == 1
    assert len(calls) == 1
    records = list(manager.records(job["id"]))
    assert len(records) == 1 and records[0]["metadata"]["central_entity"] == "supply"
    assert "Recruit hosts." not in records[0]["text"]
    assert (tmp_path / job["id"] / "candidate-0000.json").exists()


async def test_failed_judgment_is_visible_and_does_not_export_unfiltered_evidence(
    tmp_path,
):
    async def retrieve(rag, center, settings):
        return await retrieved(center, "uncertain raw meeting")

    async def invalid(payload):
        return {"topics": [{"unit_ids": ["invented"]}]}

    manager = legacy_manager(
        SimpleNamespace(tokenizer=SimpleNamespace(encode=list)),
        root=tmp_path,
        retrieve=retrieve,
        selector=invalid,
    )
    job = await manager.start(["supply"], PackageSettings())
    await asyncio.gather(*list(manager.tasks))
    final = manager.get(job["id"])
    assert final["status"] == "completed_with_errors" and final["review_pending"] == 1
    assert final["package_count"] == 0 and list(manager.records(job["id"])) == []


async def test_stop_during_selection_keeps_audit_without_publishing_half_selected_packages(
    tmp_path,
):
    entered, gate = asyncio.Event(), asyncio.Event()
    calls = []

    async def retrieve(rag, center, settings):
        return await retrieved(center, "condition and decision")

    async def select(payload):
        calls.append(payload)
        entered.set()
        await gate.wait()
        return response(payload["units"], payload["centers"])

    manager = legacy_manager(
        SimpleNamespace(tokenizer=SimpleNamespace(encode=list)),
        root=tmp_path,
        retrieve=retrieve,
        selector=select,
    )
    job = await manager.start(["supply"], PackageSettings())
    await entered.wait()
    manager.cancel(job["id"])
    gate.set()
    await asyncio.gather(*list(manager.tasks))
    assert manager.get(job["id"])["status"] == "cancelled"
    assert not list(manager.records(job["id"]))
    assert len(list((tmp_path / job["id"]).glob("focus-*.json"))) == 1
    await manager.resume(job["id"])
    await asyncio.gather(*list(manager.tasks))
    assert manager.get(job["id"])["status"] == "completed"
    assert len(calls) == 1


async def test_selection_never_changes_the_existing_shared_role_limit_on_failure(
    tmp_path,
):
    limits = []

    async def retrieve(rag, center, settings):
        return await retrieved(center, "source decision")

    async def invalid(payload):
        raise ValueError("invalid selection")

    rag = SimpleNamespace(
        tokenizer=SimpleNamespace(encode=list),
        get_llm_role_config=lambda role: {"max_async": 1},
        update_llm_role_config=lambda role, **options: limits.append(
            options["max_async"]
        ),
    )
    manager = legacy_manager(rag, root=tmp_path, retrieve=retrieve, selector=invalid)
    job = await manager.start(["supply"], PackageSettings())
    await asyncio.gather(*list(manager.tasks))
    assert manager.get(job["id"])["status"] == "completed_with_errors"
    assert limits == []


async def test_adjacent_chunk_overlap_is_emitted_once_with_both_source_locations():
    shared = "Due to uncertain sales demand, the factory must postpone purchasing the equipment."
    a = {
        "chunk_id": "doc-chunk-000",
        "content": "Premise.\n\n" + shared,
        "provenance": [
            {"chunk_id": "doc-chunk-000", "file_path": "meeting", "full_doc_id": "doc"}
        ],
    }
    b = {
        "chunk_id": "doc-chunk-001",
        "content": shared + "\n\nReview next year.",
        "provenance": [
            {"chunk_id": "doc-chunk-001", "file_path": "meeting", "full_doc_id": "doc"}
        ],
    }
    audits = []
    for source in [a, b]:
        units = source_units(source)
        audits.append(
            {
                "status": "selected",
                "units": units,
                "topics": response(units, ["supply"])["topics"],
            }
        )
    passages = merge_topics(audits)
    assert len(passages) == 1 and passages[0]["content"].count(shared) == 1
    assert (
        "Premise." in passages[0]["content"]
        and "Review next year." in passages[0]["content"]
    )
    assert len(passages[0]["duplicate_source_units"]) == 1
    assert {p["source_chunk_id"] for p in passages[0]["provenance"]} == {
        "doc-chunk-000",
        "doc-chunk-001",
    }


async def test_rebuild_creates_new_packages_without_changing_old_job_or_repeating_selection(
    tmp_path,
):
    calls = []

    async def retrieve(rag, center, settings):
        return await retrieved(center, "literal decision")

    async def select(payload):
        calls.append(payload)
        return response(payload["units"], payload["centers"])

    manager = legacy_manager(
        SimpleNamespace(tokenizer=SimpleNamespace(encode=list)),
        root=tmp_path,
        retrieve=retrieve,
        selector=select,
    )
    old = await manager.start(["supply"], PackageSettings())
    await asyncio.gather(*list(manager.tasks))
    old_records = list(manager.records(old["id"]))
    new = await manager.start(["supply"], PackageSettings(), reuse_from=old["id"])
    await asyncio.gather(*list(manager.tasks))
    assert new["id"] != old["id"] and manager.get(new["id"])["rebuild_of"] == old["id"]
    assert manager.get(new["id"])["status"] == "completed" and len(calls) == 1
    assert list(manager.records(old["id"])) == old_records


async def test_partial_chunk_overlap_stitches_original_decision_without_repeating_prefix():
    overlap = "Expenses have priorities and a clear structure is needed before deciding the budget. "
    left = "Understand the financial report first. " + overlap
    right = overlap + "Then review the provisional budget before approving it."
    audits = []
    for cid, text in [("doc-chunk-000", left), ("doc-chunk-001", right)]:
        units = source_units(
            {
                "chunk_id": cid,
                "content": text,
                "provenance": [{"chunk_id": cid, "file_path": "meeting"}],
            }
        )
        audits.append(
            {
                "status": "selected",
                "units": units,
                "topics": response(units, ["supply"])["topics"],
            }
        )
    passages = merge_topics(audits)
    assert len(passages) == 1
    assert passages[0]["content"] == left.rstrip() + " " + right[len(overlap) :]
    assert passages[0]["content"].count(overlap.strip()) == 1
    texts = {"doc-chunk-000": left, "doc-chunk-001": right}
    assert passages[0]["content"] == "".join(
        u["joiner"] + texts[u["source_chunk_id"]][u["start_char"] : u["end_char"]]
        for u in passages[0]["source_units"]
    )
