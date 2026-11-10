"""Rebuild canonical data and reject equal-count corruptions before activation."""

from copy import deepcopy
import json

import pytest

from lightrag.batch_control.index_rebuild.projection import (
    bind_plan,
    plan_digest,
    project,
    verify_rows,
)
from lightrag.batch_control.index_rebuild.source import (
    SourceIdentity,
    SourceLimits,
    read_source,
    write_source,
)
from lightrag.batch_control.storage import IntegrityError
from lightrag.utils import compute_mdhash_id

pytestmark = pytest.mark.offline


@pytest.fixture
def canonical():
    return {
        "nodes": [
            {
                "id": "甲部门",
                "description": "管理合成项目",
                "entity_type": "organization",
                "source_id": "chunk-a",
                "file_path": "one.md",
            },
            {"id": "乙项目", "description": "合成项目", "entity_type": "UNKNOWN"},
        ],
        "edges": [
            {
                "source": "乙项目",
                "target": "甲部门",
                "keywords": "管理",
                "description": "合成关系",
                "weight": 1,
                "source_id": "chunk-a",
                "file_path": "one.md",
            }
        ],
        "chunks": [
            {
                "id": "chunk-a",
                "full_doc_id": "doc-a",
                "content": "甲部门管理乙项目。",
                "file_path": "one.md",
                "tokens": 10,
            }
        ],
    }


@pytest.fixture
def identity():
    return SourceIdentity(
        data_batch_id="batch-a",
        operation_id="op-a",
        config_revision_id="rev-a",
        workspace="batch_abc",
        generation=1,
        lease_id="lease-a",
        startup_digest="a" * 64,
    )


def persisted(expected):
    return [
        {"id": key, **record, "vector": [0.25, 0.5, 0.75], "created_at": 1}
        for key, record in expected.items()
    ]


def test_projection_preserves_ingestion_ids_and_content_without_mutating_sources(
    canonical,
):
    original = deepcopy(canonical)
    plan = bind_plan(project(**canonical, names=True), names=True)
    assert (
        plan["entities"][compute_mdhash_id("甲部门", prefix="ent-")]["content"]
        == "甲部门\n管理合成项目"
    )
    source, target = sorted(["甲部门", "乙项目"])
    assert (
        plan["relationships"][compute_mdhash_id(source + target, prefix="rel-")][
            "content"
        ]
        == f"管理\t{source}\n{target}\n合成关系"
    )
    assert plan["chunks"]["chunk-a"]["content"] == "甲部门管理乙项目。"
    assert list(plan["name_entities"]) == [
        compute_mdhash_id("甲部门", prefix="ent-name-")
    ]
    assert canonical == original
    assert {key: info["rows"] for key, info in plan_digest(plan).items()} == {
        "entities": 2,
        "relationships": 1,
        "chunks": 1,
        "name_entities": 1,
    }


def test_content_transformation_is_frozen_in_the_expected_plan(canonical):
    plan = bind_plan(
        project(**canonical), transform=lambda role, key, content: content[:5]
    )
    expected = plan["entities"]
    assert all(len(row["content"]) <= 5 for row in expected.values())
    proof = verify_rows("entities", expected, persisted(expected), 3)
    assert proof["rows"] == 2
    assert proof["content_sha256"] == plan_digest(plan)["entities"]["sha256"]


@pytest.mark.parametrize(
    "corruption",
    [
        "node_duplicate",
        "edge_duplicate",
        "missing_endpoint",
        "chunk_duplicate",
        "empty_chunk",
        "identity_collision",
    ],
)
def test_ambiguous_or_incomplete_canonical_rows_never_form_a_plan(
    canonical, corruption
):
    if corruption == "node_duplicate":
        canonical["nodes"].append(dict(canonical["nodes"][0]))
    elif corruption == "edge_duplicate":
        canonical["edges"].append(
            {**canonical["edges"][0], "source": "甲部门", "target": "乙项目"}
        )
    elif corruption == "missing_endpoint":
        canonical["edges"][0]["source"] = "不存在的节点"
    elif corruption == "chunk_duplicate":
        canonical["chunks"].append(dict(canonical["chunks"][0]))
    elif corruption == "empty_chunk":
        canonical["chunks"][0]["content"] = " "
    else:
        canonical = {
            "nodes": [{"id": name} for name in ("a", "bc", "ab", "c")],
            "edges": [{"source": "a", "target": "bc"}, {"source": "ab", "target": "c"}],
            "chunks": [],
        }
    with pytest.raises(IntegrityError):
        bind_plan(project(**canonical))


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "extra",
        "same_count_wrong_id",
        "duplicate",
        "content",
        "metadata",
        "dimension",
        "nan",
        "zero",
        "boolean",
    ],
)
def test_target_count_alone_cannot_pass_complete_persisted_row_verification(
    canonical, corruption
):
    plan = bind_plan(project(**canonical))
    expected = plan["entities"]
    rows = persisted(expected)
    if corruption == "missing":
        rows.pop()
    elif corruption == "extra":
        rows.append({**rows[0], "id": "foreign"})
    elif corruption == "same_count_wrong_id":
        rows[0]["id"] = "foreign"
    elif corruption == "duplicate":
        rows[1] = dict(rows[0])
    elif corruption == "content":
        rows[0]["content"] = "different"
    elif corruption == "metadata":
        rows[0]["entity_name"] = "other"
    elif corruption == "dimension":
        rows[0]["vector"] = [1.0, 2.0]
    elif corruption == "nan":
        rows[0]["vector"][0] = float("nan")
    elif corruption == "zero":
        rows[0]["vector"] = [0.0, 0.0, 0.0]
    else:
        rows[0]["vector"][0] = True
    with pytest.raises(IntegrityError):
        verify_rows("entities", expected, rows, 3)


def test_source_publication_is_complete_owned_and_does_not_modify_inputs(
    canonical, identity, tmp_path
):
    original = deepcopy(canonical)
    directory = tmp_path / "source"
    written = write_source(directory, identity, **canonical, names=True)
    manifest, values = read_source(directory, identity)
    assert manifest == written
    assert values == original == canonical
    assert manifest["graph"]["nodes"] == 2
    with pytest.raises(FileExistsError):
        write_source(directory, identity, **canonical)
    other = identity.model_copy(update={"operation_id": "op-other"})
    with pytest.raises(IntegrityError):
        read_source(directory, other)


@pytest.mark.parametrize(
    "corruption", ["artifact", "marker", "symlink", "projection", "catalog"]
)
def test_source_marker_requires_matching_artifacts_identity_and_full_projection(
    canonical, identity, tmp_path, corruption
):
    directory = tmp_path / "source"
    write_source(directory, identity, **canonical)
    if corruption == "artifact":
        (directory / "chunks.jsonl").write_text("{}\n")
    elif corruption == "symlink":
        target = directory / "chunks.jsonl"
        target.rename(tmp_path / "foreign.jsonl")
        target.symlink_to(tmp_path / "foreign.jsonl")
    else:
        marker = directory / "source.json"
        value = json.loads(marker.read_text())
        if corruption == "marker":
            value["identity"]["generation"] = 2
        elif corruption == "projection":
            value["projection"]["chunks"]["rows"] = 0
        else:
            value["files"]["../foreign.jsonl"] = value["files"].pop("nodes.jsonl")
        marker.write_text(json.dumps(value))
    with pytest.raises(IntegrityError):
        read_source(directory, identity)


@pytest.mark.parametrize("limit", ["max_rows", "max_row_bytes", "max_evidence_bytes"])
def test_source_limits_never_seal_a_truncated_subset(
    canonical, identity, tmp_path, limit
):
    directory = tmp_path / "source"
    with pytest.raises(ValueError):
        write_source(
            directory, identity, **canonical, limits=SourceLimits(**{limit: 1})
        )
    assert not (directory / "source.json").exists()


def test_empty_source_has_an_explicit_verifiable_empty_plan(identity, tmp_path):
    directory = tmp_path / "empty"
    manifest = write_source(directory, identity, [], [], [])
    loaded, _ = read_source(directory, identity)
    assert loaded == manifest
    assert {role: value["rows"] for role, value in manifest["projection"].items()} == {
        "entities": 0,
        "relationships": 0,
        "chunks": 0,
    }
    assert verify_rows("chunks", {}, [], 3)["rows"] == 0
