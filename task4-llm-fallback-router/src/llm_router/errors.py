"""The standardized gateway error.

Every failure the router reports to a client has the same shape, and only ever
contains a fixed, safe message. Upstream status lines, response bodies,
exception text and stack traces are logged under the ``request_id`` and never
sent to the client.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class GatewayError(Exception):
    status: int
    type: str
    message: str
    retry_after: int | None = None
    request_id: str = field(default_factory=new_request_id)

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "type": self.type,
            "message": self.message,
            "request_id": self.request_id,
        }
        if self.retry_after is not None:
            error["retry_after_seconds"] = self.retry_after
        return {"error": error}

    def headers(self) -> dict[str, str]:
        headers = {"x-request-id": self.request_id}
        if self.retry_after is not None:
            headers["retry-after"] = str(self.retry_after)
        return headers


def unauthorized(request_id: str) -> GatewayError:
    return GatewayError(
        401, "authentication_error", "Missing or invalid API key.", request_id=request_id
    )


def invalid_request(message: str, request_id: str) -> GatewayError:
    return GatewayError(400, "invalid_request", message, request_id=request_id)


def rate_limited(retry_after: int, request_id: str) -> GatewayError:
    return GatewayError(
        429,
        "rate_limit_exceeded",
        "Token rate limit exceeded for this API key.",
        retry_after=retry_after,
        request_id=request_id,
    )


def request_too_large(request_id: str) -> GatewayError:
    return GatewayError(
        413,
        "request_too_large",
        "This request needs more tokens than the per-minute limit allows.",
        request_id=request_id,
    )


def all_providers_failed(request_id: str) -> GatewayError:
    return GatewayError(
        503,
        "upstream_unavailable",
        "All model providers are currently unavailable. Please retry.",
        request_id=request_id,
    )


def upstream_rejected(request_id: str) -> GatewayError:
    return GatewayError(
        400,
        "invalid_request",
        "The model provider rejected the request.",
        request_id=request_id,
    )
