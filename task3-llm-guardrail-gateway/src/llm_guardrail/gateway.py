"""OpenAI-compatible ``/v1/chat/completions`` proxy with streaming redaction.

Streaming requests (``"stream": true``) are relayed event by event:

    upstream bytes ─▶ SSEParser ─▶ JSON chunk ─▶ StreamRedactor (per choice)
                                                     │ safe text only
    client ◀─ SSE event with redacted delta ◀────────┘

Each upstream chunk is forwarded as soon as it is parsed, with its
``delta.content`` replaced by whatever the redactor has released. Nothing is
accumulated beyond the redactor's small hold-back buffer and the SSE parser's
partial line, so time to first token and memory stay independent of the
response length.

When a choice finishes (``finish_reason`` set) or the stream ends, the
remaining held text is flushed, redacted, into the outgoing event.

Non-streaming requests are redacted as a whole. Upstream failures are replaced
by a sanitized gateway error that never includes upstream bodies or internals.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from llm_guardrail.config import Settings
from llm_guardrail.redactor import StreamRedactor, redact
from llm_guardrail.sse import SSEParser, format_event

logger = logging.getLogger(__name__)

DONE = "[DONE]"


def gateway_error(status: int, error_type: str, message: str) -> JSONResponse:
    request_id = uuid.uuid4().hex[:12]
    return JSONResponse(
        {"error": {"type": error_type, "message": message, "request_id": request_id}},
        status_code=status,
    )


def _upstream_failure(status: int) -> JSONResponse:
    if status == 429:
        return gateway_error(429, "rate_limited", "The model provider is rate limiting requests.")
    if status in (400, 404, 422):
        return gateway_error(400, "invalid_request", "The model provider rejected the request.")
    return gateway_error(502, "upstream_error", "The model provider returned an error.")


class StreamRelay:
    """Turns upstream SSE bytes into redacted SSE bytes, one event at a time."""

    def __init__(self) -> None:
        self.parser = SSEParser()
        self.redactors: dict[int, StreamRedactor] = {}
        self.template: dict[str, Any] | None = None  # last chunk, for synthetic flushes

    def _redactor(self, index: int) -> StreamRedactor:
        return self.redactors.setdefault(index, StreamRedactor())

    def process_event(self, data: str) -> list[bytes]:
        if data.strip() == DONE:
            return [*self.flush_all(), format_event(DONE)]
        try:
            chunk = json.loads(data)
        except ValueError:
            logger.warning("Dropped unparseable upstream event")
            return []
        if not isinstance(chunk, dict):
            return []
        self.template = chunk

        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            redactor = self._redactor(int(choice.get("index", 0)))
            delta = choice.get("delta")
            text = ""
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text = redactor.feed(delta["content"])
            if choice.get("finish_reason") is not None:
                text += redactor.flush()
            if isinstance(delta, dict) and ("content" in delta or text):
                delta["content"] = text
        return [format_event(json.dumps(chunk, separators=(",", ":")))]

    def flush_all(self) -> list[bytes]:
        """Release text still held for choices that never sent a finish_reason."""
        events = []
        for index, redactor in self.redactors.items():
            text = redactor.flush()
            if not text:
                continue
            chunk = {
                key: value
                for key, value in (self.template or {}).items()
                if key in ("id", "object", "created", "model")
            }
            chunk["choices"] = [{"index": index, "delta": {"content": text}, "finish_reason": None}]
            events.append(format_event(json.dumps(chunk, separators=(",", ":"))))
        return events

    def feed(self, raw: bytes) -> list[bytes]:
        return [out for data in self.parser.feed(raw) for out in self.process_event(data)]

    def close(self) -> list[bytes]:
        events = [out for data in self.parser.close() for out in self.process_event(data)]
        return events + self.flush_all()


class GuardrailGateway:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        headers = {"accept-encoding": "identity"}
        if settings.upstream_api_key:
            headers["authorization"] = f"Bearer {settings.upstream_api_key}"
        self.client = client or httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            headers=headers,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=settings.read_timeout,
                write=settings.connect_timeout,
                pool=settings.connect_timeout,
            ),
        )

    async def chat_completions(self, request: Request) -> Response:
        body = await request.body()
        if len(body) > self.settings.max_body_bytes:
            return gateway_error(413, "invalid_request", "Request body is too large.")
        try:
            payload = json.loads(body)
        except ValueError:
            return gateway_error(400, "invalid_request", "Request body must be JSON.")
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return gateway_error(400, "invalid_request", "Request must include a messages list.")

        if payload.get("stream") is True:
            return await self._stream(payload)
        return await self._complete(payload)

    async def _complete(self, payload: dict[str, Any]) -> Response:
        try:
            upstream = await self.client.post("/chat/completions", json=payload)
        except httpx.TimeoutException:
            return gateway_error(504, "upstream_timeout", "The model provider timed out.")
        except httpx.HTTPError as exc:
            logger.warning("Upstream unavailable: %s", type(exc).__name__)
            return gateway_error(502, "upstream_unavailable", "The model provider is unavailable.")
        if upstream.status_code != 200:
            logger.warning("Upstream returned HTTP %s", upstream.status_code)
            return _upstream_failure(upstream.status_code)
        try:
            completion = upstream.json()
        except ValueError:
            return gateway_error(502, "upstream_error", "The model provider returned an error.")
        for choice in completion.get("choices") or []:
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                message["content"] = redact(message["content"])
        return JSONResponse(completion)

    async def _stream(self, payload: dict[str, Any]) -> Response:
        upstream_request = self.client.build_request("POST", "/chat/completions", json=payload)
        try:
            upstream = await self.client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return gateway_error(504, "upstream_timeout", "The model provider timed out.")
        except httpx.HTTPError as exc:
            logger.warning("Upstream unavailable: %s", type(exc).__name__)
            return gateway_error(502, "upstream_unavailable", "The model provider is unavailable.")
        if upstream.status_code != 200:
            await upstream.aclose()
            logger.warning("Upstream returned HTTP %s", upstream.status_code)
            return _upstream_failure(upstream.status_code)

        return StreamingResponse(
            self._relay(upstream),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    async def _relay(self, upstream: httpx.Response) -> AsyncIterator[bytes]:
        relay = StreamRelay()
        try:
            async for raw in upstream.aiter_raw():
                for event in relay.feed(raw):
                    yield event
            for event in relay.close():
                yield event
        except httpx.HTTPError as exc:
            # Headers are already sent, so report the failure in-band. Held text is
            # discarded rather than flushed: the response is incomplete anyway.
            logger.warning("Upstream stream failed: %s", type(exc).__name__)
            error = {"error": {"type": "upstream_error", "message": "The stream was interrupted."}}
            yield format_event(json.dumps(error))
        finally:
            await upstream.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> Starlette:
    gateway = GuardrailGateway(settings, client)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await gateway.aclose()

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/v1/chat/completions", gateway.chat_completions, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
