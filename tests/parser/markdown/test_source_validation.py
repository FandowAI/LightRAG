"""A missing Markdown source is distinguished from an unsupported extension."""

import pytest

from lightrag.parser.markdown.parser import NativeMarkdownParser

pytestmark = pytest.mark.offline


def test_missing_markdown_reports_source_path(tmp_path):
    source = tmp_path / "missing.md"
    with pytest.raises(FileNotFoundError, match="source file not found") as error:
        NativeMarkdownParser().validate_source(source, source.name)
    assert str(source) in str(error.value)


def test_existing_unsupported_source_keeps_extension_error(tmp_path):
    source = tmp_path / "unsupported.txt"
    source.write_text("text")
    with pytest.raises(ValueError, match="does not support pending file"):
        NativeMarkdownParser().validate_source(source, source.name)
