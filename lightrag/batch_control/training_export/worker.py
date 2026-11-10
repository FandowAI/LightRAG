"""Validate the official current review contract and project final QA, without models."""

from collections import Counter
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

from core import atomic_json, encode, publish_file, snapshot


def jsonl(path, maximum, load):
    with Path(path).open("rb") as stream:
        while raw := stream.readline(maximum + 1):
            if len(raw) > maximum:
                raise ValueError(
                    "Export evidence row exceeds the configured limit; no subset was published"
                )
            yield load(raw.decode("utf-8"))


def training_row(qa, shape):
    question, answer = qa.generated_question, qa.generated_answer
    if shape == "messages":
        return {
            "messages": [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
        }
    if shape == "alpaca":
        return {"instruction": question, "input": "", "output": answer}
    if shape == "sharegpt":
        return {
            "conversations": [
                {"from": "human", "value": question},
                {"from": "gpt", "value": answer},
            ]
        }
    raise ValueError("Unsupported training format")


def run(spec, directory):
    from data_studio import load_config, prepare_job
    from data_studio.config.source import SourceFields
    from data_studio.config.topics import CategoryCatalog
    from data_studio.contracts.records import SourceReview
    from data_studio.processing.extract import MISSING, extract
    from data_studio.processing.inspection import validate_inspection_source
    from data_studio.utils.json import load_json

    limits, files = spec["limits"], spec["inputs"]

    def unchanged():
        for info in files.values():
            if snapshot(info["path"], limits["max_evidence_bytes"]) != info["snapshot"]:
                raise ValueError("Workflow evidence changed after export submission")

    unchanged()
    manifest = load_json(Path(files["run.json"]["path"]).read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 4
        or manifest.get("review_schema_version") != 3
        or manifest.get("run_id") != spec["workflow_run_id"]
        or manifest.get("status") not in {"completed", "completed_with_errors"}
    ):
        raise ValueError(
            "Export requires a completed current manifest v4 and review v3"
        )
    for name, checkpoint in (
        ("review.jsonl", "offset"),
        ("events.jsonl", "events_offset"),
    ):
        artifact = manifest.get("artifacts", {}).get(name, {})
        size = files[name]["snapshot"]["bytes"]
        if (
            artifact.get("status") != "committed"
            or type(artifact.get("bytes")) is not int
            or artifact["bytes"] != size
            or type(manifest.get("checkpoint", {}).get(checkpoint)) is not int
            or manifest["checkpoint"][checkpoint] != size
        ):
            raise ValueError("Workflow artifact or byte checkpoint is uncommitted")
    workflow = manifest.get("config", {}).get("workflow", {})
    if workflow.get("name") != "difficulty_routed_qa":
        raise ValueError(
            "Training export requires the selected difficulty_routed_qa workflow"
        )
    config = load_config(files["job.yaml"]["path"])
    plan = prepare_job(
        config,
        environ={
            model.key_name_of_api_key: "validation-only"
            for model in config.models.values()
        },
    )
    if encode(plan.config_snapshot()) != encode(manifest["config"]):
        raise ValueError(
            "Published run configuration differs from its frozen job recipe"
        )
    if plan.parameters.scenario.parser != "scenarios_json.v2":
        raise ValueError("Training export requires explicit typed scenarios")
    expected_contract = (
        "difficulty_routed_qa.v5"
        if isinstance(plan.parameters.topic_catalog, CategoryCatalog)
        else "difficulty_routed_qa.v6"
    )
    fields = SourceFields.model_validate(
        workflow.get("parameters", {}).get("fields", {})
    )
    input_path = Path(files["input.jsonl"]["path"])
    offsets, position = [], 0
    with input_path.open("rb") as stream:
        while raw := stream.readline(limits["max_row_bytes"] + 1):
            if len(raw) > limits["max_row_bytes"]:
                raise ValueError("Workflow input row exceeds the export limit")
            offsets.append((position, len(raw)))
            position += len(raw)
    sources, source_lines, versions, issues, counts = (
        set(),
        set(),
        {},
        [],
        Counter(
            {
                "sources": 0,
                "work_items": 0,
                "samples": 0,
                "skipped_duplicate": 0,
                "skipped_technical_failure": 0,
                "skipped_policy": 0,
            }
        ),
    )
    outputs = {
        key: directory / (key + ".jsonl.part") for key in ("dataset", "provenance")
    }
    output_bytes = 0
    with (
        input_path.open("rb") as original,
        outputs["dataset"].open("xb") as data,
        outputs["provenance"].open("xb") as provenance,
    ):
        os.chmod(outputs["dataset"], 0o600)
        os.chmod(outputs["provenance"], 0o600)
        for row in jsonl(
            files["review.jsonl"]["path"], limits["max_row_bytes"], load_json
        ):
            if (
                not isinstance(row, dict)
                or row.get("schema_version") != 3
                or row.get("workflow_contract") != expected_contract
            ):
                raise ValueError(
                    "Training export requires the frozen topic-catalog contract "
                    + expected_contract
                )
            source = SourceReview.model_validate(row)
            validate_inspection_source(source)
            if (
                source.run_id != spec["workflow_run_id"]
                or source.source_id in sources
                or source.input_line in source_lines
                or not 1 <= source.input_line <= len(offsets)
                or str(input_path) != source.input_path
            ):
                raise ValueError(
                    "Review source identity does not match the delivered workflow input"
                )
            sources.add(source.source_id)
            source_lines.add(source.input_line)
            counts["sources"] += 1
            counts["sources_" + source.status] += 1
            offset, size = offsets[source.input_line - 1]
            original.seek(offset)
            raw = original.read(size)
            if source.raw_line is not None:
                if (
                    source.raw_line != raw.decode("utf-8", errors="replace")
                    or source.work_items
                    or base64.b64decode(
                        source.processing.get("raw_line_base64", ""), validate=True
                    )
                    != raw
                ):
                    raise ValueError(
                        "Invalid-input evidence contradicts its original line"
                    )
            else:
                value = load_json(raw.decode("utf-8"))
                if encode(source.raw_input) != encode(value):
                    raise ValueError(
                        "Review raw_input differs from the actual delivered input"
                    )
                text, meta = (
                    extract(value, fields.text_path),
                    extract(value, fields.meta_path),
                )
                source.validate_original(
                    None if text is MISSING else text,
                    None if meta is MISSING else meta,
                    meta is not MISSING,
                )
            issues.extend((source.source_id, issue) for issue in source.issues)
            for item in source.work_items:
                counts["work_items"] += 1
                for version in item.versions:
                    if version.id in versions:
                        raise ValueError(
                            "QA version identity is duplicated across sources"
                        )
                    versions[version.id] = source.source_id
                if item.outcome in {"duplicate", "technical_failure"}:
                    counts["skipped_" + item.outcome] += 1
                    continue
                if item.outcome != "processed" or not item.versions:
                    raise ValueError("Unfinished or unsupported QA outcome")
                final, initial, inspection = (
                    item.versions[-1],
                    item.versions[0],
                    item.inspection,
                )
                if (
                    initial.classification is not None
                    and initial.classification.status == "succeeded"
                ):
                    plan.parameters.topic_catalog.validate_topic(
                        initial.classification.topic
                    )
                passed = (
                    inspection.stage_status == "completed"
                    and inspection.ai_status == "passed"
                    and inspection.input_version_id == final.id
                )
                if spec["selection"] == "machine_passed" and not passed:
                    counts["skipped_policy"] += 1
                    continue
                counts["samples"] += 1
                if counts["samples"] > limits["max_samples"]:
                    raise ValueError(
                        "Export exceeds the sample limit; no subset was published"
                    )
                projected = training_row(final.qa, spec["format"])
                encoded = encode(projected)
                identity = {
                    key: spec[key]
                    for key in (
                        "data_batch_id",
                        "task_id",
                        "config_revision_id",
                        "workflow_run_id",
                    )
                }
                record = {
                    "schema_version": 1,
                    **identity,
                    "record_index": counts["samples"],
                    "sample_id": hashlib.sha256(
                        encode({**identity, "version_id": final.id})
                    ).hexdigest(),
                    "record_sha256": hashlib.sha256(encoded).hexdigest(),
                    "source_id": source.source_id,
                    "input_line": source.input_line,
                    "input_record_sha256": hashlib.sha256(
                        encode(source.raw_input)
                    ).hexdigest(),
                    "source_text_sha256": hashlib.sha256(
                        (source.text or "").encode("utf-8", errors="backslashreplace")
                    ).hexdigest(),
                    "work_item_id": item.id,
                    "version_id": final.id,
                    "revision": final.revision,
                    "source": spec["source"],
                    "machine_evidence": {
                        "outcome": item.outcome,
                        "human_review": item.human_review,
                        "classification_version_id": initial.id,
                        "classification": initial.classification.model_dump(
                            mode="json", exclude={"call"}
                        )
                        if initial.classification is not None
                        else None,
                        "inspection": inspection.model_dump(
                            mode="json", exclude={"call"}
                        ),
                        "final_version_machine_passed": passed,
                    },
                }
                encoded_provenance = encode(record)
                output_bytes += len(encoded) + len(encoded_provenance) + 2
                if output_bytes > limits["max_output_bytes"]:
                    raise ValueError(
                        "Export exceeds the output limit; no subset was published"
                    )
                data.write(encoded + b"\n")
                provenance.write(encoded_provenance + b"\n")
            atomic_json(directory / "progress.json", dict(counts))
        for stream in (data, provenance):
            stream.flush()
            os.fsync(stream.fileno())
    if (
        type(manifest.get("checkpoint", {}).get("source_groups")) is not int
        or counts["sources"] != manifest["checkpoint"]["source_groups"]
        or len(source_lines) != len(offsets)
    ):
        raise ValueError(
            "Workflow source count/checkpoint does not cover the delivered input"
        )
    events = {}
    for event in jsonl(
        files["events.jsonl"]["path"], limits["max_row_bytes"], load_json
    ):
        if (
            not isinstance(event, dict)
            or event.get("run_id") != spec["workflow_run_id"]
            or event.get("source_id") is not None
            and event["source_id"] not in sources
            or event.get("qa_id") is not None
            and versions.get(event["qa_id"]) != event.get("source_id")
        ):
            raise ValueError("Workflow event references a different run/source/QA")
        if event.get("event_id") is not None:
            if not isinstance(event["event_id"], str) or event["event_id"] in events:
                raise ValueError("Workflow event identity is invalid or duplicated")
            events[event["event_id"]] = event
    issue_ids = set()
    for source_id, issue in issues:
        event = events.get(issue.event_id)
        expected = {
            "source_id": source_id,
            "qa_id": issue.qa_id,
            "expert_id": issue.expert_id,
            "stage": issue.stage,
            "code": issue.code,
            "message": issue.message,
        }
        if (
            issue.event_id in issue_ids
            or event is None
            or any(event.get(key) != value for key, value in expected.items())
        ):
            raise ValueError("Workflow issue does not match its event evidence")
        issue_ids.add(issue.event_id)
    unchanged()
    artifacts = {}
    for key, part in outputs.items():
        info = snapshot(part, limits["max_output_bytes"])
        publish_file(part, directory / (key + ".jsonl"))
        artifacts[key + ".jsonl"] = {name: info[name] for name in ("bytes", "sha256")}
    result = {
        "schema_version": 1,
        **{
            key: spec[key]
            for key in (
                "export_id",
                "data_batch_id",
                "task_id",
                "config_revision_id",
                "workflow_run_id",
                "format",
                "selection",
            )
        },
        "counts": dict(counts),
        "files": artifacts,
        "inputs": {
            name: {field: info["snapshot"][field] for field in ("bytes", "sha256")}
            for name, info in files.items()
        },
        "configuration_manifest_sha256": spec["configuration_manifest_sha256"],
        "workflow_fingerprint": spec["workflow_fingerprint"],
        "source_capture": "export_submission_snapshot",
        "human_approval_inferred": False,
    }
    part = directory / "export.json.part"
    with part.open("xb") as stream:
        os.chmod(part, 0o600)
        stream.write(encode(result))
        stream.flush()
        os.fsync(stream.fileno())
    publish_file(part, directory / "export.json")
    return result


if __name__ == "__main__":
    try:
        spec_path = Path(sys.argv[1])
        result = run(json.loads(spec_path.read_text()), spec_path.parent)
        print(
            json.dumps(
                {
                    "export_id": result["export_id"],
                    "state": "completed",
                    "counts": result["counts"],
                }
            )
        )
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
