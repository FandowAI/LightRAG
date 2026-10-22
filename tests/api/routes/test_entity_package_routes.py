from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

pytestmark = pytest.mark.offline


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lightrag-server"])
    from lightrag.api.routers import query_routes
    import lightrag.entity_packages as packages

    async def authenticated():
        return None

    monkeypatch.setattr(
        query_routes, "get_combined_auth_dependency", lambda key: authenticated
    )
    build = AsyncMock(
        return_value={"status": "success", "packages": [], "summary": {"packages": 0}}
    )
    monkeypatch.setattr(packages, "from_rag", build)
    app = FastAPI()
    app.include_router(query_routes.create_query_routes(object()))
    return TestClient(app), build


def test_defaults_match_agreed_package_policy(client):
    http, build = client
    response = http.post("/query/entity-packages", json={"entity_name": " 产品供应部 "})
    assert response.status_code == 200
    _, name, settings = build.call_args.args
    assert name == "产品供应部"
    assert (settings.graph_depth, settings.rag_top_k) == (2, 10)
    assert not hasattr(settings, "max_entities")
    assert not hasattr(settings, "max_tokens")
    assert settings.near_dedup is True


@pytest.mark.parametrize(
    "body",
    [
        {"entity_name": " "},
        {"entity_name": "a", "graph_depth": 3, "max_entities": 2},
        {"entity_name": "a", "max_tokens": 12000},
        {"entity_name": "a", "rag_top_k": 10001},
        {"entity_name": "a", "near_threshold": 0.1},
    ],
)
def test_invalid_requests_do_not_start_retrieval(client, body):
    http, build = client
    assert http.post("/query/entity-packages", json=body).status_code == 422
    build.assert_not_awaited()


def test_core_center_defaults(client, monkeypatch):
    import lightrag.entity_centers as centers

    build = AsyncMock(return_value={"candidates": [], "summary": {}})
    monkeypatch.setattr(centers, "from_rag", build)
    http, _ = client
    assert http.post("/query/entity-centers", json={}).status_code == 200
    settings = build.call_args.args[1]
    assert (settings.min_neighbors, settings.min_documents) == (3, 2)


@pytest.mark.parametrize(
    "body", [{"min_neighbors": 0}, {"min_documents": 0}, {"score": 100}]
)
def test_invalid_center_thresholds_do_not_scan_graph(client, monkeypatch, body):
    import lightrag.entity_centers as centers

    build = AsyncMock()
    monkeypatch.setattr(centers, "from_rag", build)
    http, _ = client
    assert http.post("/query/entity-centers", json=body).status_code == 422
    build.assert_not_awaited()
