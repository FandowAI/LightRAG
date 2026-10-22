import copy
import json

import pytest

from lightrag.integrations.data_workflow.conversion import convert_result

pytestmark = pytest.mark.offline


def test_projects_only_the_three_fields_without_changing_text(query_result):
    original = copy.deepcopy(query_result)
    envelope = convert_result(query_result)
    assert set(envelope) == {"text"}
    context = json.loads(envelope["text"])
    assert set(context) == {"entities", "relationships", "text_chunks"}
    assert context["entities"] == [
        {"entity": "费用报销", "type": "PROCESS", "description": "申请报销的流程"}
    ]
    assert context["relationships"] == [
        {"entity1": "费用报销", "entity2": "发票", "description": "必须提交"}
    ]
    assert context["text_chunks"] == [original["data"]["chunks"][0]["content"]]
    assert query_result == original
    assert "chunk-1" not in envelope["text"] and "a.docx" not in envelope["text"]


def test_accepts_context_style_aliases_and_preserves_order(query_result):
    query_result["data"]["entities"] = [
        {"entity": "A", "type": "CONCEPT", "description": "a"}
    ]
    query_result["data"]["relationships"] = [
        {"entity1": "A", "entity2": "B", "description": "r"}
    ]
    query_result["data"]["chunks"] = [{"content": "first"}, {"content": "second"}]
    assert json.loads(convert_result(query_result)["text"])["text_chunks"] == [
        "first",
        "second",
    ]


def test_naive_needs_no_entities_or_relations():
    envelope = convert_result(
        {"status": "success", "data": {"chunks": [{"content": "原文"}]}}
    )
    assert json.loads(envelope["text"]) == {
        "entities": [],
        "relationships": [],
        "text_chunks": ["原文"],
    }


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"status": "failure", "data": {}},
        {"status": "success", "data": {}},
        {"status": "success", "data": {"chunks": [{"content": " "}]}},
        {"status": "success", "data": {"chunks": [{"content": 42}]}},
        {"status": "success", "data": {"chunks": "body"}},
        {
            "status": "success",
            "data": {"entities": [{}], "chunks": [{"content": "body"}]},
        },
    ],
)
def test_rejects_failed_or_malformed_evidence(data):
    with pytest.raises(ValueError):
        convert_result(data)
