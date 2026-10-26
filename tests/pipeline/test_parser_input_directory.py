"""Parser inputs follow an instance's CLI directory without crossing test roots."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.parser.base import ParseContext
from lightrag.parser.registry import get_parser
from lightrag.pipeline import _PipelineMixin

pytestmark = pytest.mark.offline


def _rag(input_dir, workspace="local_test"):
    rag = SimpleNamespace(
        input_dir=str(input_dir) if input_dir is not None else None,
        workspace=workspace,
        _persist_parsed_full_docs=AsyncMock(),
    )
    rag._resolve_source_file_for_parser = lambda *args, **kwargs: (
        _PipelineMixin._resolve_source_file_for_parser(rag, *args, **kwargs)
    )
    return rag


@pytest.mark.parametrize("workspace", ["", "local_test"])
def test_instance_input_directory_overrides_environment(
    tmp_path, monkeypatch, workspace
):
    original = tmp_path / "original"
    local = tmp_path / "local"
    for root in (original, local):
        folder = root / workspace if workspace else root
        folder.mkdir(parents=True)
        (folder / "考核模版.md").write_text(root.name)
    monkeypatch.setenv("INPUT_DIR", str(original))
    rag = _rag(local, workspace)

    resolved = rag._resolve_source_file_for_parser(
        "考核模版.md", parser_engine="native"
    )

    assert Path(resolved) == local / workspace / "考核模版.md"
    assert Path(resolved).read_text() == "local"
    assert os.environ["INPUT_DIR"] == str(original)


def test_explicit_input_directory_does_not_fall_back_to_other_sources(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    original = tmp_path / "inputs" / "local_test"
    original.mkdir(parents=True)
    (original / "same.md").write_text("Original source must not be consumed")
    (tmp_path / "same.md").write_text("Unrelated cwd file")
    local = tmp_path / "isolated"
    local.mkdir()
    # A file from the unnamed workspace must not be borrowed either.
    (local / "same.md").write_text("Another workspace")
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "inputs"))

    resolved = _rag(local)._resolve_source_file_for_parser(
        "same.md", parser_engine="native"
    )

    assert Path(resolved) == local / "local_test" / "same.md"
    assert not Path(resolved).exists()


def test_sdk_without_instance_input_directory_keeps_environment_fallback(
    tmp_path, monkeypatch
):
    folder = tmp_path / "inputs" / "local_test"
    folder.mkdir(parents=True)
    source = folder / "source.md"
    source.write_text("content")
    monkeypatch.setenv("INPUT_DIR", str(folder.parent))
    assert Path(_rag(None)._resolve_source_file_for_parser(source.name)) == source


def test_two_instances_resolve_their_own_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "unrelated"))
    for name in ("one", "two"):
        folder = tmp_path / name / "local_test"
        folder.mkdir(parents=True)
        (folder / "source.md").write_text(name)
    one, two = (_rag(tmp_path / name) for name in ("one", "two"))
    assert Path(one._resolve_source_file_for_parser("source.md")).read_text() == "one"
    assert Path(two._resolve_source_file_for_parser("source.md")).read_text() == "two"


def test_native_markdown_parses_cli_upload_and_archives_only_that_copy(
    tmp_path, monkeypatch
):
    original = tmp_path / "original"
    original.mkdir()
    (original / "source.md").write_text("original content")
    monkeypatch.setenv("INPUT_DIR", str(original))
    local = tmp_path / "local" / "local_test"
    local.mkdir(parents=True)
    source = local / "source.md"
    source.write_text("# Test\n\nLocal original text.\n")
    rag = _rag(local.parent)

    result = asyncio.run(
        get_parser("native").parse(
            ParseContext(
                rag,
                "doc-input-test",
                source.name,
                {
                    "parse_format": "pending_parse",
                    "parse_engine": "native",
                    "content": "",
                },
            )
        )
    )

    assert "Local original text." in result.content
    assert "original content" not in result.content
    assert Path(result.blocks_path).is_relative_to(local)
    assert Path(result.blocks_path).is_file()
    assert (original / "source.md").read_text() == "original content"
    assert not source.exists()
    rag._persist_parsed_full_docs.assert_awaited_once()
