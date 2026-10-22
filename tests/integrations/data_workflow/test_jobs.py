import concurrent.futures
import json
import time

import pytest
import yaml

from lightrag.integrations.data_workflow.config import read_yaml
from lightrag.integrations.data_workflow.progress import public_job
from lightrag.integrations.data_workflow.service import DataWorkflowService
from lightrag.integrations.data_workflow.storage import JobConflict, JobStore
from lightrag.integrations.data_workflow.worker import run

pytestmark = pytest.mark.offline


def synchronous(monkeypatch, service):
    monkeypatch.setattr(
        service,
        "_spawn",
        lambda store, record, operation: run(store, record["job_id"], operation),
    )


def test_prepare_only_writes_text_only_jsonl_and_reports_rejections(
    bridge, query_result, monkeypatch
):
    service, _, source, _ = bridge
    source.write_text(json.dumps([query_result, {"status": "failure"}, query_result]))
    synchronous(monkeypatch, service)
    job = service.submit("prepare-only", start=False)
    assert job["status"] == "ready" and job["can_start"]
    assert job["progress"]["accepted_sources"] == 2
    assert job["progress"]["rejected_sources"] == 1
    assert job["progress"]["percent"] is None
    from pathlib import Path

    records = [
        json.loads(line)
        for line in Path(job["artifacts"]["input"]).read_text().splitlines()
    ]
    assert len(records) == 2
    assert all(set(row) == {"text"} for row in records)
    assert set(json.loads(records[0]["text"])) == {
        "entities",
        "relationships",
        "text_chunks",
    }
    assert not Path(job["artifacts"]["stdout"]).exists()


@pytest.mark.parametrize("status", ["completed", "completed_with_errors", "failed"])
def test_runs_existing_cli_and_preserves_terminal_status(bridge, monkeypatch, status):
    service, _, _, recipe = bridge
    doc = read_yaml(recipe)
    doc["workflow"]["parameters"]["test_status"] = status
    recipe.write_text(yaml.safe_dump(doc))
    synchronous(monkeypatch, service)
    job = service.submit("terminal-receipt")
    assert job["status"] == status
    assert job["result"]["status"] == status
    assert job["terminal"] and job["poll_after_ms"] == 0
    assert job["progress"]["processed_sources"] == 1
    assert job["result"]["summary"]["work_items"] == 3
    assert job["progress"]["percent"] == 100
    assert service.logs(job["job_id"], "stdout", 0, 65536)["text"]


def test_duplicate_submit_and_start_cannot_execute_twice(bridge, monkeypatch):
    service, _, source, _ = bridge
    calls = []

    def spawn(store, record, operation):
        calls.append(operation)
        run(store, record["job_id"], operation)

    monkeypatch.setattr(service, "_spawn", spawn)
    first = service.submit("same-click", start=False)
    source.write_text("source changed after preparation")
    second = service.submit("same-click", start=False)
    assert first["job_id"] == second["job_id"]
    assert calls == ["prepare"]
    started = service.start(first["job_id"])
    assert started["status"] == "completed"
    assert service.submit("same-click")["job_id"] == first["job_id"]
    assert calls == ["prepare", "run"]
    with pytest.raises(JobConflict):
        service.start(first["job_id"])


def test_no_valid_source_fails_before_cli(bridge, monkeypatch):
    service, _, source, _ = bridge
    source.write_text('{"status":"success","data":{}}')
    synchronous(monkeypatch, service)
    job = service.submit("empty-source")
    assert job["status"] == "failed" and "No valid" in job["error"]
    assert job["result"] is None


def test_unconfigured_sources_fail_with_actionable_error(bridge, monkeypatch):
    service, config, _, _ = bridge
    doc = read_yaml(config)
    doc["input"]["paths"] = []
    config.write_text(yaml.safe_dump(doc))
    synchronous(monkeypatch, service)
    job = service.submit("missing-sources")
    assert job["status"] == "failed" and "input.paths" in job["error"]


def test_placeholder_models_are_not_called(bridge, monkeypatch):
    service, _, _, recipe = bridge
    doc = read_yaml(recipe)
    doc["models"] = {
        "generation": {
            "api_url": "https://api.example.com/v1/chat/completions",
            "model_name": "replace-with-generation-model",
        }
    }
    recipe.write_text(yaml.safe_dump(doc))
    synchronous(monkeypatch, service)
    job = service.submit("placeholder-model")
    assert job["status"] == "failed" and "real model" in job["error"]
    assert job["result"] is None


def test_worker_timeout_terminates_target(bridge, monkeypatch):
    service, config, _, recipe = bridge
    doc = read_yaml(recipe)
    doc["workflow"]["parameters"]["delay"] = 0.3
    recipe.write_text(yaml.safe_dump(doc))
    doc = read_yaml(config)
    doc["limits"] = {"timeout_seconds": 0.02}
    config.write_text(yaml.safe_dump(doc))
    synchronous(monkeypatch, service)
    job = service.submit("timeout")
    assert job["status"] == "failed" and "timed out" in job["error"]


def test_detached_job_survives_service_recreation(bridge):
    service, config, _, _ = bridge
    job = service.submit("background-job")
    restored = DataWorkflowService("test-workspace", config)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = restored.get(job["job_id"])
        if current["terminal"]:
            break
        time.sleep(0.05)
    assert current["status"] == "completed", current
    assert restored.list(10, 0)["total"] == 1


def test_workspaces_have_independent_job_visibility(bridge, monkeypatch):
    service, config, _, _ = bridge
    synchronous(monkeypatch, service)
    job = service.submit("workspace-job", start=False)
    other = DataWorkflowService("other-workspace", config)
    with pytest.raises(KeyError):
        other.get(job["job_id"])
    assert other.list(10, 0)["total"] == 0


def test_duplicate_reservation_is_atomic_across_store_instances(tmp_path):
    path = tmp_path / "jobs.sqlite3"

    def reserve(number):
        return JobStore(path).reserve(
            {"job_id": f"job-{number}", "request_id": "same-id", "status": "preparing"},
            maximum=1,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(created for _, created in results) == 1
    assert results[0][0]["job_id"] == results[1][0]["job_id"]


def test_capacity_and_stale_worker_recovery(bridge, monkeypatch):
    service, _, _, _ = bridge
    monkeypatch.setattr(service, "_spawn", lambda *_: None)
    first = service.submit("first")
    with pytest.raises(JobConflict, match="limit"):
        service.submit("second")
    _, store, _ = service._resources()
    # A crashed worker reservation has no live owner and must never restart itself.
    with store.connection() as conn:
        record = json.loads(
            conn.execute(
                "SELECT record FROM jobs WHERE job_id=?", (first["job_id"],)
            ).fetchone()[0]
        )
        record["updated_at"] = "2000-01-01T00:00:00+00:00"
        store._write(conn, record)
    recovered = service.get(first["job_id"])
    assert recovered["status"] == "interrupted" and recovered["terminal"]
    assert service.submit("second")["status"] == "preparing"


def test_progress_counts_sources_not_qa_or_uncommitted_bytes(tmp_path):
    directory = tmp_path / "job"
    run_dir = directory / "outputs/run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "summary": {"sources": 2, "work_items": 20},
                "checkpoint": {"batch_id": 1},
            }
        )
    )
    result = public_job(
        {"directory": str(directory), "status": "running", "accepted_sources": 4}
    )
    assert result["progress"]["processed_sources"] == 2
    assert result["progress"]["percent"] == 50
    (run_dir / "run.json").write_text("incomplete")
    assert (
        public_job({"directory": str(directory), "status": "running"})["progress"][
            "percent"
        ]
        is None
    )


def test_log_cursor_does_not_split_chinese_characters(bridge, monkeypatch):
    from pathlib import Path

    service, _, _, _ = bridge
    synchronous(monkeypatch, service)
    job = service.submit("unicode-log", start=False)
    Path(job["artifacts"]["stderr"]).write_text("第一行\n第二行\n", encoding="utf-8")
    offset = 0
    pieces = []
    while True:
        response = service.logs(job["job_id"], "stderr", offset, 4)
        pieces.append(response["text"])
        assert response["next_offset"] > offset
        offset = response["next_offset"]
        if not response["has_more"]:
            break
    assert "".join(pieces) == "第一行\n第二行\n"
