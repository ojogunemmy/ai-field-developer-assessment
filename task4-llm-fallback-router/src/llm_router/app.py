"""HTTP front end: ``POST /v1/chat/completions`` with a tenant API key.

Requests authenticate with ``Authorization: Bearer <tenant API key>``. The key
identifies the tenant whose token budget the request is charged to. Every
error response, from validation to upstream failure, uses the standardized
``GatewayError`` payload.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from llm_router import errors
from llm_router.config import Settings
from llm_router.ratelimit import SlidingWindowLimiter
from llm_router.router import ModelRouter

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000


def error_response(error: errors.GatewayError) -> JSONResponse:
    return JSONResponse(error.payload(), status_code=error.status, headers=error.headers())


def _validate(payload: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise errors.invalid_request("Request body must be a JSON object.", request_id)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise errors.invalid_request("Request must include a non-empty messages list.", request_id)
    if payload.get("stream") is True:
        raise errors.invalid_request("Streaming is not supported by this endpoint.", request_id)
    for key in ("max_tokens", "max_completion_tokens"):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise errors.invalid_request(f"{key} must be a positive integer.", request_id)
    return payload


def create_app(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    limiter: SlidingWindowLimiter | None = None,
) -> Starlette:
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    limiter = limiter or SlidingWindowLimiter(
        settings.db_path, settings.tokens_per_window, settings.window_seconds
    )
    router = ModelRouter(limiter, [settings.primary, settings.secondary], http)

    async def completions(request: Request) -> Response:
        request_id = errors.new_request_id()
        try:
            scheme, _, key = request.headers.get("authorization", "").partition(" ")
            tenant = settings.tenant_for(key.strip()) if scheme.lower() == "bearer" else None
            if tenant is None:
                raise errors.unauthorized(request_id)

            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                raise errors.invalid_request("Request body is too large.", request_id)
            try:
                payload = _validate(json.loads(body), request_id)
            except ValueError:
                raise errors.invalid_request("Request body must be JSON.", request_id) from None

            result = await router.complete(tenant, payload, request_id)
        except errors.GatewayError as exc:
            return error_response(exc)
        except Exception:
            # Last line of defence: log the details, return nothing internal.
            logger.exception("[%s] unexpected router error", request_id)
            return error_response(
                errors.GatewayError(
                    500, "internal_error", "Internal gateway error.", request_id=request_id
                )
            )

        remaining = max(0, limiter.limit - await limiter.usage(tenant))
        return JSONResponse(
            result.completion,
            headers={
                "x-request-id": request_id,
                "x-router-provider": result.provider,
                "x-ratelimit-limit-tokens": str(limiter.limit),
                "x-ratelimit-remaining-tokens": str(remaining),
            },
        )

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await http.aclose()

    app = Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/v1/chat/completions", completions, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.router = router
    app.state.limiter = limiter
    return app
