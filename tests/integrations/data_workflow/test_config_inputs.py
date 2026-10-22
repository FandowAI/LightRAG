import json
import sys

import pytest
import yaml

from lightrag.integrations.data_workflow.config import (
    load_settings,
    read_yaml,
    write_job_config,
)
from lightrag.integrations.data_workflow.inputs import read_results

pytestmark = pytest.mark.offline


def test_interpreter_symlink_keeps_venv_path(bridge, tmp_path):
    _, config, _, _ = bridge
    interpreter = tmp_path / "venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    doc = read_yaml(config)
    doc["python"] = "venv/bin/python"
    config.write_text(yaml.safe_dump(doc))
    assert load_settings(config).python == interpreter


def test_snapshot_rebases_prompt_files_and_owns_input_mapping(bridge, tmp_path):
    _, _, _, recipe = bridge
    doc = read_yaml(recipe)
    doc["workflow"] = {
        "name": "review_classifition",
        "parameters": {
            "scenario": {"prompt_file": "prompts/scenario.md"},
            "fields": {"text_path": ["other"], "strip_outer_whitespace": False},
        },
    }
    recipe.write_text(yaml.safe_dump(doc))
    output = tmp_path / "prepared"
    output.mkdir()
    snapshot = write_job_config(recipe, output)
    assert snapshot["input"]["paths"] == [str(output / "input.jsonl")]
    assert snapshot["output"]["directory"] == str(output / "outputs")
    params = snapshot["workflow"]["parameters"]
    assert params["scenario"]["prompt_file"] == str(tmp_path / "prompts/scenario.md")
    assert params["fields"]["text_path"] == ["text"]
    assert read_yaml(recipe) == doc


def test_json_array_and_wrapper_path(bridge, query_result):
    _, config, source, _ = bridge
    source.write_text(
        json.dumps({"payload": {"results": [query_result, query_result]}})
    )
    doc = read_yaml(config)
    doc["input"]["records_path"] = ["payload", "results"]
    config.write_text(yaml.safe_dump(doc))
    assert [item for item, error in read_results(load_settings(config))] == [
        query_result
    ] * 2


def test_jsonl_reports_bad_line_and_continues(bridge, query_result):
    _, config, source, _ = bridge
    jsonl = source.with_suffix(".jsonl")
    jsonl.write_text(
        json.dumps(query_result) + "\ninvalid\n" + json.dumps(query_result)
    )
    doc = read_yaml(config)
    doc["input"]["paths"] = [str(jsonl)]
    config.write_text(yaml.safe_dump(doc))
    results = list(read_results(load_settings(config)))
    assert results[0] == results[2] == (query_result, None)
    assert results[1][0] is None and results[1][1]


@pytest.mark.parametrize("limits", [{"max_input_bytes": 10}, {"max_records": 1}])
def test_input_limits_fail_the_snapshot(bridge, query_result, limits):
    _, config, source, _ = bridge
    source.write_text(json.dumps([query_result, query_result]))
    doc = read_yaml(config)
    doc["limits"] = limits
    config.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match="exceeds"):
        list(read_results(load_settings(config)))


def test_directory_scan_is_ordered_and_deduplicated(bridge, query_result, tmp_path):
    _, config, _, _ = bridge
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for name in ("b.json", "a.json"):
        (incoming / name).write_text(json.dumps(query_result))
    (incoming / "ignored.txt").write_text("not input")
    doc = read_yaml(config)
    doc["input"]["paths"] = [str(incoming), str(incoming / "a.json")]
    config.write_text(yaml.safe_dump(doc))
    assert len(list(read_results(load_settings(config)))) == 2


def test_duplicate_yaml_keys_are_not_silently_accepted(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("version: 1\nversion: 2\n")
    with pytest.raises(ValueError, match="Duplicate"):
        read_yaml(path)
