"""Result previews preserve the official artifact and enforce its publication."""

import json
from pathlib import Path

import pytest

from lightrag.integrations.data_workflow.results import artifact, preview
from lightrag.integrations.data_workflow.storage import JobConflict

pytestmark = pytest.mark.offline


@pytest.fixture
def result(tmp_path):
    run = tmp_path / "outputs" / "run-test"
    run.mkdir(parents=True)
    source = {
        "schema_version": 3,
        "run_id": run.name,
        "workflow_contract": "difficulty_routed_qa.v5",
        "source_id": "source-test",
        "work_items": [
            {
                "id": str(i),
                "outcome": "processed",
                "versions": [
                    {
                        "id": "old",
                        "qa": {"generated_question": "old", "generated_answer": "old"},
                    },
                    {
                        "id": f"v-{i}",
                        "qa": {
                            "generated_question": f"Q{i}",
                            "generated_answer": f"A{i}",
                        },
                    },
                ],
            }
            for i in range(4)
        ]
        + [{"outcome": "duplicate"}],
    }
    review = run / "review.jsonl"
    review.write_text(json.dumps(source) + "\n")
    manifest = {
        "schema_version": 4,
        "review_schema_version": 3,
        "run_id": run.name,
        "status": "completed",
        "artifacts": {
            "review.jsonl": {"status": "committed", "bytes": review.stat().st_size}
        },
        "checkpoint": {"offset": review.stat().st_size},
    }
    (run / "run.json").write_text(json.dumps(manifest))
    (tmp_path / "stderr.log").write_text("完整日志")
    record = {
        "status": "completed",
        "directory": str(tmp_path),
        "result": {"run_id": run.name, "directory": str(run), "status": "completed"},
    }
    return record, review, manifest, source


def publish(review, manifest, source):
    review.write_text(json.dumps(source) + "\n")
    manifest["artifacts"]["review.jsonl"]["bytes"] = review.stat().st_size
    manifest["checkpoint"]["offset"] = review.stat().st_size
    (review.parent / "run.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "contract", ["difficulty_routed_qa.v5", "difficulty_routed_qa.v6"]
)
def test_preview_final_versions_and_pagination_preserve_original_file(result, contract):
    record, review, manifest, source = result
    source["workflow_contract"] = contract
    publish(review, manifest, source)
    original = review.read_bytes()
    first = preview(record, 0, 2)
    assert [x["question"] for x in first["items"]] == ["Q0", "Q1"]
    assert first["has_more"] and first["next_offset"] == 2
    second = preview(record, 2, 2)
    assert [x["answer"] for x in second["items"]] == ["A2", "A3"]
    assert not second["has_more"]
    assert artifact(record, "review").read_bytes() == original
    assert artifact(record, "stderr").read_text() == "完整日志"


@pytest.mark.parametrize("status", ["running", "ready", "failed", "interrupted"])
def test_unfinished_or_failed_results_are_not_downloaded(result, status):
    record, *_ = result
    record["status"] = status
    with pytest.raises(JobConflict):
        preview(record)


def test_changed_publication_and_foreign_identity_are_rejected(result):
    record, review, manifest, source = result
    review.write_text("modified")
    with pytest.raises(JobConflict):
        artifact(record, "review")
    source["run_id"] = "another-run"
    publish(review, manifest, source)
    with pytest.raises(ValueError, match="foreign"):
        preview(record)


def test_symlink_and_foreign_result_directory_are_rejected(result, tmp_path):
    record, review, *_ = result
    outside = tmp_path / "elsewhere"
    outside.write_text(review.read_text())
    review.unlink()
    review.symlink_to(outside)
    with pytest.raises(ValueError):
        artifact(record, "review")
    record["result"]["directory"] = str(tmp_path / "foreign" / "run")
    with pytest.raises(ValueError):
        artifact(record, "review")


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        {"work_items": None},
        {"work_items": [None]},
        {"work_items": [{"outcome": "processed", "versions": [None]}]},
    ],
)
def test_malformed_current_review_fails_explicitly(result, malformed):
    record, review, manifest, source = result
    if isinstance(malformed, dict):
        source.update(malformed)
    else:
        source = malformed
    publish(review, manifest, source)
    with pytest.raises(ValueError):
        preview(record)


def test_service_uses_workspace_store_for_preview_and_download(bridge, monkeypatch):
    service, *_ = bridge
    monkeypatch.setattr(service, "_spawn", lambda *args: None)
    own = service.submit("result-scope", start=False)
    with pytest.raises(KeyError):
        service.result_preview("foreign-job", 0, 20)
    with pytest.raises(KeyError):
        service.artifact_file("foreign-job", "stderr")
    # A reset log cursor starts again from byte zero after a shortened file.
    Path(own["artifacts"]["stderr"]).write_text("重新开始")
    value = service.logs(own["job_id"], "stderr", 500, 8192)
    assert value["offset"] == 0 and value["text"] == "重新开始"
