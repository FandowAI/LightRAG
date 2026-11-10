"""Read completed workflow artifacts by job identity, never by caller-supplied paths.

Preview final QA versions without implying machine or human approval. Downloads
retain the official full review record, including source and inspection evidence.
"""

import json
from pathlib import Path

from .storage import JobConflict, TERMINAL


MAX_ROW_BYTES = 64 * 1024 * 1024


def _regular(path, parent):
    if path.absolute() != path.resolve() or path.parent.resolve() != parent.resolve():
        raise ValueError("Workflow artifact must stay inside its owned directory")
    if not path.is_file():
        raise JobConflict("Workflow artifact is not available")
    return path


def artifact(record, name):
    directory = Path(record["directory"])
    if name in {"stdout", "stderr"}:
        if record["status"] not in TERMINAL:
            raise JobConflict("Wait for completion before downloading the full log")
        return _regular(directory / (name + ".log"), directory)
    if name != "review":
        raise ValueError("Unknown workflow artifact")
    if record["status"] not in {"completed", "completed_with_errors"}:
        raise JobConflict("QA results require a completed workflow")
    receipt = record.get("result") or {}
    run = Path(receipt.get("directory", ""))
    if run.parent.resolve() != (directory / "outputs").resolve():
        raise ValueError("Workflow result belongs to another job")
    manifest_path = _regular(run / "run.json", run)
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("Workflow manifest exceeds the preview limit")
    manifest = json.loads(manifest_path.read_text())
    path = _regular(run / "review.jsonl", run)
    info = manifest.get("artifacts", {}).get("review.jsonl", {})
    size = path.stat().st_size
    if (
        manifest.get("schema_version") != 4
        or manifest.get("review_schema_version") != 3
        or manifest.get("run_id") != receipt.get("run_id")
        or manifest.get("status") != receipt.get("status")
        or info.get("status") != "committed"
        or info.get("bytes") != size
        or manifest.get("checkpoint", {}).get("offset") != size
    ):
        raise JobConflict("Workflow result has no matching committed publication")
    return path


def preview(record, offset=0, limit=20):
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid preview page")
    path = artifact(record, "review")
    receipt = record["result"]
    found, items = 0, []
    with path.open("rb") as stream:
        while raw := stream.readline(MAX_ROW_BYTES + 1):
            if len(raw) > MAX_ROW_BYTES:
                raise ValueError(
                    "Review row is too large for preview; use the full download"
                )
            if not raw.strip():
                continue
            source = json.loads(raw)
            if (
                not isinstance(source, dict)
                or source.get("schema_version") != 3
                or source.get("run_id") != receipt["run_id"]
                or source.get("workflow_contract")
                not in {"difficulty_routed_qa.v5", "difficulty_routed_qa.v6"}
            ):
                raise ValueError("Unsupported or foreign workflow review record")
            work_items = source.get("work_items")
            if not isinstance(work_items, list):
                raise ValueError("Review work_items must be an array")
            for item in work_items:
                if not isinstance(item, dict):
                    raise ValueError("Review item must be an object")
                if item.get("outcome") != "processed":
                    continue
                versions = item.get("versions") or []
                if (
                    not isinstance(versions, list)
                    or not versions
                    or not isinstance(versions[-1], dict)
                ):
                    raise ValueError("Final QA version is missing")
                qa = versions[-1].get("qa", {})
                if not isinstance(qa, dict):
                    raise ValueError("Final QA must be an object")
                question, answer = (
                    qa.get("generated_question"),
                    qa.get("generated_answer"),
                )
                if not isinstance(question, str) or not isinstance(answer, str):
                    raise ValueError("Final QA version is missing question or answer")
                if found >= offset:
                    if len(items) == limit:
                        return {
                            "items": items,
                            "offset": offset,
                            "next_offset": offset + len(items),
                            "has_more": True,
                        }
                    items.append(
                        {
                            "question": question,
                            "answer": answer,
                            "source_id": source.get("source_id"),
                            "work_item_id": item.get("id"),
                            "version_id": versions[-1].get("id"),
                        }
                    )
                found += 1
    return {
        "items": items,
        "offset": offset,
        "next_offset": offset + len(items),
        "has_more": False,
    }
