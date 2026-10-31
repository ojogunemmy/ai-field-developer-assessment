"""The HTTP reverse proxy.

Every request goes through the same pipeline:

1. **Authenticate.** Read ``Authorization: Bearer <token>`` and verify it.
   Failure: HTTP 401 with a JSON-RPC ``-32001`` body. Nothing is forwarded.
2. **Parse** (POST only). Read the body with a size cap and parse it as a single
   JSON-RPC message. Failure: a JSON-RPC ``-32700`` or ``-32600`` error.
3. **Authorize.** Apply ``policy.authorize``. Failure: a JSON-RPC error, such as
   ``-32001 Unauthorized Tool Call``, returned with HTTP 200 so MCP clients
   surface it as a normal tool-call error. Nothing is forwarded.
4. **Forward.** Send the *re-serialized* message (exactly what was checked) to
   the downstream server and stream its response back unchanged. The caller's
   ``Authorization`` header is never forwarded.

GET (server-to-client SSE stream) and DELETE (session end) carry no JSON-RPC
body; they are authenticated and then forwarded.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mcp_gateway import policy
from mcp_gateway.config import Settings
from mcp_gateway.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    UNAUTHORIZED,
    JsonRpcError,
    error_response,
    parse_message,
)
from mcp_gateway.tokens import InvalidToken, Principal, parse_bearer, verify_token

logger = logging.getLogger(__name__)

# Request headers passed to the downstream server. Everything else, including
# Authorization and Cookie, is dropped.
FORWARDED_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
)
# Response headers passed back to the client.
FORWARDED_RESPONSE_HEADERS = (
    "cache-control",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
)


class BodyTooLarge(Exception):
    pass


def _jsonrpc_reply(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


def _unauthenticated(reason: str) -> JSONResponse:
    logger.info("Rejected unauthenticated request: %s", reason)
    response = _jsonrpc_reply(
        error_response(None, UNAUTHORIZED, "Unauthorized: missing or invalid bearer token"),
        status_code=401,
    )
    response.headers["WWW-Authenticate"] = 'Bearer realm="mcp-gateway"'
    return response


async def _read_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise BodyTooLarge
    return bytes(body)


class Gateway:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.downstream_timeout),
            # Ask for unencoded bodies so raw bytes can be streamed straight through.
            headers={"accept-encoding": "identity"},
        )

    def _authenticate(self, request: Request) -> Principal:
        token = parse_bearer(request.headers.get("authorization"))
        return verify_token(token, self.settings.jwt_secret)

    async def handle(self, request: Request) -> Response:
        try:
            principal = self._authenticate(request)
        except InvalidToken as exc:
            return _unauthenticated(str(exc))

        if request.method != "POST":
            return await self._forward(request, body=None)

        try:
            body = await _read_body(request, self.settings.max_body_bytes)
        except BodyTooLarge:
            return _jsonrpc_reply(
                error_response(None, INVALID_REQUEST, "Invalid Request: body too large"), 413
            )

        try:
            message = parse_message(body)
            policy.authorize(principal, message)
        except JsonRpcError as exc:
            logger.info(
                "Blocked %s from %s (%s): %s",
                _describe(body),
                principal.subject,
                principal.role,
                exc.message,
            )
            return _jsonrpc_reply(exc.to_response())

        logger.info(
            "Forwarding %s from %s (%s)",
            _describe_message(message.raw),
            principal.subject,
            principal.role,
        )
        canonical = json.dumps(message.raw, separators=(",", ":")).encode()
        return await self._forward(request, body=canonical, request_id=message.request_id)

    async def _forward(
        self, request: Request, body: bytes | None, request_id: str | int | None = None
    ) -> Response:
        headers = {
            name: value
            for name in FORWARDED_REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        downstream_request = self.client.build_request(
            request.method, self.settings.downstream_url, headers=headers, content=body
        )
        try:
            downstream = await self.client.send(downstream_request, stream=True)
        except httpx.TimeoutException:
            logger.warning("Downstream MCP server timed out")
            return _jsonrpc_reply(
                error_response(request_id, INTERNAL_ERROR, "Downstream MCP server timed out"), 504
            )
        except httpx.HTTPError as exc:
            logger.warning("Downstream MCP server unavailable: %s", type(exc).__name__)
            return _jsonrpc_reply(
                error_response(request_id, INTERNAL_ERROR, "Downstream MCP server unavailable"),
                502,
            )

        response_headers = {
            name: value
            for name in FORWARDED_RESPONSE_HEADERS
            if (value := downstream.headers.get(name)) is not None
        }
        return StreamingResponse(
            downstream.aiter_raw(),
            status_code=downstream.status_code,
            headers=response_headers,
            background=BackgroundTask(downstream.aclose),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def _describe_message(raw: dict[str, Any]) -> str:
    method = raw.get("method", "response")
    params = raw.get("params")
    if method == "tools/call" and isinstance(params, dict):
        return f"tools/call {params.get('name')!r}"
    return str(method)


def _describe(body: bytes) -> str:
    try:
        raw = json.loads(body)
    except ValueError:
        return "unparseable body"
    return _describe_message(raw) if isinstance(raw, dict) else "non-object body"


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> Starlette:
    gateway = Gateway(settings, client)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await gateway.aclose()

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/mcp", gateway.handle, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    return app
