"""Production JSONL must preserve graph indexes and verifiable literal evidence."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from lightrag.batch_retrieval import BatchManager
from lightrag.entity_packages import PackageSettings, assemble
from lightrag.utils import make_relation_chunk_key

pytestmark = [pytest.mark.offline, pytest.mark.asyncio]


async def run_batch(tmp_path):
    async def retrieve(rag, center, settings):
        return await assemble(
            center,
            {
                "supply": {"entity_type": "department", "source_id": "c1"},
                "inventory": {"entity_type": "topic", "source_id": "c1"},
            },
            [
                {
                    "source": "supply",
                    "target": "inventory",
                    "source_id": "c1",
                    "description": "inventory planning",
                }
            ],
            {
                "c1": {
                    "content": "Demand is uncertain.\n\nDo not buy equipment yet.",
                    "file_path": "meeting.md",
                    "full_doc_id": "doc",
                }
            },
            {},
            {make_relation_chunk_key("supply", "inventory"): ["c1"]},
            [],
            settings,
            len,
        )

    async def select(payload):
        return {
            "review": False,
            "exclusion_reason": "No unrelated matters.",
            "topics": [
                {
                    "unit_ids": [u["id"] for u in payload["units"]],
                    "scores": {
                        name: {"score": 3, "reason": "Decision basis"}
                        for name in payload["centers"]
                    },
                }
            ],
        }

    manager = BatchManager(
        SimpleNamespace(tokenizer=SimpleNamespace(encode=list), workspace="enterprise"),
        root=tmp_path,
        retrieve=retrieve,
        selector=select,
    )
    job = await manager.start(["supply"], PackageSettings(near_dedup=False))
    await asyncio.gather(*manager.tasks)
    return manager, manager.get(job["id"])


async def test_published_jsonl_contains_entities_relations_originals_and_source_archive(
    tmp_path,
):
    manager, job = await run_batch(tmp_path)
    assert job["status"] == "completed"
    folder = tmp_path / job["id"]
    assert job["export_path"] == str((folder / "packages.jsonl").resolve())
    record = json.loads((folder / "packages.jsonl").read_text(encoding="utf-8"))
    assert record["schema_version"] == "enterprise-evidence/1.0"
    assert {e["name"] for e in record["entities"]} == {"supply", "inventory"}
    edge = record["relationships"][0]
    assert edge["description"] == "inventory planning"
    assert edge["source_chunk_ids"] == ["c1"]
    original = record["original_texts"][0]
    assert "Do not buy" in original["text"]
    source = json.loads((folder / "sources.jsonl").read_text(encoding="utf-8"))
    assert source["chunk_id"] == "c1"
    assert source["text"] == "Demand is uncertain.\n\nDo not buy equipment yet."
    rebuilt = "".join(
        u.get("joiner", "") + source["text"][u["start_char"] : u["end_char"]]
        for u in original["source_units"]
    )
    assert rebuilt == original["text"]
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for filename, info in manifest["files"].items():
        assert (
            hashlib.sha256((folder / filename).read_bytes()).hexdigest()
            == info["sha256"]
        )
        assert info["rows"] == 1
    assert list(manager.jsonl(job["id"])) == (folder / "packages.jsonl").read_text(
        encoding="utf-8"
    ).splitlines(keepends=True)


async def test_source_mismatch_refuses_publication(tmp_path):
    manager, job = await run_batch(tmp_path)
    folder = tmp_path / job["id"]
    for path in folder.glob("focus-*.json"):
        audit = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(audit, dict) and audit.get("source_snapshots"):
            audit["source_snapshots"][0]["content"] = "changed source"
            path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="Conflicting source"):
        manager.persist_export(job["id"])


async def test_output_root_is_configurable_and_scoped_by_workspace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LIGHTRAG_BATCH_OUTPUT_DIR", str(tmp_path))
    manager = BatchManager(
        SimpleNamespace(workspace="enterprise", working_dir="unused")
    )
    assert manager.root == (tmp_path / "enterprise").resolve()
    with pytest.raises(ValueError, match="single directory"):
        BatchManager(SimpleNamespace(workspace="../outside", working_dir="unused"))
