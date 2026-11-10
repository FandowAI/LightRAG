"""A real legacy child releases its data before a verified managed child adopts it."""

# ruff: noqa: F811

import json
import os
from pathlib import Path
import subprocess
import time

import httpx
import networkx as nx
import numpy as np
from nano_vectordb import NanoVectorDB
import pytest

from lightrag.batch_control.adoption import AdoptionService, LegacySource
from lightrag.batch_control.adoption_files import verify_backup
from lightrag.batch_control.bindings import BindingStore
from lightrag.batch_control.resources import digest
from lightrag.batch_control.runtime_config import clean_environment, compile_startup
from lightrag.batch_control.runtime_process import atomic_json
from lightrag.batch_control.storage import Conflict
from lightrag.batch_control.writer_claim import WorkspaceClaim
from tests.batch_control.test_runtime import runtime_setup  # noqa: F401

pytestmark = pytest.mark.offline


def wait_for(predicate, child, log, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if child.poll() is not None:
            pytest.fail("Legacy fixture exited: " + log.read_text()[-6000:])
        time.sleep(0.02)
    pytest.fail("Legacy fixture timed out: " + log.read_text()[-6000:])


def test_actual_legacy_transfer_preserves_data_backups_and_writer_ownership(
    runtime_setup, tmp_path
):
    control, policy = runtime_setup
    seed = control.create("user", "Source configuration", "seed")
    owner, _, document, _, manifest = control.read("user", seed["id"])
    startup, secrets = compile_startup(
        control, "user", owner, seed["active"], document, manifest, policy
    )
    directory = tmp_path / "source-process"
    directory.mkdir()
    root = tmp_path / "existing-storage/legacy"
    root.mkdir(parents=True)
    inputs = tmp_path / "existing-inputs"
    (inputs / "legacy").mkdir(parents=True)
    (inputs / "legacy/original.md").write_text("original evidence")
    docs = {
        "doc-existing": {"content": "original evidence", "file_path": "original.md"},
        "doc-failed": {"content": "failed evidence", "file_path": "failed.md"},
    }
    statuses = {
        key: {
            "content_summary": value["content"],
            "content_length": len(value["content"]),
            "file_path": value["file_path"],
            "status": "failed" if key == "doc-failed" else "processed",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "chunks_count": 0,
            "chunks_list": [],
            "error_msg": "Retain original failure" if key == "doc-failed" else None,
            "metadata": {"kg_write_state": "pre_graph"},
        }
        for key, value in docs.items()
    }
    (root / "kv_store_full_docs.json").write_text(json.dumps(docs))
    (root / "kv_store_doc_status.json").write_text(json.dumps(statuses))
    graph = nx.Graph()
    graph.add_node(
        "Original entity", entity_type="concept", description="Original graph evidence"
    )
    nx.write_graphml(graph, root / "graph_chunk_entity_relation.graphml")
    vectors = NanoVectorDB(
        embedding_dim=8, storage_file=str(root / "vdb_entities.json")
    )
    vectors.upsert(
        [
            {
                "__id__": "original-entity",
                "__vector__": np.array([1.0, 0, 0, 0, 0, 0, 0, 0]),
                "entity_name": "Original entity",
                "content": "Original vector evidence",
            }
        ]
    )
    vectors.save()
    environment = {
        **clean_environment(policy.project_directory),
        **startup["environment"],
        **secrets,
    }
    for key in ("LIGHTRAG_MANAGED_BATCH_ID", "LIGHTRAG_BATCH_REGISTRY"):
        environment.pop(key)
    environment.update(
        WORKSPACE="legacy",
        WORKING_DIR=str(root.parent),
        INPUT_DIR=str(inputs),
        LOG_DIR=str(directory / "logs"),
        LIGHTRAG_API_KEY="synthetic-source-key",
        EXTRACT_OPENAI_LLM_TEMPERATURE="0.25",
        EXTRACT_OPENAI_LLM_MAX_TOKENS="8192",
        OPENAI_LLM_EXTRA_BODY='{"enable_thinking":false}',
        ENTITY_TYPE_PROMPT_FILE="fando_enterprise.yml",
        ENTITY_EXTRACTION_USE_JSON="true",
        LIGHTRAG_PARSER="*:native-teP,*:legacy-R",
    )
    environment.pop("LIGHTRAG_BATCH_OUTPUT_DIR", None)
    helper = Path(__file__).with_name("legacy_source_fixture.py")
    command = [str(policy.python), str(helper), str(directory)]
    log = directory / "source.log"
    with log.open("wb") as stream:
        child = subprocess.Popen(
            command, cwd=directory, env=environment, stdout=stream, stderr=stream
        )
    service = None
    operation = None
    try:
        wait_for(lambda: (directory / "ready.json").exists(), child, log)
        credential = directory / "source.env"
        credential.write_text("LIGHTRAG_API_KEY=synthetic-source-key\n")
        control.catalog.credentials["source-key"] = credential
        source = LegacySource(
            url="http://127.0.0.1:9",
            workspace="legacy",
            working_directory=str(root.parent),
            input_directory=str(inputs),
            command_directory=str(directory),
            command_sha256=digest(command),
            credential_id="source-key",
            resources={"PROMPT_DIR": environment["PROMPT_DIR"]},
        )

        def transport(request):
            response_file = directory / "response.json"
            response_file.unlink(missing_ok=True)
            atomic_json(
                directory / "request.json",
                {
                    "method": request.method,
                    "path": request.url.path,
                    "headers": dict(request.headers),
                    "body": json.loads(request.content) if request.content else None,
                },
            )
            wait_for(response_file.exists, child, log)
            response = json.loads(response_file.read_text())
            return httpx.Response(response["status"], text=response["body"])

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            service = AdoptionService(
                control, policy, {"old": source}, actor="user", client=client
            )
            record = service.request("old", "Existing legacy data", "actual-transfer")
            operation = record["operation_id"]
            for _ in range(40):
                record = service.step(operation)
                assert not record.get("error"), record
                if record["phase"] == "ready":
                    break
                time.sleep(0.05)
            assert record["phase"] == "ready"
            assert child.wait(timeout=15) == 0
            assert (directory / "stopped.json").exists()
            assert json.loads((root / "kv_store_full_docs.json").read_text()) == docs
            assert (
                json.loads((root / "kv_store_doc_status.json").read_text()) == statuses
            )
            backed = verify_backup(record["backup_directory"])
            assert backed["workspace"] == "legacy"
            assert (
                Path(record["backup_directory"]) / "inputs/original.md"
            ).read_text() == "original evidence"
            binding = BindingStore(control.registry).get(record["batch_id"])
            assert binding["state"] == "adopted"
            assert binding["binding"]["graph"]["nodes"] == 1
            assert (
                nx.read_graphml(root / "graph_chunk_entity_relation.graphml").nodes[
                    "Original entity"
                ]["description"]
                == "Original graph evidence"
            )
            assert binding["binding"]["vectors"]["entities"]["rows"] == 1
            assert binding["binding"]["document_states"]["failed"] == 1
            assert set(binding["binding"]["vectors"]) == {
                "entities",
                "relationships",
                "chunks",
            } | set(binding["binding"]["auxiliary_collections"])
            proof = service.runtime.backend.observe(
                service.runtime.store.get(record["batch_id"])
            )
            assert proof["role_provider_options"]["extract"]["temperature"] == 0.25
            assert proof["role_provider_options"]["extract"]["max_tokens"] == 8192
            assert proof["role_provider_options"]["query"]["extra_body"] == {
                "enable_thinking": False
            }
            evidence = {
                "schema_version": 1,
                "storage_mode": "local",
                "external_model_requests": 0,
                "operation": service.public(operation),
                "lease": record["lease"],
                "binding": binding,
                "source_stopped": True,
                "backup_manifest": backed,
                "original_document_status_preserved": True,
                "observation": {
                    key: value for key, value in proof.items() if key != "capability"
                },
            }
            service.runtime.stop(record["batch_id"])
            with pytest.raises(Conflict, match="different batch registry"):
                with WorkspaceClaim(root.parent, "legacy"):
                    pytest.fail("Old service reopened adopted data")
            evidence["managed_child_stopped"] = (
                service.runtime.backend.ownership(
                    service.runtime.store.get(record["batch_id"])
                )
                == "gone"
            )
            assert evidence["managed_child_stopped"]
            if os.getenv("LIGHTRAG_TRANSFER_VALIDATION_RECORD"):
                atomic_json(
                    Path(os.environ["LIGHTRAG_TRANSFER_VALIDATION_RECORD"]), evidence
                )
    finally:
        if service is not None and operation is not None:
            record = service.store.get(operation)
            runtime = service.runtime.store.get(record["batch_id"])
            if runtime and service.runtime.backend.ownership(runtime) == "owned":
                service.runtime.stop(record["batch_id"])
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=30)
