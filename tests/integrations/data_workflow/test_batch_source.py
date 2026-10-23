"""Batch publication, source conversion, workspace ownership and durable association."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from lightrag.integrations.data_workflow.batch_source import (
    read_batch_results,
    resolve_batch,
)
from lightrag.integrations.data_workflow.config import Limits
from lightrag.integrations.data_workflow.conversion import convert_result
from lightrag.integrations.data_workflow.service import DataWorkflowService
from lightrag.integrations.data_workflow.storage import JobConflict
from lightrag.integrations.data_workflow.worker import run

pytestmark = pytest.mark.offline


@pytest.fixture
def publication(tmp_path, monkeypatch):
    from lightrag.batch_retrieval import BatchManager

    monkeypatch.setenv("LIGHTRAG_BATCH_OUTPUT_DIR", str(tmp_path / "exports"))
    rag = SimpleNamespace(
        workspace="test-workspace", working_dir=str(tmp_path / "storage")
    )
    manager = BatchManager(rag)
    batch_id = "batch-1"
    directory = manager.root / batch_id
    directory.mkdir()
    original = "  合同原文。\n${literal} {literal}"
    row = {
        "schema_version": "enterprise-evidence/1.0",
        "batch_id": batch_id,
        "workspace": rag.workspace,
        "package_id": "p-1",
        "central_entity": "合同",
        "entities": [
            {
                "name": "合同",
                "entity_type": "DOC",
                "description": "合同描述",
                "source_chunk_ids": ["c-1"],
            }
        ],
        "relationships": [
            {"source": "合同", "target": "审批", "description": "需要审批"}
        ],
        "original_texts": [{"passage_id": "c-1", "text": original}],
        "text": "IGNORE THIS RENDERED TEXT",
        "metadata": {"private": "do not deliver"},
        "shared_evidence": [{"owner_package_id": "p-1", "chunk_id": "archive-only"}],
    }
    manifest = {
        "schema_version": "enterprise-evidence/1.0",
        "batch_id": batch_id,
        "workspace": rag.workspace,
        "files": {},
    }
    for filename, rows in [
        ("packages.jsonl", [row]),
        (
            "sources.jsonl",
            [{"chunk_id": "archive-only", "text": "DO NOT EXPAND SHARED TEXT"}],
        ),
    ]:
        raw = (
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
        ).encode()
        (directory / filename).write_bytes(raw)
        manifest["files"][filename] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    job = {
        "id": batch_id,
        "status": "completed",
        "package_count": 1,
        "export_path": str(directory / "packages.jsonl"),
        "handoff": {"status": "not_submitted"},
    }
    with manager.connect() as db:
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?)",
            (batch_id, json.dumps(job), "completed", manager.pid, manager.birth),
        )
    return rag, directory, manager, original


def test_batch_projection_preserves_original_and_only_delivers_three_fields(
    publication,
):
    rag, _, _, original = publication
    snapshot = resolve_batch(rag, "batch-1")
    pairs = list(read_batch_results(snapshot, Limits()))
    assert len(pairs) == 1 and pairs[0][1] is None
    envelope = convert_result(pairs[0][0])
    assert set(envelope) == {"text"}
    context = json.loads(envelope["text"])
    assert context == {
        "entities": [{"entity": "合同", "type": "DOC", "description": "合同描述"}],
        "relationships": [
            {"entity1": "合同", "entity2": "审批", "description": "需要审批"}
        ],
        "text_chunks": [original],
    }
    assert "DO NOT EXPAND" not in envelope["text"]
    assert "private" not in envelope["text"]


@pytest.mark.parametrize(
    "filename", ["manifest.json", "packages.jsonl", "sources.jsonl"]
)
def test_modified_publication_is_refused_before_any_records(publication, filename):
    rag, directory, _, _ = publication
    snapshot = resolve_batch(rag, "batch-1")
    with (directory / filename).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError, match="changed|checksum"):
        next(read_batch_results(snapshot, Limits()))


def test_batch_input_limits_and_workspace_isolation(publication):
    rag, _, manager, _ = publication
    snapshot = resolve_batch(rag, "batch-1")
    with pytest.raises(ValueError, match="max_input_bytes"):
        list(read_batch_results(snapshot, Limits(max_input_bytes=4)))
    with pytest.raises(KeyError):
        resolve_batch(
            SimpleNamespace(workspace="another", working_dir=rag.working_dir), "batch-1"
        )
    manager.update("batch-1", lambda j: j.update(status="cancelled"))
    with pytest.raises(JobConflict, match="finish"):
        resolve_batch(rag, "batch-1")


def batch_service(bridge, publication):
    _, config, _, _ = bridge
    rag, _, _, _ = publication
    settings = yaml.safe_load(config.read_text())
    settings["input"]["paths"] = []
    config.write_text(yaml.safe_dump(settings))
    return DataWorkflowService(
        rag.workspace,
        config,
        batch_lookup=lambda batch_id: resolve_batch(rag, batch_id),
    )


def test_batch_workflow_needs_no_input_paths_and_runs_shared_executor(
    bridge, publication, monkeypatch
):
    service = batch_service(bridge, publication)
    monkeypatch.setattr(service, "_spawn", lambda *args: None)
    job = service.submit_batch("batch-1", start=False)
    _, store, _ = service._resources()
    run(store, job["job_id"], "prepare")
    assert service.get(job["job_id"])["status"] == "ready"
    store.claim_start(job["job_id"], 1)
    run(store, job["job_id"], "run")
    done = service.get(job["job_id"])
    assert done["status"] == "completed"
    assert done["progress"]["percent"] == 100
    assert done["source"] == {"type": "batch", "batch_id": "batch-1"}
    assert done["progress"]["accepted_sources"] == 1
    assert service.submit_batch("batch-1")["job_id"] == job["job_id"]
    fresh = batch_service(bridge, publication)
    assert fresh.list(1, 0, "batch-1")["items"][0]["job_id"] == job["job_id"]
    assert fresh.list(1, 0, "missing")["total"] == 0


def test_concurrent_clicks_create_one_job_and_worker(bridge, publication, monkeypatch):
    service = batch_service(bridge, publication)
    from unittest.mock import Mock

    spawn = Mock()
    monkeypatch.setattr(service, "_spawn", spawn)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.submit_batch("batch-1"), range(6)))
    assert len({job["job_id"] for job in results}) == 1
    assert spawn.call_count == 1
    assert service.list(20, 0)["total"] == 1


def test_failure_keeps_same_association_and_never_restarts(
    bridge, publication, monkeypatch
):
    service = batch_service(bridge, publication)
    monkeypatch.setattr(service, "_spawn", lambda *args: None)
    job = service.submit_batch("batch-1")
    _, store, _ = service._resources()
    store.update(job["job_id"], status="failed", phase="failed", error="test failure")
    assert service.submit_batch("batch-1")["status"] == "failed"
    with pytest.raises(ValueError, match="reserved"):
        service.submit("batch:batch-1")


def test_resolver_rejects_missing_manifest_and_wrong_workspace(publication):
    rag, directory, _, _ = publication
    marker = directory / "manifest.json"
    manifest = json.loads(marker.read_text())
    manifest["workspace"] = "another"
    marker.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="workspace"):
        resolve_batch(rag, "batch-1")
    marker.unlink()
    with pytest.raises(JobConflict, match="incomplete"):
        resolve_batch(rag, "batch-1")


def test_symlink_artifacts_cannot_escape_published_directory(publication, tmp_path):
    rag, directory, _, _ = publication
    snapshot = resolve_batch(rag, "batch-1")
    path = directory / "sources.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        list(read_batch_results(snapshot, Limits()))


def test_batch_configuration_readiness_does_not_require_directory_paths(
    bridge, publication
):
    service = batch_service(bridge, publication)
    configuration = service.configuration()
    assert configuration["ready"] is True and configuration["input"]["paths"] == []
    _, _, _, recipe = bridge
    document = yaml.safe_load(recipe.read_text())
    document["models"] = {
        "generation": {
            "api_url": "https://example.com",
            "model_name": "replace-with-model",
        }
    }
    recipe.write_text(yaml.safe_dump(document))
    assert service.configuration()["ready"] is False
    assert service.configuration()["issues"]
