"""Export actual current workflow publications with final-version provenance."""

import json
import os
from pathlib import Path
import subprocess
import asyncio
import sys

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lightrag.batch_control.contracts import Configuration, Environment
from lightrag.batch_control.deployment import BatchDeployment
from lightrag.batch_control.routes import create_control_routes
from lightrag.batch_control.runtime_config import RuntimePolicy
from lightrag.batch_control.storage import Conflict, IntegrityError
from lightrag.batch_control.training_export.service import ExportLimits
from lightrag.batch_control.training_export.service import TrainingExports
from lightrag.batch_control.task_store import TaskStore
from lightrag.batch_control.task_workflow import workflow_fingerprint
from lightrag.integrations.data_workflow.config import load_settings, write_job_config
from lightrag.integrations.data_workflow.conversion import convert_result

pytestmark = pytest.mark.offline

GENERATE = """
import json,sys
from data_studio import load_config,prepare_job,run_job
questions=['原始问题一？','原始问题二？','原始问题三？']
pass_first=sys.argv[2]=='pass-first'
class Service:
    def __init__(self,role): self.role=role
    def generate_from_input(self,user_inputs,system_prompt=''):
        results=[]
        for prompt in user_inputs:
            payload=json.loads(prompt.rsplit('\\n',1)[1])
            if self.role=='scenario':
                value={'scenarios':[{'question':q,'question_type':'直接理解','refusal_type':'none'} for q in questions]}
            elif self.role=='qa':
                q=payload['scenario']
                value={'qa':[{'generated_question':q,'generated_answer':'原始回答'+str(questions.index(q)+1)}]}
            elif self.role=='classification':
                value={'importance':'low','importance_reason':'合成依据','difficulty':'low','difficulty_reason':'原文明确','security_check':False,'topic':'general'}
            else:
                q=payload['question']; changed=q==questions[1]
                value={'ai_status':'passed' if pass_first and q==questions[0] else 'not_passed','is_revised':changed,'reason':'合成检查证据','question':'修订后的问题二？' if changed else q,'answer':'修订后的回答二' if changed else payload['answer']}
            results.append(json.dumps(value,ensure_ascii=False))
        return results
    def cleanup(self): pass
config=load_config(sys.argv[1])
plan=prepare_job(config,environ={m.key_name_of_api_key:'offline' for m in config.models.values()})
result=run_job(plan,serving_builder=lambda config:Service(config.model_name))
print(json.dumps({'run_id':result.run_id,'directory':str(result.directory),'status':result.status,'summary':result.summary}))
"""


@pytest.fixture
def export_setup(factory, tmp_path, request):
    project = Path(__file__).resolve().parents[3] / "data-work-flow"
    python = project / ".venv-qa/bin/python"
    if not python.is_file():
        pytest.skip("Selected workflow interpreter is unavailable")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip()
    factory[3].environments["test"] = Environment(
        str(project), str(python), commit, "local-test-dependencies"
    )
    catalog = tmp_path / "topics.yaml"
    catalog.write_text(
        "fallback_topic: general\ntopics:\n  - name: general\n    description: 合成资料\n"
    )
    if getattr(request, "param", None) == "categories":
        catalog.write_text(
            "categories:\n  - id: general\n    name: 通用合成资料\n"
            "    definition: 独立测试资料\n    include: [合成资料]\n"
            "    exclude_to: {}\n    example_questions: [原始问题一？]\n"
        )
    original = tmp_path / "query.jsonl"
    query = {
        "status": "success",
        "data": {
            "entities": [],
            "relationships": [],
            "chunks": [{"content": "原始资料完整保留；一、二、三均为合成问题。"}],
        },
    }
    original.write_text(json.dumps(query, ensure_ascii=False))
    factory[3].inputs["queries"] = [original]
    data = factory[2].model_dump()
    data["bridge"]["input_ids"] = ["queries"]
    roles = ("scenario", "qa", "classification", "inspection")
    data["recipe"]["models"] = {
        role: {
            "api_url": "https://offline.invalid/v1/chat/completions",
            "model_name": role,
            "key_name_of_api_key": "OFFLINE_KEY",
            "max_workers": 1,
        }
        for role in roles
    }
    parameters = data["recipe"]["workflow"]["parameters"]
    parameters["topic_catalog_file"] = str(catalog)
    for role in roles:
        parameters[role]["model"] = role
    parameters["scenario"].update(
        prompt="TypedScenarioPrompt", parser="scenarios_json.v2"
    )
    control = factory[0](defaults_override=Configuration.model_validate(data))

    def probe(settings, operation, recipe=None):
        if operation == "describe":
            return {"installed_commit": commit}
        if operation == "prompt_defaults":
            return {"prompts": {role: role + "\n$payload" for role in roles}}
        return {"valid": True, "issues": []}

    control.probe = probe
    batch = control.create("user", "Export batch", "batch")
    task = control.register_task(
        "user",
        batch["id"],
        batch["active"],
        "distill",
        "distillation",
        {"data_batch_id": batch["id"], "type": "directory", "input_ids": ["queries"]},
        payload={"version": 1},
    )
    root = (
        control.paths.runtime_root
        / "tasks"
        / batch["namespace"]
        / task["task_id"]
        / "workflow"
    )
    root.mkdir(parents=True)
    (root / "input.jsonl").write_text(
        json.dumps(convert_result(query), ensure_ascii=False) + "\n"
    )
    settings = load_settings(
        control.paths.revision(batch["id"], batch["active"]) / "data_workflow.yaml"
    )
    document = control.read("user", batch["id"])[2]
    write_job_config(
        settings.workflow_config,
        root,
        recipe=document.recipe,
        config_root=control.paths.config_root,
    )
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ
    }
    environment.update(PYTHONPATH=str(project / "src"), PYTHONDONTWRITEBYTECODE="1")
    generated = subprocess.run(
        [
            str(python),
            "-c",
            GENERATE,
            str(root / "job.yaml"),
            "pass-first"
            if getattr(request, "param", None) == "categories"
            else getattr(request, "param", "pass-first"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert generated.returncode == 0, generated.stderr[-6000:]
    receipt = json.loads(generated.stdout.splitlines()[-1])
    assert receipt["status"] == "completed"
    TaskStore(control.registry).update(
        batch["id"],
        task["task_id"],
        state="completed",
        payload={"version": 1, "workflow_fingerprint": workflow_fingerprint(settings)},
        result={"distillation": {"status": receipt["status"], "result": receipt}},
    )
    return control, batch, task, root, receipt


@pytest.mark.parametrize("shape", ["messages", "alpaca", "sharegpt"])
def test_final_versions_and_training_shapes_have_exact_line_provenance(
    export_setup, shape
):
    control, batch, task, root, receipt = export_setup
    service = TrainingExports(control, actor="user")
    original = {
        name: (Path(receipt["directory"]) / name).read_bytes()
        for name in ("run.json", "review.jsonl", "events.jsonl")
    }
    job = service.request(batch["id"], task["task_id"], shape, "processed", "export")
    service.run(batch["id"], task["task_id"], job["export_id"])
    status = service.status(batch["id"], task["task_id"], job["export_id"])
    assert status["state"] == "completed", status
    assert status["counts"]["samples"] == 3
    dataset = [
        json.loads(line)
        for line in service.artifact(
            batch["id"], task["task_id"], job["export_id"], "dataset"
        )
        .read_text()
        .splitlines()
    ]
    assert set(dataset[0]) == {
        "messages"
        if shape == "messages"
        else "instruction"
        if shape == "alpaca"
        else "conversations"
    } | ({"input", "output"} if shape == "alpaca" else set())
    serialized = json.dumps(dataset, ensure_ascii=False)
    assert "修订后的问题二？" in serialized and "修订后的回答二" in serialized
    assert "原始回答2" not in serialized
    provenance = [
        json.loads(line)
        for line in service.artifact(
            batch["id"], task["task_id"], job["export_id"], "provenance"
        )
        .read_text()
        .splitlines()
    ]
    assert [row["record_index"] for row in provenance] == [1, 2, 3]
    assert all(
        row["data_batch_id"] == batch["id"]
        and row["config_revision_id"] == task["config_revision_id"]
        for row in provenance
    )
    assert provenance[1]["revision"] == 1
    assert provenance[1]["machine_evidence"]["inspection"]["ai_status"] == "not_passed"
    assert provenance[1]["machine_evidence"]["final_version_machine_passed"] is False
    assert provenance[1]["machine_evidence"]["human_review"] == "pending"
    assert (
        provenance[1]["machine_evidence"]["classification_version_id"]
        != provenance[1]["version_id"]
    )
    assert {
        name: (Path(receipt["directory"]) / name).read_bytes() for name in original
    } == original
    assert (
        service.request(batch["id"], task["task_id"], shape, "processed", "export")[
            "export_id"
        ]
        == job["export_id"]
    )
    passed = service.request(
        batch["id"], task["task_id"], shape, "machine_passed", "passed"
    )
    service.run(batch["id"], task["task_id"], passed["export_id"])
    assert (
        service.status(batch["id"], task["task_id"], passed["export_id"])["counts"][
            "samples"
        ]
        == 1
    )


def test_changed_evidence_after_submission_never_publishes_a_partial_dataset(
    export_setup,
):
    control, batch, task, root, _ = export_setup
    service = TrainingExports(control, actor="user")
    job = service.request(
        batch["id"], task["task_id"], "messages", "processed", "export"
    )
    (root / "input.jsonl").write_text('{"text":"different evidence"}\n')
    service.run(batch["id"], task["task_id"], job["export_id"])
    assert (
        service.status(batch["id"], task["task_id"], job["export_id"])["state"]
        == "failed"
    )
    assert (
        "changed after export submission"
        in service.artifact(
            batch["id"], task["task_id"], job["export_id"], "log"
        ).read_text()
    )
    with pytest.raises(Conflict):
        service.artifact(batch["id"], task["task_id"], job["export_id"], "dataset")
    assert not (
        service.directory(batch["id"], task["task_id"], job["export_id"])
        / "export.json"
    ).exists()


@pytest.mark.parametrize(
    "corruption", ["checkpoint", "contract", "original", "source_count"]
)
def test_completed_receipt_does_not_replace_validated_workflow_evidence(
    export_setup, corruption
):
    control, batch, task, _, receipt = export_setup
    output = Path(receipt["directory"])
    manifest = json.loads((output / "run.json").read_text())
    if corruption == "checkpoint":
        manifest["checkpoint"]["offset"] -= 1
    elif corruption == "source_count":
        manifest["checkpoint"]["source_groups"] += 1
    else:
        review = json.loads((output / "review.jsonl").read_text())
        if corruption == "contract":
            review["workflow_contract"] = "difficulty_routed_qa.v5"
        else:
            review["raw_input"]["text"] = "another original source"
        data = (json.dumps(review, ensure_ascii=False) + "\n").encode()
        (output / "review.jsonl").write_bytes(data)
        manifest["artifacts"]["review.jsonl"]["bytes"] = len(data)
        manifest["checkpoint"]["offset"] = len(data)
    (output / "run.json").write_text(json.dumps(manifest))
    service = TrainingExports(control, actor="user")
    job = service.request(
        batch["id"], task["task_id"], "messages", "processed", "export"
    )
    service.run(batch["id"], task["task_id"], job["export_id"])
    assert (
        service.status(batch["id"], task["task_id"], job["export_id"])["state"]
        == "failed"
    )
    with pytest.raises(Conflict):
        service.artifact(batch["id"], task["task_id"], job["export_id"], "dataset")
    log = service.artifact(
        batch["id"], task["task_id"], job["export_id"], "log"
    ).read_text()
    assert {
        "checkpoint": "checkpoint is uncommitted",
        "source_count": "count/checkpoint",
        "contract": "frozen topic-catalog contract difficulty_routed_qa.v6",
        "original": "raw_input differs",
    }[corruption] in log


@pytest.mark.parametrize("limit", ["max_samples", "max_output_bytes", "max_row_bytes"])
def test_export_limits_fail_the_whole_publication_instead_of_truncating(
    export_setup, limit
):
    control, batch, task, _, _ = export_setup
    control.training_export_limits = ExportLimits(**{limit: 2})
    service = TrainingExports(control, actor="user")
    job = service.request(
        batch["id"], task["task_id"], "messages", "processed", "limited"
    )
    service.run(batch["id"], task["task_id"], job["export_id"])
    assert (
        service.status(batch["id"], task["task_id"], job["export_id"])["state"]
        == "failed"
    )
    directory = service.directory(batch["id"], task["task_id"], job["export_id"])
    assert not (directory / "export.json").exists()
    with pytest.raises(Conflict):
        service.artifact(batch["id"], task["task_id"], job["export_id"], "dataset")
    assert (
        "limit"
        in service.artifact(
            batch["id"], task["task_id"], job["export_id"], "log"
        ).read_text()
    )


@pytest.mark.parametrize("corruption", ["missing_marker", "dataset", "identity"])
def test_completed_registry_row_cannot_authorize_damaged_exports(
    export_setup, corruption
):
    control, batch, task, _, _ = export_setup
    service = TrainingExports(control, actor="user")
    job = service.request(
        batch["id"], task["task_id"], "messages", "processed", "export"
    )
    service.run(batch["id"], task["task_id"], job["export_id"])
    directory = service.directory(batch["id"], task["task_id"], job["export_id"])
    marker = directory / "export.json"
    if corruption == "missing_marker":
        marker.unlink()
    elif corruption == "dataset":
        (directory / "dataset.jsonl").write_text("changed\n")
    else:
        manifest = json.loads(marker.read_text())
        manifest["task_id"] = "another-task"
        marker.write_text(json.dumps(manifest))
    with pytest.raises(IntegrityError):
        service.artifact(batch["id"], task["task_id"], job["export_id"], "dataset")


def test_interrupted_export_is_not_replayed_and_verified_marker_recovers_success(
    export_setup, monkeypatch
):
    control, batch, task, _, _ = export_setup
    service = TrainingExports(control, actor="user")
    job = service.request(
        batch["id"], task["task_id"], "messages", "processed", "export"
    )
    service.store.update(
        batch["id"],
        task["task_id"],
        job["export_id"],
        state="running",
        worker_owner={"unknown": "test"},
    )
    monkeypatch.setattr(
        "lightrag.batch_control.training_export.service.owner_gone", lambda _: True
    )
    assert (
        service.status(batch["id"], task["task_id"], job["export_id"])["state"]
        == "interrupted"
    )
    service.run(batch["id"], task["task_id"], job["export_id"])
    assert not (
        service.directory(batch["id"], task["task_id"], job["export_id"]) / "spec.json"
    ).exists()
    retry = service.request(
        batch["id"], task["task_id"], "messages", "processed", "explicit-retry"
    )
    service.run(batch["id"], task["task_id"], retry["export_id"])
    assert (
        service.status(batch["id"], task["task_id"], retry["export_id"])["state"]
        == "completed"
    )
    service.store.update(
        batch["id"], task["task_id"], retry["export_id"], state="interrupted"
    )
    assert (
        service.status(batch["id"], task["task_id"], retry["export_id"])["state"]
        == "completed"
    )


async def test_export_http_contract_permissions_isolation_and_authenticated_files(
    export_setup,
):
    control, batch, task, _, _ = export_setup
    peer = control.create("user", "Other batch", "peer")
    policy = RuntimePolicy(
        Path(__file__).resolve().parents[2], Path(sys.executable), storage_mode="local"
    )
    deployment = BatchDeployment(control, policy)
    app = FastAPI()
    app.include_router(create_control_routes(deployment, lambda: "user"))
    base = f"/data-batches/{batch['id']}/tasks/{task['task_id']}/training-exports"
    body = {"format": "messages", "selection": "processed", "request_id": "http-export"}
    try:
        async with AsyncClient(
            transport=ASGITransport(app), base_url="http://test"
        ) as client:
            control.authorize = lambda actor, action, resource: (
                action != "export_training"
            )
            assert (await client.post(base, json=body)).status_code == 403
            assert (await client.get(base)).json() == {"items": []}
            control.authorize = lambda *args: True
            assert (
                await client.post(base, json={**body, "format": "unknown"})
            ).status_code == 422
            response = await client.post(base, json=body)
            assert response.status_code == 202, response.text
            exported = response.json()["export_id"]
            assert "spec" not in response.json()
            assert str(control.paths.config_root) not in response.text
            await asyncio.gather(*list(deployment.export_background.values()))
            status = await client.get(base + "/" + exported)
            assert status.json()["state"] == "completed", status.text
            assert (await client.post(base, json=body)).json()["export_id"] == exported
            assert (
                await client.post(base, json={**body, "selection": "machine_passed"})
            ).status_code == 409
            for name in ("dataset", "provenance", "manifest"):
                download = await client.get(base + f"/{exported}/files/{name}")
                assert download.status_code == 200
                assert "attachment" in download.headers["content-disposition"]
                assert download.content
            other = base.replace(batch["id"], peer["id"])
            assert (await client.get(other)).status_code == 404
            assert (
                await client.get(other + f"/{exported}/files/dataset")
            ).status_code == 404
            control.authorize = lambda *args: False
            assert (
                await client.get(base + f"/{exported}/files/dataset")
            ).status_code == 403
    finally:
        if deployment.export_background:
            await asyncio.gather(*list(deployment.export_background.values()))


@pytest.mark.parametrize("export_setup", ["none-passed"], indirect=True)
def test_no_machine_passed_versions_produces_a_verified_empty_export(export_setup):
    control, batch, task, _, _ = export_setup
    service = TrainingExports(control, actor="user")
    job = service.request(
        batch["id"], task["task_id"], "messages", "machine_passed", "empty"
    )
    service.run(batch["id"], task["task_id"], job["export_id"])
    status = service.status(batch["id"], task["task_id"], job["export_id"])
    assert status["state"] == "completed", status
    assert status["counts"]["samples"] == 0
    assert status["counts"]["skipped_policy"] == 3
    assert status["counts"]["sources_processed"] == 1
    for name in ("dataset", "provenance"):
        assert (
            service.artifact(
                batch["id"], task["task_id"], job["export_id"], name
            ).read_bytes()
            == b""
        )


def test_export_remains_bound_to_original_revision_after_batch_settings_change(
    export_setup,
):
    control, batch, task, _, _ = export_setup
    current = control.read("user", batch["id"])[2].model_dump()
    current["retrieval"]["ordinary"]["top_k"] = 41
    saved = control.save(
        "user",
        batch["id"],
        batch["active"],
        Configuration.model_validate(current),
        "change-settings",
    )
    assert not saved["requires_restart"]
    assert control.registry.get(batch["id"])["active"] == saved["revision"]
    assert saved["revision"] != task["config_revision_id"]
    service = TrainingExports(control, actor="user")
    job = service.request(
        batch["id"], task["task_id"], "messages", "processed", "original-revision"
    )
    service.run(batch["id"], task["task_id"], job["export_id"])
    status = service.status(batch["id"], task["task_id"], job["export_id"])
    assert status["state"] == "completed", status
    assert status["config_revision_id"] == task["config_revision_id"]


@pytest.mark.parametrize("export_setup", ["categories"], indirect=True)
@pytest.mark.parametrize("shape", ["messages", "alpaca", "sharegpt"])
def test_official_category_catalog_contract_exports_without_changing_workflow(
    export_setup, shape
):
    control, batch, task, _, receipt = export_setup
    review = Path(receipt["directory"]) / "review.jsonl"
    before = review.read_bytes()
    assert json.loads(before)["workflow_contract"] == "difficulty_routed_qa.v5"
    service = TrainingExports(control, actor="user")
    exported = service.request(
        batch["id"], task["task_id"], shape, "processed", "category-export"
    )
    service.run(batch["id"], task["task_id"], exported["export_id"])
    status = service.status(batch["id"], task["task_id"], exported["export_id"])
    assert status["state"] == "completed", status
    assert status["counts"]["samples"] == 3
    assert review.read_bytes() == before
    assert (
        len(
            service.artifact(
                batch["id"], task["task_id"], exported["export_id"], "dataset"
            )
            .read_text()
            .splitlines()
        )
        == 3
    )
