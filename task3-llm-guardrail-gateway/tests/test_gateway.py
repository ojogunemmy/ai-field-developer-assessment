"""The gateway over HTTP, against the mock provider and scripted upstreams."""

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from llm_guardrail.config import Settings
from llm_guardrail.gateway import create_app
from llm_guardrail.mock_provider import create_mock_provider
from llm_guardrail.redactor import REDACTED

PII_TEXT = (
    "Sure! Email jane.doe@example.com, SSN 123-45-6789, card 4111 1111 1111 1111. "
    "Order 1234567812345678 is fine."
)
SECRETS = ["jane.doe@example.com", "123-45-6789", "4111 1111 1111 1111"]


def chat(content: str, stream: bool) -> dict[str, object]:
    return {"model": "mock", "stream": stream, "messages": [{"role": "user", "content": content}]}


def content_of(sse_body: str) -> str:
    text = []
    for line in sse_body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            event = json.loads(line[6:])
            for choice in event.get("choices", []):
                text.append(choice.get("delta", {}).get("content") or "")
    return "".join(text)


def gateway_client(upstream_app=None, handler=None) -> httpx.AsyncClient:
    if handler is not None:
        transport: httpx.AsyncBaseTransport = httpx.MockTransport(handler)
    else:
        transport = httpx.ASGITransport(app=upstream_app or create_mock_provider(delay=0, seed=1))
    upstream = httpx.AsyncClient(transport=transport, base_url="http://upstream.test/v1")
    app = create_app(Settings(), client=upstream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw.test")


# --- streaming ------------------------------------------------------------


@pytest.mark.parametrize("seed", range(10))
async def test_stream_is_redacted(seed):
    provider = create_mock_provider(delay=0, max_chunk=4, seed=seed)
    async with gateway_client(provider) as client:
        response = await client.post("/v1/chat/completions", json=chat(PII_TEXT, True))
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    for secret in SECRETS:
        assert secret not in body
    assert content_of(body) == (
        f"Sure! Email {REDACTED}, SSN {REDACTED}, card {REDACTED}. Order 1234567812345678 is fine."
    )
    assert body.rstrip().endswith("data: [DONE]")


async def test_stream_keeps_chunk_metadata():
    async with gateway_client() as client:
        response = await client.post("/v1/chat/completions", json=chat("hi there", True))
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")
    ]
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert all(event["object"] == "chat.completion.chunk" for event in events)
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def scripted(events: list[str], status: int = 200):
    body = "".join(f"data: {event}\n\n" for event in events).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, stream=httpx.ByteStream(body), headers={"content-type": "text/event-stream"}
        )

    return handler


def chunk(content: str | None = None, finish: str | None = None, index: int = 0) -> str:
    delta = {} if content is None else {"content": content}
    return json.dumps(
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "m",
            "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
        }
    )


async def test_held_text_is_flushed_when_upstream_omits_finish_reason():
    handler = scripted([chunk("mail bob@exa"), chunk("mple.com"), "[DONE]"])
    async with gateway_client(handler=handler) as client:
        response = await client.post("/v1/chat/completions", json=chat("x", True))
    assert content_of(response.text) == f"mail {REDACTED}"
    assert "bob@" not in response.text


async def test_each_choice_has_its_own_redactor():
    handler = scripted(
        [
            chunk("a@b", index=0),
            chunk("x 12", index=1),
            chunk(".io", index=0),
            chunk("3-45-6789", index=1),
            chunk(None, "stop", 0),
            chunk(None, "stop", 1),
            "[DONE]",
        ]
    )
    async with gateway_client(handler=handler) as client:
        response = await client.post("/v1/chat/completions", json=chat("x", True))
    per_choice: dict[int, str] = {}
    for line in response.text.splitlines():
        if line.startswith("data: {"):
            for choice in json.loads(line[6:])["choices"]:
                per_choice[choice["index"]] = per_choice.get(choice["index"], "") + (
                    choice["delta"].get("content") or ""
                )
    assert per_choice == {0: REDACTED, 1: f"x {REDACTED}"}


async def test_unparseable_upstream_events_are_dropped():
    handler = scripted(["not json", chunk("hello "), chunk(None, "stop"), "[DONE]"])
    async with gateway_client(handler=handler) as client:
        response = await client.post("/v1/chat/completions", json=chat("x", True))
    assert content_of(response.text) == "hello "
    assert "not json" not in response.text


async def test_stream_interrupted_mid_way_reports_in_band():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {chunk('card 4111 1111')}\n\n".encode()
            raise httpx.ReadError("connection reset")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})

    async with gateway_client(handler=handler) as client:
        response = await client.post("/v1/chat/completions", json=chat("x", True))
    assert "4111 1111" not in response.text  # the held partial card is discarded
    assert "The stream was interrupted" in response.text
    assert "connection reset" not in response.text


# --- non-streaming --------------------------------------------------------


async def test_non_streaming_is_redacted():
    async with gateway_client() as client:
        response = await client.post("/v1/chat/completions", json=chat(PII_TEXT, False))
    content = response.json()["choices"][0]["message"]["content"]
    for secret in SECRETS:
        assert secret not in content
    assert content.count(REDACTED) == 3


# --- errors ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "status"),
    [(b"not json", 400), (b"[]", 400), (b'{"model":"m"}', 400)],
)
async def test_bad_requests(body, status):
    async with gateway_client() as client:
        response = await client.post("/v1/chat/completions", content=body)
    assert response.status_code == status
    assert response.json()["error"]["type"] == "invalid_request"


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize(
    ("upstream_status", "status", "error_type"),
    [(429, 429, "rate_limited"), (500, 502, "upstream_error"), (400, 400, "invalid_request")],
)
async def test_upstream_errors_are_sanitized(stream, upstream_status, status, error_type):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(upstream_status, json={"error": "secret internal stack trace"})

    async with gateway_client(handler=handler) as client:
        response = await client.post("/v1/chat/completions", json=chat("x", stream))
    assert response.status_code == status
    error = response.json()["error"]
    assert error["type"] == error_type
    assert "request_id" in error
    assert "stack trace" not in response.text


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize(
    ("exc", "status"), [(httpx.ConnectError("x"), 502), (httpx.ConnectTimeout("x"), 504)]
)
async def test_upstream_unreachable(stream, exc, status):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with gateway_client(handler=handler) as client:
        response = await client.post("/v1/chat/completions", json=chat("x", stream))
    assert response.status_code == status


async def test_health():
    async with gateway_client() as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}


# --- real sockets: time to first token ----------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerThread:
    def __init__(self, app: object, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "ServerThread":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def test_first_token_arrives_long_before_the_stream_ends():
    provider_port, gateway_port = free_port(), free_port()
    text = "This reply is long enough to take a while to stream. " * 6 + "Email a@b.io."
    settings = Settings(upstream_base_url=f"http://127.0.0.1:{provider_port}/v1")
    with (
        ServerThread(create_mock_provider(delay=0.02, max_chunk=4, seed=3), provider_port),
        ServerThread(create_app(settings), gateway_port),
    ):
        start = time.monotonic()
        first_content_at = None
        received = []
        with httpx.stream(
            "POST",
            f"http://127.0.0.1:{gateway_port}/v1/chat/completions",
            json=chat(text, True),
            timeout=30,
        ) as response:
            for line in response.iter_lines():
                received.append(line)
                if first_content_at is None and line.startswith("data: {"):
                    event = json.loads(line[6:])
                    if event["choices"][0]["delta"].get("content"):
                        first_content_at = time.monotonic() - start
        total = time.monotonic() - start

    assert first_content_at is not None
    assert total > 1.0  # the mock really did stream slowly
    assert first_content_at < total * 0.2, (first_content_at, total)
    assert content_of("\n".join(received)).endswith(f"Email {REDACTED}.")
