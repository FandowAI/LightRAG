"""The actual SDK and official workflow Serving share admission across interpreters."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from lightrag.batch_control.provider_quota import hooks
from lightrag.batch_control.provider_quota.core import QuotaStore

pytestmark = pytest.mark.offline

SCRIPT = """
import asyncio,json,os,sys,time,selectors
from pathlib import Path
sys.path.insert(0,sys.argv[1])
original=selectors.EpollSelector.select
selectors.EpollSelector.select=lambda self,timeout=None: original(self,.01 if timeout is None else min(timeout,.01))
from hooks import install_httpx,install_requests
root=Path(sys.argv[2]); label=sys.argv[3]
spec={'registry':str(root/'quota.sqlite3'),'batch_id':label,'task_id':label,'endpoints':['https://model.invalid/v1']}
body={'id':'offline','object':'chat.completion','created':1,'model':'offline','choices':[{'index':0,'message':{'role':'assistant','content':'Original answer'},'finish_reason':'stop'}],'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}}
def gate():
    (root/(label+'.entered')).write_text('HTTP attempt admitted')
    deadline=time.monotonic()+20
    while not (root/(label+'.release')).exists():
        if time.monotonic()>deadline: raise TimeoutError('Test transport was not released')
        time.sleep(.02)
if label=='rag':
    import httpx
    from openai import AsyncOpenAI
    install_httpx(spec)
    async def transport(request):
        gate()
        return httpx.Response(200,json=body)
    async def run():
        async with AsyncOpenAI(api_key='shared',base_url='https://model.invalid/v1',http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))) as client:
            result=await client.chat.completions.create(model='offline',messages=[{'role':'user','content':'Original question'}])
            assert result.choices[0].message.content=='Original answer'
    asyncio.run(run())
else:
    import requests
    from data_studio.models.dataflow_serving import ContentOnlyServing
    install_requests(spec)
    def transport(adapter,request,**kwargs):
        gate()
        response=requests.Response(); response.status_code=200
        response.url=request.url; response.request=request
        response._content=json.dumps(body).encode()
        return response
    requests.adapters.HTTPAdapter.send=transport
    os.environ['OFFLINE_KEY']='shared'
    serving=ContentOnlyServing(api_url='https://model.invalid/v1/chat/completions',model_name='offline',key_name_of_api_key='OFFLINE_KEY',max_workers=1,max_retries=1)
    try:
        batch=serving.generate_batch(['Original question'])
        assert batch.responses=={0:'Original answer'}
        assert batch.metrics[0]['http_attempts']==1
    finally: serving.cleanup()
"""


def test_sdk_and_official_serving_serialize_in_distinct_processes(tmp_path):
    workflow = Path(__file__).resolve().parents[3] / "data-work-flow"
    workflow_python = workflow / ".venv-qa/bin/python"
    if not workflow_python.is_file():
        pytest.skip("Selected workflow interpreter is unavailable")
    store = QuotaStore(tmp_path / "quota.sqlite3")
    store.configure({"defaults": {"max_concurrent": 1}})
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ
    }
    environment.update(PYTHONPATH=str(workflow / "src"), PYTHONDONTWRITEBYTECODE="1")
    children = []

    def start(python, label):
        with (tmp_path / (label + ".log")).open("wb") as log:
            child = subprocess.Popen(
                [
                    str(python),
                    "-c",
                    SCRIPT,
                    str(Path(hooks.__file__).parent),
                    str(tmp_path),
                    label,
                ],
                cwd=tmp_path,
                env=environment,
                stdout=log,
                stderr=log,
            )
        children.append(child)

    def wait(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            for child in children:
                assert child.poll() in (None, 0), {
                    path.name: path.read_text()[-4000:]
                    for path in tmp_path.glob("*.log")
                }
            time.sleep(0.02)
        pytest.fail("Expected provider admission transition was not observed")

    try:
        start(sys.executable, "rag")
        wait(lambda: (tmp_path / "rag.entered").exists())
        start(workflow_python, "workflow")
        wait(lambda: bool(store.summary("workflow")["items"]))
        assert store.summary("workflow")["items"][0]["queued"] == 1
        assert store.summary("rag")["items"][0]["active"] == 1
        assert not (tmp_path / "workflow.entered").exists()
        (tmp_path / "rag.release").touch()
        wait(lambda: (tmp_path / "workflow.entered").exists())
        assert store.summary("rag")["items"][0]["finished"] == 1
        assert store.summary("workflow")["items"][0]["active"] == 1
        (tmp_path / "workflow.release").touch()
        assert [child.wait(timeout=10) for child in children] == [0, 0]
        with store.database() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT id,pool,batch,state,tokens,granted,finished FROM provider_requests ORDER BY sequence"
                )
            ]
        assert len({row["pool"] for row in rows}) == 1
        assert [row["state"] for row in rows] == ["finished", "finished"]
        assert [row["tokens"] for row in rows] == [5, 5]
        assert rows[0]["finished"] <= rows[1]["granted"]
        if os.getenv("LIGHTRAG_PROVIDER_VALIDATION_RECORD"):
            evidence = {
                "schema_version": 1,
                "limits": {"max_concurrent": 1},
                "queued_workflow_observed": True,
                "distinct_interpreters": [sys.executable, str(workflow_python)],
                "attempts": rows,
                "external_network_requests": 0,
                "both_original_responses_preserved": True,
                "both_children_stopped": True,
            }
            path = Path(os.environ["LIGHTRAG_PROVIDER_VALIDATION_RECORD"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2))
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
