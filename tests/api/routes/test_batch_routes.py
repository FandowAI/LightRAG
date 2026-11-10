"""Batch route validation, status, export and explicit integration boundary."""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

pytestmark = pytest.mark.offline


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["lightrag-server"])
    from lightrag.api.routers import batch_routes

    async def authenticated():
        pass

    monkeypatch.setattr(
        batch_routes, "get_combined_auth_dependency", lambda key: authenticated
    )
    folder = tmp_path / "batch-id"
    folder.mkdir()
    for name, content in [
        ("packages.jsonl", '{"text":"complete"}\n'),
        ("sources.jsonl", '{"chunk_id":"source"}\n'),
        ("manifest.json", "{}"),
    ]:
        (folder / name).write_bytes(content.encode("utf-8"))
    job = {
        "id": "batch-id",
        "status": "completed",
        "package_count": 1,
        "export_path": str(folder / "packages.jsonl"),
    }

    class Jobs:
        root = tmp_path
        start = AsyncMock(return_value=job)

        def get(self, job_id):
            if job_id != "batch-id":
                raise KeyError(job_id)
            return job

        def cancel(self, job_id):
            return job

        def jsonl(self, job_id):
            return iter(['{"text":"complete"}\n'])

    jobs = Jobs()
    monkeypatch.setattr(batch_routes, "BatchManager", lambda rag: jobs)
    app = FastAPI()
    app.include_router(batch_routes.create_batch_routes(object()))
    return TestClient(app), jobs


def test_explicit_start_freezes_unique_centers_and_retrieval_settings(client):
    http, jobs = client
    response = http.post("/batch-retrieval/jobs", json={"centers": [" a ", "a", "b"]})
    assert response.status_code == 202
    centers, settings = jobs.start.call_args.args
    assert centers == ["a", "b"]
    assert (
        settings.graph_depth == 2 and settings.rag_top_k == 10 and settings.near_dedup
    )


@pytest.mark.parametrize("count", [800, 1500, 1501, 2000])
def test_all_selected_centers_reach_the_manager_without_truncation(client, count):
    http, jobs = client
    centers = [f"Entity {index}" for index in range(count)]
    response = http.post(
        "/batch-retrieval/jobs",
        json={"centers": centers, "graph_depth": 1, "rag_top_k": 10},
    )
    assert response.status_code == 202
    jobs.start.assert_awaited_once()
    received, settings = jobs.start.call_args.args
    assert received == centers
    assert settings.graph_depth == 1 and settings.rag_top_k == 10


def test_api_schema_has_no_fixed_batch_size_limit(client):
    http, _ = client
    schema = http.get("/openapi.json").json()
    field = schema["components"]["schemas"]["BatchRequest"]["properties"]["centers"]
    assert field["minItems"] == 1
    assert "maxItems" not in field


@pytest.mark.parametrize(
    "body",
    [
        {"centers": []},
        {"centers": [" "]},
        {"centers": ["x" * 513]},
        {"centers": ["a"], "graph_depth": 7},
        {"centers": ["a"], "rag_top_k": 101},
    ],
)
def test_invalid_starts_do_not_create_a_job(client, body):
    http, jobs = client
    assert http.post("/batch-retrieval/jobs", json=body).status_code == 422
    jobs.start.assert_not_awaited()


def test_status_export_and_retired_handoff_are_truthful(client):
    http, jobs = client
    assert http.get("/batch-retrieval/config").status_code == 404
    assert http.get("/batch-retrieval/jobs/batch-id").status_code == 200
    assert http.get("/batch-retrieval/jobs/missing").status_code == 404
    exported = http.get("/batch-retrieval/jobs/batch-id/packages.jsonl")
    assert exported.status_code == 200 and exported.text == '{"text":"complete"}\n'
    assert http.post("/batch-retrieval/jobs/batch-id/distill").status_code == 404


def test_all_artifacts_are_downloadable_and_incomplete_exports_are_refused(client):
    http, jobs = client
    assert (
        http.get("/batch-retrieval/jobs/batch-id/sources.jsonl").text
        == '{"chunk_id":"source"}\n'
    )
    assert http.get("/batch-retrieval/jobs/batch-id/manifest.json").status_code == 200
    (jobs.root / "batch-id" / "manifest.json").unlink()
    assert http.get("/batch-retrieval/jobs/batch-id/packages.jsonl").status_code == 409


def test_package_preview_pages_published_evidence_without_truncating_export(client):
    import json

    http, jobs = client
    path = jobs.root / "batch-id" / "packages.jsonl"
    rows = [
        {
            "schema_version": "enterprise-evidence/1.0",
            "batch_id": "batch-id",
            "workspace": "",
            "entities": [],
            "relationships": [],
            "original_texts": [{"text": str(i)}],
        }
        for i in range(12)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = path.read_bytes()
    first = http.get("/batch-retrieval/jobs/batch-id/preview?limit=10").json()
    assert len(first["items"]) == 10 and first["has_more"]
    second = http.get(
        "/batch-retrieval/jobs/batch-id/preview?offset=10&limit=10"
    ).json()
    assert len(second["items"]) == 2 and not second["has_more"]
    assert path.read_bytes() == before
    assert http.get("/batch-retrieval/jobs/foreign/preview").status_code == 404
    rows[0]["workspace"] = "foreign"
    path.write_text(json.dumps(rows[0]) + "\n")
    assert http.get("/batch-retrieval/jobs/batch-id/preview").status_code == 422
