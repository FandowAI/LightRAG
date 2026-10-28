"""Destructive reset behavior is tested only with temporary files and fake databases."""

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import MagicMock, Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "reset_system_data_script",
    Path(__file__).resolve().parents[2] / "scripts/reset_system_data.py",
)
reset = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reset
spec.loader.exec_module(reset)

pytestmark = pytest.mark.offline


@pytest.fixture
def deployment(tmp_path):
    project = tmp_path / "LightRAG"
    config = tmp_path / "config"
    workflow = tmp_path / "data-work-flow"
    for path in (project, config, workflow):
        path.mkdir()
    (project / ".env").write_text("DO_NOT_DELETE=config")
    (config / "prompt.md").write_text("KEEP $payload")
    roots = {
        name: tmp_path / name for name in ("inputs", "storage", "distillation_jobs")
    }
    for path in roots.values():
        path.mkdir()
    (roots["inputs"] / "document.txt").write_text("original")
    workspace = roots["storage"] / "workspace"
    workspace.mkdir()
    (workspace / "kv_store_full_docs.json").write_text('{"doc": "text"}')
    prompts = roots["storage"] / "prompt_admin"
    prompts.mkdir()
    (prompts / "prompts.sqlite3").write_text("keep prompts")
    database = roots["storage"] / "entity_merge_settings.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE entity_merge_settings (threshold REAL)")
        connection.execute("INSERT INTO entity_merge_settings VALUES (0.9)")
        connection.execute("CREATE TABLE enterprise_aliases (alias TEXT)")
        connection.execute("INSERT INTO enterprise_aliases VALUES ('derived entity')")
    job = roots["distillation_jobs"] / "hash" / "distill-test"
    job.mkdir(parents=True)
    (job / "job.yaml").write_text("schema_version: 3")
    (job / "settings.json").write_text('{"credentials_file": "/private.env"}')
    (job / "review.jsonl").write_text('{"question": "private source"}')
    return reset.Deployment(
        project, config, workflow, {}, roots, [config, project / ".env"]
    )


@pytest.fixture
def wired(deployment, monkeypatch):
    database = Mock()
    database.inventory.return_value = {
        "neo4j_nodes": 5,
        "workspaces": ["workspace"],
        "milvus_collections": [{"name": "workspace_entities"}],
    }
    monkeypatch.setattr(reset.Deployment, "load", lambda *args: deployment)
    monkeypatch.setattr(reset, "Databases", lambda *args: database)
    stop = Mock()
    monkeypatch.setattr(reset, "stop_writers", stop)
    monkeypatch.setattr(reset, "processes", lambda *args: [])
    return database, stop


def test_default_is_preview_without_stopping_or_mutation(deployment, wired, capsys):
    database, stop = wired
    assert reset.main([]) == 0
    assert "PREVIEW ONLY" in capsys.readouterr().out
    stop.assert_not_called()
    database.clear_graph.assert_not_called()
    database.clear_vectors.assert_not_called()
    assert (deployment.roots["inputs"] / "document.txt").is_file()
    assert not (deployment.config / "history").exists()


def test_execute_requires_exact_confirmation_before_loading_or_connecting(monkeypatch):
    load = Mock()
    monkeypatch.setattr(reset.Deployment, "load", load)
    with pytest.raises(SystemExit) as error:
        reset.main(["--execute", "--confirm", "yes"])
    assert error.value.code == 2
    load.assert_not_called()


def test_confirmed_reset_clears_data_but_preserves_configs_and_prompts(
    deployment, wired
):
    database, stop = wired
    assert reset.main(["--execute", "--confirm", reset.CONFIRMATION]) == 0
    stop.assert_called_once()
    database.clear_graph.assert_called_once()
    database.clear_vectors.assert_called_once()
    database.verify_empty.assert_called_once()
    assert list(deployment.roots["inputs"].iterdir()) == []
    assert list(deployment.roots["distillation_jobs"].iterdir()) == []
    assert (deployment.config / "prompt.md").read_text() == "KEEP $payload"
    assert (deployment.project / ".env").exists()
    assert (deployment.roots["storage"] / "prompt_admin/prompts.sqlite3").exists()
    with sqlite3.connect(
        deployment.roots["storage"] / "entity_merge_settings.sqlite3"
    ) as connection:
        assert connection.execute("SELECT * FROM entity_merge_settings").fetchall() == [
            (0.9,)
        ]
        assert (
            connection.execute("SELECT count(*) FROM enterprise_aliases").fetchone()[0]
            == 0
        )
    reports = list((deployment.config / "history").glob("*/report.json"))
    assert json.loads(reports[0].read_text())["status"] == "completed"
    archived = list(reports[0].parent.glob("task-configs/*/*/*"))
    assert {path.name for path in archived} == {"job.yaml", "settings.json"}
    assert not list(reports[0].parent.rglob("review.jsonl"))


def test_failed_preflight_does_not_stop_services_or_touch_files(deployment, wired):
    database, stop = wired
    database.inventory.side_effect = reset.ResetError("unrecognized database content")
    assert reset.main(["--execute", "--confirm", reset.CONFIRMATION]) == 1
    stop.assert_not_called()
    database.clear_graph.assert_not_called()
    assert (deployment.roots["inputs"] / "document.txt").exists()


def test_failed_graph_deletion_does_not_delete_vectors_or_files(deployment, wired):
    database, _ = wired
    database.clear_graph.side_effect = reset.ResetError("interrupted graph deletion")
    assert reset.main(["--execute", "--confirm", reset.CONFIRMATION]) == 1
    database.clear_vectors.assert_not_called()
    assert (deployment.roots["inputs"] / "document.txt").exists()
    report = next((deployment.config / "history").glob("*/report.json"))
    assert json.loads(report.read_text())["last_completed_phase"] == "writers_stopped"


def test_data_symlinks_are_unlinked_without_touching_external_source(
    deployment, tmp_path
):
    source = tmp_path / "Feishu_extract"
    source.mkdir()
    original = source / "original.md"
    original.write_text("preserve original")
    (deployment.roots["inputs"] / "external").symlink_to(source)
    reset.clear_files(reset.file_plan(deployment))
    assert original.read_text() == "preserve original"


@pytest.mark.parametrize("root_name", ["/", "/data", "/root", "/tmp"])
def test_broad_roots_are_refused(root_name):
    with pytest.raises(reset.ResetError):
        reset.validate_root(Path(root_name), [])


def test_configuration_and_external_import_roots_are_protected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(reset.ResetError):
        reset.validate_root(source / "subfolder", [source])
    with pytest.raises(reset.ResetError):
        reset.validate_root(tmp_path, [source])
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(reset.ResetError):
        reset.validate_root(link, [])


def test_foreign_milvus_collections_are_not_deleted(deployment):
    database = reset.Databases.__new__(reset.Databases)
    database.name = "neo4j"
    database.milvus_name = "lightrag"
    database.vectors = Mock()
    database.vectors.list_collections.return_value = ["other_application"]
    database.vectors.describe_collection.return_value = {
        "description": "someone else's vectors"
    }
    with pytest.raises(reset.ResetError, match="Unrecognized Milvus"):
        database.inventory(deployment)
    database.vectors.drop_collection.assert_not_called()


def test_reused_process_id_is_not_signalled(deployment, monkeypatch):
    monkeypatch.setattr(
        reset, "processes", Mock(side_effect=[[{"pid": 99, "start": "old"}], []])
    )
    monkeypatch.setattr(reset, "same_process", lambda _row: False)
    kill = Mock()
    monkeypatch.setattr(reset.os, "kill", kill)
    reset.stop_writers(deployment, 1)
    kill.assert_not_called()


def test_failure_to_stop_writers_prevents_all_data_deletion(deployment, wired):
    database, stop = wired
    stop.side_effect = reset.ResetError("writers are still alive")
    assert reset.main(["--execute", "--confirm", reset.CONFIRMATION]) == 1
    database.clear_graph.assert_not_called()
    database.clear_vectors.assert_not_called()
    assert (deployment.roots["inputs"] / "document.txt").exists()


def test_inventory_recognizes_all_four_vector_namespaces(deployment):
    database = reset.Databases.__new__(reset.Databases)
    database.name, database.milvus_name = "neo4j", "lightrag"
    database.vectors = Mock()
    descriptions = {
        f"same_workspace_{namespace}_test_4096d": description
        for description, namespace in reset.MILVUS_NAMESPACES.items()
    }
    database.vectors.list_collections.return_value = list(descriptions)
    database.vectors.describe_collection.side_effect = lambda name: {
        "description": descriptions[name]
    }
    database.vectors.get_collection_stats.return_value = {"row_count": 2}
    database.graph = MagicMock()
    session = database.graph.session.return_value.__enter__.return_value
    result = Mock()
    result.data.return_value = [{"label": "same_workspace", "nodes": 3}]
    result.single.side_effect = [{"n": 0}, {"nodes": 3}, {"relationships": 2}]
    session.run.return_value = result
    inventory = database.inventory(deployment)
    assert "same_workspace" in inventory["workspaces"]
    assert "same_workspace_name" not in inventory["workspaces"]
    assert len(inventory["milvus_collections"]) == 4
    database.vectors.drop_collection.assert_not_called()


def test_graph_deletion_is_bounded_to_recognized_workspace_labels():
    database = reset.Databases.__new__(reset.Databases)
    database.name = "neo4j"
    database.graph = MagicMock()
    session = database.graph.session.return_value.__enter__.return_value
    session.run.return_value.single.side_effect = [{"deleted": 3}, {"deleted": 0}]
    database.clear_graph({"neo4j_nodes": 3, "workspaces": ["scope"]})
    assert session.run.call_count == 2
    for call in session.run.call_args_list:
        assert (
            "WHERE any(label IN labels(n) WHERE label IN $workspaces)" in call.args[0]
        )
        assert call.kwargs["workspaces"] == ["scope"]


def test_unknown_graph_nodes_abort_before_any_deletion(deployment):
    database = reset.Databases.__new__(reset.Databases)
    database.name, database.milvus_name = "neo4j", "lightrag"
    database.vectors = Mock()
    database.vectors.list_collections.return_value = []
    database.graph = MagicMock()
    result = database.graph.session.return_value.__enter__.return_value.run.return_value
    result.data.return_value = [{"label": "other_application", "nodes": 1}]
    result.single.return_value = {"n": 1}
    with pytest.raises(reset.ResetError, match="outside recognized"):
        database.inventory(deployment)
    database.vectors.drop_collection.assert_not_called()


@pytest.mark.parametrize(
    "uri",
    [
        "neo4j://user:password@localhost:7687?key=secret",
        "http://user:password@127.0.0.1:19530?token=secret",
        "bolt://user:password@[::1]:7687/path?key=secret",
    ],
)
def test_preview_endpoints_do_not_expose_credentials(uri):
    endpoint = reset.public_endpoint(uri)
    assert "password" not in endpoint
    assert "secret" not in endpoint
    assert "user" not in endpoint
