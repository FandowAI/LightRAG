"""Server imports retain native ingress and never traverse outside selected roots."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

with patch.object(sys, "argv", ["lightrag-server"]):
    from lightrag.api.routers.server_import_routes import create_server_import_routes
    from lightrag.api import utils_api
from lightrag.api.server_files import ENTRIES_ENV, FILES_ENV, ROOTS_ENV

pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def auth_mode(monkeypatch):
    monkeypatch.setattr(utils_api, "auth_configured", False)
    monkeypatch.delenv(FILES_ENV, raising=False)
    monkeypatch.delenv(ENTRIES_ENV, raising=False)


@pytest.fixture
async def server(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    source.mkdir()
    monkeypatch.setenv(ROOTS_ENV, json.dumps([str(source)]))
    received = []

    async def upload(**kwargs):
        received.append(
            {
                "content": await kwargs["file"].read(),
                "filename": kwargs["file"].filename,
                "relative": kwargs["relative_path"],
                "tasks": kwargs["managed_tasks"],
            }
        )
        return {"status": "success", "track_id": "upload-test", "message": "queued"}

    tasks = set()
    app = FastAPI()
    app.include_router(
        create_server_import_routes(
            SimpleNamespace(
                is_supported_file=lambda name: name.endswith((".md", ".json", ".txt"))
            ),
            upload,
            lambda: 1024,
            lambda: tasks,
        ),
        prefix="/documents",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield (client, source, received)


async def preview(client, path, recursive=True):
    return await client.post(
        "/documents/server-import/preview",
        json={"path": str(path), "recursive": recursive},
    )


async def test_browse_preview_and_copy_preserve_originals_and_nested_names(server):
    client, source, received = server
    for name in ["a", "b"]:
        (source / name).mkdir()
        (source / name / "notes.md").write_text(name)
    (source / "image.bin").write_bytes(b"ignored")
    (source / "large.md").write_bytes(b"x" * 1025)
    (source / ".private").mkdir()
    (source / ".private/hidden.md").write_text("hidden")
    (source / "__parsed__").mkdir()
    (source / "__parsed__/old.md").write_text("archived")
    response = await client.post(
        "/documents/server-import/browse", json={"path": str(source)}
    )
    assert response.status_code == 200
    assert response.json()["parent"] is None
    assert [row["name"] for row in response.json()["directories"]] == ["a", "b"]
    result = (await preview(client, source)).json()
    assert result["skipped"] == 4
    assert [row["relative_path"] for row in result["files"]] == [
        "a/notes.md",
        "b/notes.md",
    ]
    for row in result["files"]:
        response = await client.post(
            "/documents/server-import/file",
            json={"path": row["path"], "version": row["version"]},
        )
        assert response.status_code == 200
        assert response.json()["track_id"] == "upload-test"
    assert [row["content"] for row in received] == [b"a", b"b"]
    assert len({row["relative"] for row in received}) == 2
    assert (source / "a/notes.md").read_text() == "a"
    assert (await preview(client, source, recursive=False)).json()["files"] == []


async def test_changed_source_is_not_submitted(server):
    client, source, received = server
    document = source / "notes.md"
    document.write_text("before")
    row = (await preview(client, source)).json()["files"][0]
    document.write_text("after preview with different length")
    response = await client.post(
        "/documents/server-import/file",
        json={"path": row["path"], "version": row["version"]},
    )
    assert response.status_code == 409
    assert received == []


async def test_roots_and_parent_or_symlink_escape_are_rejected(server, tmp_path):
    client, source, received = server
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside")
    (source / "link").symlink_to(outside, target_is_directory=True)
    (source / "file.md").symlink_to(outside / "secret.md")
    for path in [outside, str(source) + "/../outside", source / "link"]:
        assert (await preview(client, path)).status_code in {400, 403}
    assert (await preview(client, source)).json()["files"] == []
    for path in [source / "link/secret.md", source / "file.md"]:
        assert (
            await client.post(
                "/documents/server-import/file",
                json={"path": str(path), "version": "0" * 64},
            )
        ).status_code == 403
    assert received == []


async def test_parent_swap_after_preview_cannot_read_external_file(server, tmp_path):
    client, source, received = server
    folder = source / "folder"
    folder.mkdir()
    (folder / "notes.md").write_text("inside")
    row = (await preview(client, source)).json()["files"][0]
    (folder / "notes.md").unlink()
    folder.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("outside")
    folder.symlink_to(outside, target_is_directory=True)
    assert (
        await client.post(
            "/documents/server-import/file",
            json={"path": row["path"], "version": row["version"]},
        )
    ).status_code == 403
    assert received == []


async def test_configured_root_cannot_be_replaced_by_a_symlink(server, tmp_path):
    client, source, received = server
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("outside")
    source.rmdir()
    source.symlink_to(outside, target_is_directory=True)
    assert (await preview(client, source)).status_code == 403
    assert received == []


async def test_preview_limits_fail_without_silently_delivering_a_subset(
    server, monkeypatch
):
    client, source, received = server
    for name in ["a.md", "b.md"]:
        (source / name).write_text("content")
    monkeypatch.setattr("lightrag.api.server_files.MAX_FILES", 1)
    response = await preview(client, source)
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "server_import_files_limit",
        "message": "Directory exceeds the 1 files preview limit; filter file types, disable recursion or choose a subdirectory",
        "limit": 1,
        "observed": 2,
        "configuration_variable": FILES_ENV,
    }
    assert received == []


async def test_file_filter_avoids_counting_unwanted_supported_files(
    server, monkeypatch
):
    client, source, _ = server
    (source / "document.md").write_text("policy")
    (source / "cache.json").write_text("{}")
    monkeypatch.setattr("lightrag.api.server_files.MAX_FILES", 1)
    assert (await preview(client, source)).status_code == 422
    response = await client.post(
        "/documents/server-import/preview",
        json={"path": str(source), "extensions": [".MD"]},
    )
    assert response.status_code == 200
    assert [row["relative_path"] for row in response.json()["files"]] == ["document.md"]
    assert response.json()["excluded"][0]["reason"] == "extension_filtered"


async def test_configured_preview_limits_are_applied_and_reported(server, monkeypatch):
    client, source, _ = server
    for name in ["a.md", "b.md"]:
        (source / name).write_text("content")
    monkeypatch.setattr("lightrag.api.server_files.MAX_FILES", 1)
    monkeypatch.setenv(FILES_ENV, "2")
    monkeypatch.setenv(ENTRIES_ENV, "3")
    assert len((await preview(client, source)).json()["files"]) == 2
    assert (await client.get("/documents/server-import/config")).json()["limits"] == {
        "max_files": 2,
        "max_entries": 3,
    }
    monkeypatch.setenv(ENTRIES_ENV, "1")
    response = await preview(client, source)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "server_import_entries_limit"
    assert response.json()["detail"]["configuration_variable"] == ENTRIES_ENV
    monkeypatch.setenv(FILES_ENV, "0")
    assert (await client.get("/documents/server-import/config")).status_code == 503


@pytest.mark.parametrize("extensions", [[], ["../md"], ["md"], [1]])
async def test_invalid_filters_cannot_change_path_or_format_policy(server, extensions):
    client, source, _ = server
    response = await client.post(
        "/documents/server-import/preview",
        json={"path": str(source), "extensions": extensions},
    )
    assert response.status_code == 422


async def test_default_is_disabled_and_bad_configuration_is_explicit(
    server, monkeypatch
):
    client, source, _ = server
    monkeypatch.delenv(ROOTS_ENV)
    assert (await client.get("/documents/server-import/config")).json()[
        "enabled"
    ] is False
    assert (await preview(client, source)).status_code == 403
    monkeypatch.setenv(ROOTS_ENV, "not-json")
    assert (await client.get("/documents/server-import/config")).status_code == 503


async def test_server_files_cannot_be_read_through_auth_whitelist(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(utils_api, "path_is_whitelisted", lambda scope: True)
    monkeypatch.setenv(ROOTS_ENV, json.dumps([str(tmp_path)]))
    app = FastAPI()
    app.include_router(
        create_server_import_routes(
            SimpleNamespace(is_supported_file=lambda name: True),
            AsyncMock(),
            lambda: 1024,
            lambda: set(),
            api_key="private-test-key",
        ),
        prefix="/documents",
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    assert (await client.get("/documents/server-import/config")).status_code in {
        401,
        403,
    }
    assert (
        await client.get(
            "/documents/server-import/config", headers={"X-API-Key": "private-test-key"}
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_actual_upload_still_owns_admission_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["lightrag-server"])
    from lightrag.api.routers import document_routes as routes
    from lightrag.api.server_files import ServerFiles
    from lightrag.folder_upload import source_paths
    import lightrag.kg.shared_storage as shared

    source = tmp_path / "source"
    source.mkdir()
    original = source / "report.[native].md"
    original.write_text("native document")
    monkeypatch.setenv(ROOTS_ENV, json.dumps([str(source)]))
    reserve = AsyncMock()
    monkeypatch.setattr(routes, "_reserve_enqueue_slot", reserve)
    monkeypatch.setattr(routes, "_release_enqueue_slot", AsyncMock())
    monkeypatch.setattr(
        routes, "get_existing_doc_by_file_path_candidates", AsyncMock(return_value=None)
    )
    background = AsyncMock()
    monkeypatch.setattr(shared, "start_reserved_background_task", background)
    rag = SimpleNamespace(
        working_dir=str(tmp_path / "storage"), workspace="test", doc_status=object()
    )
    manager = routes.DocumentManager(str(tmp_path / "input"), workspace="test")
    router = routes.create_document_routes(rag, manager)
    endpoint = next(
        (
            row.endpoint
            for row in router.routes
            if row.path == "/documents/server-import/file"
        )
    )
    from lightrag.api.routers.server_import_routes import ImportRequest

    row = ServerFiles().preview(str(source), True, manager.is_supported_file, 1024)[
        "files"
    ][0]
    result = await endpoint(
        ImportRequest(path=row["path"], version=row["version"]),
        request=None,
        managed_tasks=set(),
    )
    assert result.status == "success"
    assert reserve.await_count == background.await_count == 1
    assert original.read_text() == "native document"
    files = list(manager.input_dir.glob("*.md"))
    assert len(files) == 1 and files[0].read_text() == original.read_text()
    assert len(source_paths(rag.working_dir, rag.workspace)) == 1
