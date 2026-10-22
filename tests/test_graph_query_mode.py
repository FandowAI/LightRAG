from __future__ import annotations

import pytest

from lightrag.lightrag import LightRAG
from lightrag.operate import (
    _graph_chunks,
    _tracked_chunk_ids,
    _traverse_graph_from_entity,
    graph_query,
    graph_query_data,
)
from lightrag.base import QueryParam
from lightrag.utils import make_relation_chunk_key
from lightrag.utils import Tokenizer


class _Graph:
    def __init__(self):
        self.nodes = {
            "A": {"entity_type": "ROOT", "source_id": "chunk-a"},
            "B": {"entity_type": "NODE", "source_id": "chunk-b"},
            "C": {"entity_type": "NODE", "source_id": "chunk-c"},
            "D": {"entity_type": "NODE", "source_id": "chunk-d"},
        }
        self.edges = {
            ("A", "B"): {"description": "A-B", "source_id": "chunk-ab"},
            ("B", "C"): {"description": "B-C", "source_id": "chunk-bc"},
            ("B", "D"): {"description": "B-D", "source_id": "chunk-bd"},
        }

    async def get_node(self, node_id):
        return self.nodes.get(node_id)

    async def get_node_edges(self, node_id):
        if node_id not in self.nodes:
            return None
        return [edge for edge in self.edges if node_id in edge]

    async def get_edge(self, src_id, tgt_id):
        return self.edges.get(tuple(sorted((src_id, tgt_id))))


class _KV:
    def __init__(self, records, global_config=None):
        self.records = records
        self.global_config = global_config or {}

    async def get_by_ids(self, ids):
        return [self.records.get(item_id) for item_id in ids]


@pytest.mark.asyncio
async def test_graph_traversal_respects_hop_depth():
    graph = _Graph()

    depth_zero = await _traverse_graph_from_entity("A", 0, graph)
    assert depth_zero is not None
    assert list(depth_zero.nodes) == ["A"]
    assert depth_zero.relations == {}

    depth_one = await _traverse_graph_from_entity("A", 1, graph)
    assert depth_one is not None
    assert set(depth_one.nodes) == {"A", "B"}
    assert set(depth_one.relations) == {("A", "B")}

    depth_two = await _traverse_graph_from_entity("A", 2, graph)
    assert depth_two is not None
    assert set(depth_two.nodes) == {"A", "B", "C", "D"}
    assert set(depth_two.relations) == {
        ("A", "B"),
        ("B", "C"),
        ("B", "D"),
    }


@pytest.mark.asyncio
async def test_graph_traversal_returns_none_for_missing_exact_root():
    assert await _traverse_graph_from_entity("missing", 2, _Graph()) is None


@pytest.mark.asyncio
async def test_graph_traversal_relation_cap_does_not_leave_isolated_nodes():
    result = await _traverse_graph_from_entity(
        "A", 2, _Graph(), max_nodes=100, max_relations=1
    )

    assert result is not None
    assert result.is_truncated is True
    assert set(result.nodes) == {"A", "B"}
    assert set(result.relations) == {("A", "B")}


def test_authoritative_empty_tracking_row_does_not_fall_back():
    assert _tracked_chunk_ids({"chunk_ids": [], "count": 0}, "fallback") == []
    assert _tracked_chunk_ids(None, "fallback") == ["fallback"]


@pytest.mark.asyncio
async def test_graph_chunks_prefer_tracking_and_deduplicate_stably():
    traversal = await _traverse_graph_from_entity("A", 1, _Graph())
    assert traversal is not None
    entity_chunks = _KV(
        {
            "A": {"chunk_ids": ["tracked-a", "shared"]},
            "B": {"chunk_ids": []},
        }
    )
    relation_chunks = _KV(
        {
            make_relation_chunk_key("A", "B"): {
                "chunk_ids": ["shared", "tracked-ab"]
            }
        }
    )
    text_chunks = _KV(
        {
            "tracked-a": {"content": "A"},
            "shared": {"content": "shared"},
            "tracked-ab": {"content": "A-B"},
        }
    )

    chunks = await _graph_chunks(
        traversal,
        text_chunks,
        entity_chunks,
        relation_chunks,
    )

    assert [chunk["chunk_id"] for chunk in chunks] == [
        "tracked-a",
        "shared",
        "tracked-ab",
    ]


class _CharacterTokenizer:
    def encode(self, content):
        return list(content)

    def decode(self, tokens):
        return "".join(tokens)


@pytest.mark.asyncio
async def test_graph_query_returns_query_data_compatible_payload():
    tokenizer = Tokenizer("test", _CharacterTokenizer())
    global_config = {
        "tokenizer": tokenizer,
        "user_prompt_prefix": "",
        "enable_content_headings": False,
    }
    text_chunks = _KV(
        {
            "chunk-a": {"content": "root evidence", "file_path": "a.txt"},
            "chunk-b": {"content": "neighbor evidence", "file_path": "b.txt"},
            "chunk-ab": {"content": "relation evidence", "file_path": "ab.txt"},
        },
        global_config,
    )

    result = await graph_query(
        "A",
        _Graph(),
        text_chunks,
        _KV({}),
        _KV({}),
        QueryParam(
            mode="graph",
            graph_depth=1,
            only_need_context=True,
            enable_rerank=False,
            max_entity_tokens=10_000,
            max_relation_tokens=10_000,
            max_total_tokens=20_000,
        ),
        global_config,
    )

    assert result is not None
    assert result.llm_generated is False
    assert result.raw_data["metadata"]["query_mode"] == "graph"
    assert result.raw_data["metadata"]["graph_depth"] == 1
    assert {item["entity_name"] for item in result.raw_data["data"]["entities"]} == {
        "A",
        "B",
    }
    assert len(result.raw_data["data"]["relationships"]) == 1
    assert len(result.raw_data["data"]["chunks"]) == 3


@pytest.mark.asyncio
async def test_graph_query_data_returns_complete_bounded_subgraph_without_token_truncation():
    graph = _Graph()
    text_chunks = _KV(
        {
            "chunk-a": {"content": "A", "file_path": "a.txt"},
            "chunk-b": {"content": "B", "file_path": "b.txt"},
            "chunk-c": {"content": "C", "file_path": "c.txt"},
            "chunk-d": {"content": "D", "file_path": "d.txt"},
            "chunk-ab": {"content": "A-B", "file_path": "ab.txt"},
            "chunk-bc": {"content": "B-C", "file_path": "bc.txt"},
            "chunk-bd": {"content": "B-D", "file_path": "bd.txt"},
        }
    )

    result = await graph_query_data(
        "A",
        graph,
        text_chunks,
        _KV({}),
        _KV({}),
        QueryParam(
            mode="graph",
            graph_depth=2,
            max_entity_tokens=1,
            max_relation_tokens=1,
            max_total_tokens=1,
        ),
    )

    assert result is not None
    assert len(result.raw_data["data"]["entities"]) == 4
    assert len(result.raw_data["data"]["relationships"]) == 3
    assert result.raw_data["metadata"]["returned_nodes"] == 4
    assert result.raw_data["metadata"]["returned_relationships"] == 3


@pytest.mark.asyncio
async def test_graph_query_rejects_out_of_range_depth_for_sdk_callers():
    with pytest.raises(ValueError, match="graph_depth"):
        await graph_query(
            "A",
            _Graph(),
            _KV({}),
            _KV({}),
            _KV({}),
            QueryParam(mode="graph", graph_depth=6, only_need_context=True),
            {},
        )


@pytest.mark.asyncio
async def test_graph_query_supports_the_streaming_answer_path():
    async def query_llm(prompt, *, stream=False, **kwargs):
        assert prompt == "A"
        assert stream is True

        async def response_chunks():
            yield "graph "
            yield "answer"

        return response_chunks()

    tokenizer = Tokenizer("test", _CharacterTokenizer())
    global_config = {
        "tokenizer": tokenizer,
        "user_prompt_prefix": "",
        "enable_content_headings": False,
        "role_llm_funcs": {"query": query_llm},
    }
    result = await graph_query(
        "A",
        _Graph(),
        _KV({"chunk-a": {"content": "evidence", "file_path": "a.txt"}}),
        _KV({}),
        _KV({}),
        QueryParam(
            mode="graph",
            graph_depth=0,
            stream=True,
            enable_rerank=False,
            max_entity_tokens=10_000,
            max_relation_tokens=10_000,
            max_total_tokens=20_000,
        ),
        global_config,
    )

    assert result is not None
    assert result.is_streaming is True
    assert [chunk async for chunk in result.response_iterator] == [
        "graph ",
        "answer",
    ]


@pytest.mark.asyncio
async def test_graph_batch_deduplicates_relations_in_input_order():
    class _BatchRag:
        chunk_entity_relation_graph = _Graph()
        text_chunks = _KV(
            {
                "chunk-a": {"content": "A", "file_path": "a.txt"},
                "chunk-b": {"content": "B", "file_path": "b.txt"},
                "chunk-c": {"content": "C", "file_path": "c.txt"},
                "chunk-d": {"content": "D", "file_path": "d.txt"},
                "chunk-ab": {"content": "A-B", "file_path": "ab.txt"},
                "chunk-bc": {"content": "B-C", "file_path": "bc.txt"},
                "chunk-bd": {"content": "B-D", "file_path": "bd.txt"},
            }
        )
        entity_chunks = _KV({})
        relation_chunks = _KV({})

        async def _query_done(self):
            return None

    result = await LightRAG.aquery_data_batch(
        _BatchRag(),
        ["A", "B"],
        QueryParam(mode="graph", graph_depth=1),
    )

    assert result["status"] == "success"
    assert [item["entity"] for item in result["results"]] == ["A", "B"]
    second = result["results"][1]["data"]
    assert {item["entity_name"] for item in second["data"]["entities"]} == {
        "B",
        "C",
        "D",
    }
    assert {
        (item["src_id"], item["tgt_id"])
        for item in second["data"]["relationships"]
    } == {("B", "C"), ("B", "D")}
    assert second["metadata"]["deduplicated_relationships"] == [
        make_relation_chunk_key("A", "B")
    ]
    assert result["metadata"]["unique_relationships"] == 3


@pytest.mark.asyncio
async def test_graph_batch_prunes_only_paths_through_an_earlier_root():
    class _PathGraph(_Graph):
        def __init__(self):
            self.nodes = {
                name: {"entity_type": "NODE", "source_id": f"chunk-{name.lower()}"}
                for name in ("A", "B", "C", "D", "E", "F", "G")
            }
            self.edges = {
                ("A", "B"): {"description": "A-B", "source_id": "chunk-ab"},
                ("B", "C"): {"description": "B-C", "source_id": "chunk-bc"},
                ("C", "D"): {"description": "C-D", "source_id": "chunk-cd"},
                ("B", "E"): {"description": "B-E", "source_id": "chunk-be"},
                ("E", "F"): {"description": "E-F", "source_id": "chunk-ef"},
                ("E", "G"): {"description": "E-G", "source_id": "chunk-eg"},
            }

    class _PathBatchRag:
        chunk_entity_relation_graph = _PathGraph()
        text_chunks = _KV(
            {
                **{
                    f"chunk-{name.lower()}": {
                        "content": name,
                        "file_path": f"{name}.txt",
                    }
                    for name in ("A", "B", "C", "D", "E", "F", "G")
                },
                **{
                    f"chunk-{left.lower()}{right.lower()}": {
                        "content": f"{left}-{right}",
                        "file_path": f"{left.lower()}{right.lower()}.txt",
                    }
                    for left, right in (
                        ("A", "B"),
                        ("B", "C"),
                        ("C", "D"),
                        ("B", "E"),
                        ("E", "F"),
                        ("E", "G"),
                    )
                },
            }
        )
        entity_chunks = _KV({})
        relation_chunks = _KV({})

        async def _query_done(self):
            return None

    result = await LightRAG.aquery_data_batch(
        _PathBatchRag(),
        ["B", "E"],
        QueryParam(mode="graph", graph_depth=2),
    )

    first = result["results"][0]["data"]
    assert {
        canonical
        for relationship in first["data"]["relationships"]
        for canonical in [
            tuple(sorted((relationship["src_id"], relationship["tgt_id"])))
        ]
    } == {
        ("A", "B"),
        ("B", "C"),
        ("B", "E"),
        ("C", "D"),
        ("E", "F"),
        ("E", "G"),
    }

    second = result["results"][1]["data"]
    assert {item["entity_name"] for item in second["data"]["entities"]} == {
        "E",
        "F",
        "G",
    }
    assert {
        tuple(sorted((item["src_id"], item["tgt_id"])))
        for item in second["data"]["relationships"]
    } == {("E", "F"), ("E", "G")}
    assert set(second["metadata"]["deduplicated_relationships"]) == {
        make_relation_chunk_key("A", "B"),
        make_relation_chunk_key("B", "C"),
        make_relation_chunk_key("B", "E"),
    }
    assert second["metadata"]["skipped_disconnected_relationships"] == []
    assert result["metadata"]["returned_relationships"] == 8
    assert result["metadata"]["unique_relationships"] == 6
    assert result["metadata"]["repeated_relationships"] == 2
