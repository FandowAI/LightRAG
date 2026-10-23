"""Folder uploads use the authenticated native ingress, with durable provenance."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException, UploadFile
import pytest

from lightrag.folder_upload import folder_source_name, source_paths

pytestmark = pytest.mark.offline


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["lightrag-server"])
    from lightrag.api.routers import document_routes as routes
    import lightrag.kg.shared_storage as shared

    monkeypatch.setattr(routes, "_reserve_enqueue_slot", AsyncMock())
    monkeypatch.setattr(routes, "_release_enqueue_slot", AsyncMock())
    monkeypatch.setattr(
        routes, "get_existing_doc_by_file_path_candidates", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(shared, "start_reserved_background_task", AsyncMock())
    rag = SimpleNamespace(
        working_dir=str(tmp_path / "storage"),
        workspace="enterprise",
        doc_status=object(),
    )
    manager = routes.DocumentManager(str(tmp_path / "input"))
    router = routes.create_document_routes(rag, manager)
    upload = next(r.endpoint for r in router.routes if r.path == "/documents/upload")
    return upload, rag, manager


@pytest.mark.asyncio
async def test_nested_same_name_files_are_distinct_and_paths_survive_restart(endpoint):
    upload, rag, manager = endpoint
    for relative in ["root/a/notes.md", "root/b/notes.md"]:
        result = await upload(
            managed_tasks=set(),
            file=UploadFile(filename="notes.md", file=BytesIO(relative.encode())),
            relative_path=relative,
        )
        assert result.status == "success"
    files = list(manager.input_dir.glob("*.md"))
    assert len(files) == 2 and {p.read_text() for p in files} == {
        "root/a/notes.md",
        "root/b/notes.md",
    }
    mapping = source_paths(rag.working_dir, rag.workspace)
    assert set(mapping.values()) == {"root/a/notes.md", "root/b/notes.md"}
    assert source_paths(rag.working_dir, "other") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../notes.md",
        "/root/notes.md",
        "root/../notes.md",
        "C:/notes.md",
        "root/different.pdf",
    ],
)
async def test_bad_folder_path_cannot_write_a_file(endpoint, path):
    upload, _, manager = endpoint
    with pytest.raises(HTTPException) as error:
        await upload(
            managed_tasks=set(),
            file=UploadFile(filename="notes.md", file=BytesIO(b"test")),
            relative_path=path,
        )
    assert error.value.status_code == 400
    assert not list(manager.input_dir.iterdir())


def test_long_unicode_names_stay_below_filesystem_byte_limit():
    filename = "企业经营方法" * 30 + ".md"
    basename, normalized = folder_source_name("资料/" + filename, filename)
    assert len(basename.encode("utf-8")) < 255
    assert normalized == "资料/" + filename


def test_folder_names_preserve_parser_hints():
    from lightrag.parser.routing import resolve_parser_directives

    name, _ = folder_source_name("root/report.[native].md", "report.[native].md")
    assert (
        resolve_parser_directives(name).engine
        == resolve_parser_directives("report.[native].md").engine
    )
