import json
import sys

import pytest
import yaml


@pytest.fixture
def query_result():
    return {
        "status": "success",
        "message": "ok",
        "data": {
            "entities": [
                {
                    "entity_name": "费用报销",
                    "entity_type": "PROCESS",
                    "description": "申请报销的流程",
                    "source_id": "chunk-1",
                    "file_path": "a.docx",
                }
            ],
            "relationships": [
                {
                    "src_id": "费用报销",
                    "tgt_id": "发票",
                    "description": "必须提交",
                    "weight": 1,
                    "source_id": "chunk-1",
                }
            ],
            "chunks": [
                {
                    "content": "  员工必须提交合法发票。\n${literal} {literal}",
                    "chunk_id": "chunk-1",
                    "reference_id": "1",
                    "file_path": "a.docx",
                }
            ],
            "references": [{"reference_id": "1", "file_path": "a.docx"}],
        },
        "metadata": {"query_mode": "mix"},
    }


@pytest.fixture
def bridge(tmp_path, query_result):
    from lightrag.integrations.data_workflow.service import DataWorkflowService

    source = tmp_path / "source.json"
    source.write_text(json.dumps(query_result, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "data-work-flow"
    package = project / "src/data_studio"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    # A real child process with the CLI's receipt/manifest protocol; no network.
    (package / "__main__.py").write_text(
        """
import json, sys, time
from pathlib import Path
import yaml
args = sys.argv
config = yaml.safe_load(Path(args[args.index('--config') + 1]).read_text())
params = config['workflow']['parameters']
time.sleep(params.get('delay', 0))
rows = [json.loads(x) for x in Path(config['input']['paths'][0]).read_text().splitlines()]
assert all(set(row) == {'text'} and isinstance(row['text'], str) for row in rows)
directory = Path(config['output']['directory']) / 'run-fixture'
directory.mkdir(parents=True)
status = params.get('test_status', 'completed')
summary = {'sources': len(rows), 'work_items': len(rows) * 3}
result = {'run_id': directory.name, 'directory': str(directory), 'status': status, 'summary': summary}
(directory / 'run.json').write_text(json.dumps({**result, 'checkpoint': {'batch_id': 1}}))
(directory / 'review.jsonl').write_text(''.join(json.dumps(row) + '\\n' for row in rows))
print(json.dumps(result))
sys.exit({'completed': 0, 'completed_with_errors': 1, 'failed': 2}[status])
""",
        encoding="utf-8",
    )
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "input": {"paths": ["unused.jsonl"]},
                "output": {"directory": "unused"},
                "workflow": {"name": "source_review", "parameters": {}},
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "bridge.yaml"
    document = {
        "version": 1,
        "project_directory": str(project),
        "python": sys.executable,
        "workflow_config": str(recipe),
        "jobs_directory": str(tmp_path / "jobs"),
        "input": {"paths": [str(source)]},
    }
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    return DataWorkflowService("test-workspace", config), config, source, recipe
