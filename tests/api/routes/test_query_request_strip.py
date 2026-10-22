"""QueryRequest applies the weighted RAG minimum after stripping whitespace."""

import importlib
import sys

import pytest
from pydantic import ValidationError

_original_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
_qr = importlib.import_module("lightrag.api.routers.query_routes")
sys.argv = _original_argv

QueryRequest = _qr.QueryRequest
GraphQueryBatchRequest = _qr.GraphQueryBatchRequest

pytestmark = pytest.mark.offline


@pytest.mark.parametrize("query", ["   ", "\t\t\t", "  a  ", "ab ", " a ", "中"])
def test_query_request_rejects_queries_below_weighted_minimum(query):
    with pytest.raises(ValidationError):
        QueryRequest(query=query)


@pytest.mark.parametrize(
    "query, expected",
    [
        ("  abc  ", "abc"),
        ("中a", "中a"),
        ("中文", "中文"),
        ("ねこ", "ねこ"),
        ("한글", "한글"),
    ],
)
def test_query_request_accepts_english_equivalent_weight_three(query, expected):
    assert QueryRequest(query=query).query == expected


@pytest.mark.parametrize("query", ["a", "中", " a "])
def test_query_request_does_not_apply_rag_minimum_to_bypass(query):
    assert QueryRequest(query=query, mode="bypass").query == query.strip()


@pytest.mark.parametrize("mode", ["mix", "naive", "graph", "bypass"])
@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
def test_query_request_rejects_an_empty_query_in_every_mode(mode, query):
    """`bypass` is exempt from the RAG minimum, not from carrying a prompt.

    Dropping the field's `min_length=3` for the mode-aware minimum also dropped
    the only guard that kept an empty string out of the direct-LLM path.
    """
    with pytest.raises(ValidationError):
        QueryRequest(query=query, mode=mode)


def test_graph_mode_accepts_single_character_entity_and_depth():
    request = QueryRequest(query=" A ", mode="graph", graph_depth=2)

    assert request.query == "A"
    param = request.to_query_params(is_stream=True)
    assert param.mode == "graph"
    assert param.graph_depth == 2
    assert param.stream is True


def test_graph_batch_request_strips_entities_and_builds_graph_params():
    request = GraphQueryBatchRequest(
        entities=[" A ", "B"],
        graph_depth=2,
        deduplicate_relations=True,
    )

    assert request.entities == ["A", "B"]
    param = request.to_query_params()
    assert param.mode == "graph"
    assert param.graph_depth == 2
    assert param.only_need_context is True
    assert param.enable_rerank is False


@pytest.mark.parametrize("entities", [[], ["   "], ["A"] * 101])
def test_graph_batch_request_rejects_invalid_entity_lists(entities):
    with pytest.raises(ValidationError):
        GraphQueryBatchRequest(entities=entities)
