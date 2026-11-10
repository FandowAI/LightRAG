"""Admit each provider attempt and retain its slot until the response is closed."""

import asyncio
from contextvars import ContextVar
import json
import time

if __package__:
    from .core import QuotaError, QuotaStore, endpoint
else:
    from core import QuotaError, QuotaStore, endpoint

TASK = ContextVar("provider_admission_task", default=None)


def httpx_spec():
    import httpx

    return getattr(httpx.AsyncClient._send_single_request, "_provider_admission", None)


class Usage:
    def __init__(self, streaming=False):
        self.streaming, self.buffer, self.tokens, self.overflow = (
            streaming,
            b"",
            None,
            False,
        )

    def document(self, value):
        usage = value.get("usage") if isinstance(value, dict) else None
        if not isinstance(usage, dict):
            return
        total = usage.get("total_tokens")
        if total is None:
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
            if (
                type(prompt) is int
                and type(completion) is int
                and prompt >= 0
                and completion >= 0
            ):
                total = prompt + completion
        if type(total) is int and total >= 0:
            self.tokens = total

    def feed(self, chunk):
        if self.overflow and not self.streaming:
            return
        self.buffer += chunk
        if self.streaming:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:"):
                    try:
                        self.document(json.loads(line[5:].strip()))
                    except (ValueError, UnicodeError):
                        pass
        if len(self.buffer) > 1024 * 1024:
            self.buffer, self.overflow = b"", True

    def finish(self):
        if not self.streaming and not self.overflow:
            try:
                self.document(json.loads(self.buffer))
            except (ValueError, UnicodeError):
                pass
        return self.tokens


class Admission:
    def __init__(self, spec):
        self.spec = spec
        self.store = QuotaStore(spec["registry"])
        self.store.ready()
        self.endpoints = {endpoint(value) for value in spec["endpoints"]}

    def prepare(self, url, headers, content):
        known = endpoint(url) in self.endpoints
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeError, TypeError):
            if known:
                raise QuotaError(
                    "Shared provider admission requires a JSON model request"
                ) from None
            return None
        if not known:
            if (
                isinstance(payload, dict)
                and "model" in payload
                and any(
                    key in payload
                    for key in ("messages", "input", "prompt", "contents")
                )
            ):
                raise QuotaError(
                    "Model endpoint is outside the owned provider admission routes"
                )
            return None
        if not isinstance(payload, dict):
            raise QuotaError("Model request must be an object")
        policy = self.store.limits(url)
        embedding = str(url).split("?", 1)[0].rstrip("/").endswith("/embeddings")
        output = 0
        changed = False
        if not embedding:
            limits = [
                payload[key]
                for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
                if key in payload and payload[key] is not None
            ]
            if any(type(value) is not int or value < 1 for value in limits):
                raise QuotaError("Model output token limits must be positive integers")
            if limits:
                output = max(limits)
            else:
                output = policy.default_output_tokens
                if policy.tokens_per_minute is not None:
                    payload[
                        "max_output_tokens"
                        if str(url).rstrip("/").endswith("/responses")
                        else "max_tokens"
                    ] = output
                    changed = True
            candidates = payload.get("n", 1)
            if type(candidates) is not int or candidates < 1:
                raise QuotaError("Model completion count must be a positive integer")
            output *= candidates
        raw = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if changed
            else content
        )
        authentication = (
            headers.get("authorization")
            or headers.get("api-key")
            or headers.get("x-api-key")
            or ""
        )
        if authentication.lower().startswith("bearer "):
            authentication = authentication[7:].strip()
        identity = self.store.enqueue(
            url,
            authentication,
            self.spec["batch_id"],
            TASK.get() or self.spec.get("task_id"),
            len(raw) + 512 + output,
        )
        return identity, raw

    def decision(self, identity):
        row = self.store.poll(identity)
        if row["state"] not in {"queued", "granted"}:
            raise QuotaError(row["reason"] or "Provider admission was cancelled")
        return row["state"] == "granted"

    async def wait_async(self, identity):
        try:
            while not self.decision(identity):
                await asyncio.sleep(0.05)
        except BaseException:
            self.store.finish(identity, reason="Admission ended before sending")
            raise

    def wait(self, identity):
        try:
            while not self.decision(identity):
                time.sleep(0.05)
        except BaseException:
            self.store.finish(identity, reason="Admission ended before sending")
            raise


def rejection(httpx, request, error):
    # Both the SDK and LightRAG's outer retry owner must fail before a replay.
    return httpx.Response(
        429,
        request=request,
        headers={"x-should-retry": "false", "x-lightrag-admission": "rejected"},
        json={
            "error": {
                "code": "budget_exceeded",
                "type": "shared_provider_admission",
                "message": "Local shared provider admission: "
                + str(error)
                + ". No upstream request was sent.",
            }
        },
    )


def install_httpx(spec):
    import httpx

    previous = httpx.AsyncClient._send_single_request
    installed = getattr(previous, "_provider_admission", None)
    if installed is not None:
        if installed != spec:
            raise QuotaError("A process cannot change its provider admission owner")
        return spec
    admission = Admission(spec)

    class Stream(httpx.AsyncByteStream):
        def __init__(self, inner, identity, headers):
            self.inner, self.identity = inner, identity
            self.usage = Usage(
                headers.get("content-type", "").startswith("text/event-stream")
            )
            self.closed = False

        async def __aiter__(self):
            async for chunk in self.inner:
                self.usage.feed(chunk)
                yield chunk

        async def aclose(self):
            if self.closed:
                return
            pending = asyncio.create_task(self.inner.aclose())
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            pending.result()
            admission.store.finish(self.identity, tokens=self.usage.finish())
            self.closed = True
            if cancelled:
                raise asyncio.CancelledError

    async def send(client, request):
        if request.method != "POST":
            return await previous(client, request)
        known = endpoint(str(request.url)) in admission.endpoints
        if not known and "application/json" not in request.headers.get(
            "content-type", ""
        ):
            return await previous(client, request)
        identity = None
        try:
            prepared = admission.prepare(
                str(request.url), request.headers, await request.aread()
            )
            if prepared is None:
                return await previous(client, request)
            identity, raw = prepared
            await admission.wait_async(identity)
            if raw != request.content:
                request._content = raw
                request.stream = httpx.ByteStream(raw)
                request.headers["Content-Length"] = str(len(raw))
                request.headers.pop("Transfer-Encoding", None)
            response = await previous(client, request)
            if response.is_closed:
                usage = Usage(
                    response.headers.get("content-type", "").startswith(
                        "text/event-stream"
                    )
                )
                try:
                    usage.feed(response.content)
                except httpx.ResponseNotRead:
                    pass
                admission.store.finish(identity, tokens=usage.finish())
                return response
            response.stream = Stream(response.stream, identity, response.headers)
            return response
        except QuotaError as exc:
            if identity:
                admission.store.finish(
                    identity, reason="Local provider admission rejected"
                )
            return rejection(httpx, request, exc)
        except BaseException:
            if identity:
                admission.store.finish(
                    identity, reason="Transport outcome unknown; reservation retained"
                )
            raise

    send._provider_admission = spec
    httpx.AsyncClient._send_single_request = send
    return spec


def install_requests(spec):
    import requests

    previous = requests.Session.send
    installed = getattr(previous, "_provider_admission", None)
    if installed is not None:
        if installed != spec:
            raise QuotaError("A process cannot change its provider admission owner")
        return spec
    admission = Admission(spec)

    def send(session, request, **kwargs):
        if request.method != "POST":
            return previous(session, request, **kwargs)
        known = endpoint(request.url) in admission.endpoints
        if not known and "application/json" not in request.headers.get(
            "content-type", ""
        ):
            return previous(session, request, **kwargs)
        content = (
            request.body.encode() if isinstance(request.body, str) else request.body
        )
        prepared = admission.prepare(request.url, request.headers, content)
        if prepared is None:
            return previous(session, request, **kwargs)
        identity, raw = prepared
        try:
            if kwargs.get("stream"):
                raise QuotaError(
                    "The workflow adapter requires non-streaming responses"
                )
            admission.wait(identity)
            request.body = raw
            request.headers["Content-Length"] = str(len(raw))
            request.headers.pop("Transfer-Encoding", None)
            follow = kwargs.pop("allow_redirects", True)
            response = previous(session, request, allow_redirects=False, **kwargs)
            usage = Usage()
            usage.feed(response.content)
            admission.store.finish(identity, tokens=usage.finish())
            if follow:
                history = list(session.resolve_redirects(response, request, **kwargs))
                if history:
                    history.insert(0, response)
                    response = history.pop()
                    response.history = history
            return response
        except BaseException:
            admission.store.finish(
                identity, reason="Transport outcome unknown; reservation retained"
            )
            raise

    send._provider_admission = spec
    requests.Session.send = send
    return spec
