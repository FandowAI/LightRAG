"""Exercise the installed workflow's actual Serving without editing its checkout."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from lightrag.batch_control.provider_quota import hooks
from lightrag.batch_control.provider_quota.core import QuotaStore

pytestmark = pytest.mark.offline


def test_official_serving_uses_the_portable_shared_admission_adapter(tmp_path):
    project = Path(__file__).resolve().parents[3] / "data-work-flow"
    python = project / ".venv-qa/bin/python"
    if not python.is_file():
        pytest.skip("Selected data-work-flow interpreter is unavailable")
    store = QuotaStore(tmp_path / "quota.sqlite3")
    store.configure({"defaults": {"max_concurrent": 1}})
    spec = {
        "registry": str(store.path),
        "batch_id": "workflow-batch",
        "task_id": "workflow-task",
        "endpoints": ["https://model.invalid/v1"],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    script = """
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from hooks import install_requests
install_requests(json.loads(Path(sys.argv[2]).read_text()))
import requests
from data_studio.models.dataflow_serving import ContentOnlyServing
sent=[]
def network(adapter,request,**kwargs):
    sent.append(json.loads(request.body))
    time.sleep(.05)
    response=requests.Response()
    response.status_code=200
    response.request=request
    response.url=request.url
    response._content=json.dumps({'choices':[{'message':{'content':'original answer'}}], 'usage':{'prompt_tokens':8,'completion_tokens':4}}).encode()
    return response
requests.adapters.HTTPAdapter.send=network
os.environ['OFFLINE_KEY']='synthetic-key'
service=ContentOnlyServing(api_url='https://model.invalid/v1/chat/completions',model_name='official-adapter-fixture',key_name_of_api_key='OFFLINE_KEY',max_workers=2,max_retries=1,connect_timeout=1,read_timeout=1)
try:
    batch=service.generate_batch(['first source','second source'],'Original instructions')
    assert batch.responses == {0:'original answer',1:'original answer'}
    assert all(value['http_attempts']==1 and value['usage']=={'prompt_tokens':8,'completion_tokens':4} for value in batch.metrics.values())
    assert len(sent)==2 and all(row['messages'][0]['content']=='Original instructions' for row in sent)
    print(json.dumps({'responses':2,'actual_http_attempts':2,'model_requests_over_network':0,'upstream_source_edited':False}))
finally:
    service.cleanup()
"""
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ
    }
    environment["PYTHONPATH"] = str(project / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [str(python), "-c", script, str(Path(hooks.__file__).parent), str(spec_path)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr[-5000:]
    assert store.summary("workflow-batch", "workflow-task")["items"][0]["finished"] == 2
    with store.database() as db:
        assert [
            row[0] for row in db.execute("SELECT tokens FROM provider_requests")
        ] == [12, 12]
