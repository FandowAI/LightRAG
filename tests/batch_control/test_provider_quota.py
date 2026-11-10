"""Model attempts across batches share slots and retain uncertain token charges."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx
from openai import AsyncOpenAI, RateLimitError
import pytest
import requests

from lightrag.batch_control.provider_quota import hooks
from lightrag.batch_control.provider_quota.core import (
    Policy,
    QuotaError,
    QuotaStore,
    endpoint,
)

pytestmark = pytest.mark.offline


@pytest.fixture
def quota(tmp_path, monkeypatch):
    now = [1000.0]
    store = QuotaStore(tmp_path / "quota.sqlite3", clock=lambda: now[0])
    store.configure({"defaults": {"max_concurrent": 1}})
    monkeypatch.setattr(
        httpx.AsyncClient,
        "_send_single_request",
        httpx.AsyncClient._send_single_request,
    )
    monkeypatch.setattr(requests.Session, "send", requests.Session.send)
    return store, now


def enqueue(store, batch="batch-a", tokens=30):
    return store.enqueue(
        "https://model.invalid/v1/chat/completions",
        "API_SUPER_SECRET",
        batch,
        "task-" + batch,
        tokens,
    )


def test_fifo_shared_credentials_and_no_elapsed_time_slot_release(quota):
    store, now = quota
    first, second, third = enqueue(store), enqueue(store, "batch-b"), enqueue(store)
    assert store.poll(first)["state"] == "granted"
    now[0] += 3600
    assert store.poll(second)["state"] == "rejected"  # Queue deadline, not a free slot.
    fourth = enqueue(store, "batch-b")
    assert store.poll(fourth)["state"] == "queued"
    store.finish(first)
    assert store.poll(third)["state"] == "rejected"
    assert store.poll(fourth)["state"] == "granted"
    fifth, sixth = enqueue(store), enqueue(store, "batch-b")
    store.finish(fourth)
    assert store.poll(sixth)["state"] == "queued"
    assert store.poll(fifth)["state"] == "granted"
    assert "API_SUPER_SECRET" not in store.path.read_bytes().decode(errors="ignore")


def test_token_reservations_survive_long_calls_and_unknown_outcomes(quota):
    store, now = quota
    store.configure({"defaults": {"max_concurrent": 3, "tokens_per_minute": 100}})
    first, second = enqueue(store, tokens=80), enqueue(store, "batch-b", tokens=40)
    assert store.poll(first)["state"] == "granted"
    now[0] += 61
    assert store.poll(second)["state"] == "queued"
    store.finish(first)
    assert store.poll(second)["state"] == "queued"
    now[0] += 61
    assert store.poll(second)["state"] == "granted"
    store.finish(second, tokens=10)
    third = enqueue(store, tokens=90)
    assert store.poll(third)["state"] == "granted"
    oversize = enqueue(store, tokens=101)
    assert store.poll(oversize)["state"] == "rejected"


def test_rpm_and_reconfiguration_preserve_existing_charges(quota):
    store, now = quota
    store.configure({"defaults": {"requests_per_minute": 1}})
    first, second = enqueue(store), enqueue(store, "batch-b")
    assert store.poll(first)["state"] == "granted"
    store.finish(first, tokens=1)
    store.configure({"defaults": {"max_concurrent": 5, "requests_per_minute": 1}})
    assert store.poll(second)["state"] == "queued"
    now[0] += 61
    assert store.poll(second)["state"] == "granted"


def test_confirmed_owner_exit_recovers_slots_but_retains_token_debt(quota):
    store, now = quota
    store.configure({"defaults": {"max_concurrent": 1, "tokens_per_minute": 100}})
    first = enqueue(store, tokens=90)
    store.poll(first)
    with store.database(write=True) as db:
        owner = json.loads(
            db.execute(
                "SELECT owner FROM provider_requests WHERE id=?", (first,)
            ).fetchone()[0]
        )
        owner["pid"] = 999999999
        db.execute(
            "UPDATE provider_requests SET owner=? WHERE id=?",
            (json.dumps(owner), first),
        )
    now[0] += 2
    second = enqueue(store, "batch-b", tokens=20)
    assert store.poll(second)["state"] == "queued"
    assert store.summary("batch-a")["items"][0]["orphaned"] == 1
    now[0] += 61
    assert store.poll(second)["state"] == "granted"


@pytest.mark.parametrize("value", [0, -1, True, float("nan")])
def test_invalid_capacities_fail_before_requests(value):
    with pytest.raises(ValueError):
        Policy(max_concurrent=value)


async def test_httpx_stream_owns_slot_until_close_and_uses_reported_usage(quota):
    store, _ = quota
    spec = {
        "registry": str(store.path),
        "batch_id": "batch-a",
        "endpoints": ["https://model.invalid/v1"],
    }
    hooks.install_httpx(spec)
    sent = []

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'
            yield b'data: {"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'

        async def aclose(self):
            pass

    async def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Bytes()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        first = await client.send(
            client.build_request(
                "POST",
                "https://model.invalid/v1/chat/completions",
                headers={"Authorization": "Bearer key"},
                json={"model": "a", "messages": []},
            ),
            stream=True,
        )
        second = asyncio.create_task(
            client.post(
                "https://model.invalid/v1/chat/completions",
                headers={"Authorization": "Bearer key"},
                json={"model": "b", "messages": []},
            )
        )
        await asyncio.sleep(0.1)
        assert len(sent) == 1
        assert store.summary("batch-a")["items"][0]["active"] == 1
        await first.aread()
        await asyncio.wait_for(second, 3)
    with store.database() as db:
        rows = db.execute("SELECT tokens,state FROM provider_requests").fetchall()
    assert [(row["tokens"], row["state"]) for row in rows] == [
        (7, "finished"),
        (7, "finished"),
    ]


async def test_waiting_cancellation_never_sends_and_frees_queue(quota):
    store, _ = quota
    spec = {
        "registry": str(store.path),
        "batch_id": "batch-a",
        "endpoints": ["https://model.invalid/v1"],
    }
    hooks.install_httpx(spec)
    held = store.enqueue("https://model.invalid/v1", "key", "batch-b", "task-b", 1)
    assert store.poll(held)["state"] == "granted"
    sent = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: sent.append(True))
    ) as client:
        pending = asyncio.create_task(
            client.post(
                "https://model.invalid/v1/chat/completions",
                headers={"Authorization": "Bearer key"},
                json={"model": "a", "messages": []},
            )
        )
        await asyncio.sleep(0.1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert sent == []
    assert store.summary("batch-a")["items"][0]["cancelled"] == 1


async def test_openai_rejection_is_local_and_does_not_retry(quota):
    store, _ = quota
    store.configure({"defaults": {"tokens_per_minute": 10}})
    hooks.install_httpx(
        {
            "registry": str(store.path),
            "batch_id": "batch-a",
            "endpoints": ["https://model.invalid/v1"],
        }
    )
    sent = []
    async with AsyncOpenAI(
        api_key="key",
        base_url="https://model.invalid/v1",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: sent.append(True))
        ),
    ) as client:
        with pytest.raises(
            RateLimitError, match="No upstream request was sent"
        ) as caught:
            await client.chat.completions.create(model="a", messages=[])
    assert caught.value.code == "budget_exceeded"
    assert sent == []
    assert store.summary("batch-a")["items"][0]["rejected"] == 1


def test_requests_and_httpx_share_pool_and_cap_unbounded_output(quota, monkeypatch):
    store, _ = quota
    store.configure(
        {"defaults": {"tokens_per_minute": 10000, "default_output_tokens": 12}}
    )
    spec = {
        "registry": str(store.path),
        "batch_id": "workflow-batch",
        "task_id": "workflow-task",
        "endpoints": ["https://model.invalid/v1/chat/completions"],
    }
    captured = []

    def network(adapter, request, **kwargs):
        captured.append(json.loads(request.body))
        response = requests.Response()
        response.status_code, response.url, response.request = 200, request.url, request
        response._content = b'{"usage":{"total_tokens":9},"choices":[]}'
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", network)
    hooks.install_requests(spec)
    requests.post(
        "https://model.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer shared"},
        json={"model": "a", "messages": []},
    )
    assert captured[0]["max_tokens"] == 12
    assert store.summary("workflow-batch", "workflow-task")["items"][0]["finished"] == 1
    other = store.enqueue(
        "https://model.invalid/v1", "shared", "rag-batch", "rag-task", 1
    )
    with store.database() as db:
        assert (
            len({row[0] for row in db.execute("SELECT pool FROM provider_requests")})
            == 1
        )
    store.finish(other)
    with pytest.raises(QuotaError, match="outside"):
        requests.post(
            "https://other.invalid/v1/chat/completions",
            json={"model": "a", "messages": []},
        )
    assert len(captured) == 1


def test_two_real_processes_serialize_provider_attempts(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    store.configure({"defaults": {"max_concurrent": 1}})
    module = Path(hooks.__file__).parent
    script = """
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from core import QuotaStore
store=QuotaStore(sys.argv[2])
root=Path(sys.argv[3]); label=sys.argv[4]
identity=store.enqueue("https://model.invalid/v1", "shared", label, label, 1)
while store.poll(identity)["state"] != "granted": time.sleep(.02)
(root/(label+".granted")).write_text(identity)
while not (root/(label+".release")).exists(): time.sleep(.02)
store.finish(identity,tokens=1)
"""
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(module),
                str(store.path),
                str(tmp_path),
                name,
            ]
        )
        for name in ("one", "two")
    ]
    try:
        deadline = time.monotonic() + 15
        while not list(tmp_path.glob("*.granted")) and time.monotonic() < deadline:
            time.sleep(0.02)
        granted = list(tmp_path.glob("*.granted"))
        assert len(granted) == 1
        with store.database() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM provider_requests WHERE state='granted'"
                ).fetchone()[0]
                == 1
            )
        (tmp_path / (granted[0].stem + ".release")).touch()
        while len(list(tmp_path.glob("*.granted"))) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(list(tmp_path.glob("*.granted"))) == 2
        for name in ("one", "two"):
            (tmp_path / (name + ".release")).touch()
        assert [child.wait(timeout=5) for child in children] == [0, 0]
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_endpoint_identity_normalization():
    assert (
        endpoint("https://MODEL.invalid:443/v1/chat/completions")
        == "https://model.invalid/v1"
    )
    assert (
        endpoint("https://model.invalid/v1/embeddings?version=1")
        == "https://model.invalid/v1"
    )
