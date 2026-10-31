"""JSON-RPC 2.0 wire-format parsing and error construction."""

import json
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001

RequestId = str | int | None


class JsonRpcError(Exception):
    """A failure that the gateway answers itself with a JSON-RPC error."""

    def __init__(self, code: int, message: str, request_id: RequestId = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id

    def to_response(self) -> dict[str, Any]:
        return error_response(self.request_id, self.code, self.message)


@dataclass(frozen=True)
class Message:
    """A parsed JSON-RPC message. ``raw`` is the exact object to forward."""

    raw: dict[str, Any]
    method: str | None
    request_id: RequestId
    has_id: bool

    @property
    def params(self) -> Any:
        return self.raw.get("params")


def error_response(request_id: RequestId, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # A body like {"name": "get_status", "name": "admin_reset_key"} could be read
    # differently by the gateway and the downstream parser. Refuse it outright.
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonRpcError(INVALID_REQUEST, f"Invalid Request: duplicate key {key!r}")
        result[key] = value
    return result


def parse_message(body: bytes) -> Message:
    """Parse one JSON-RPC message, raising ``JsonRpcError`` for anything invalid."""
    try:
        data = json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except JsonRpcError:
        raise
    except (ValueError, RecursionError) as exc:
        raise JsonRpcError(PARSE_ERROR, "Parse error") from exc

    if isinstance(data, list):
        # MCP (2025-06-18 onwards) removed JSON-RPC batching; refusing batches also
        # means one message can never hide an admin call among allowed ones.
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: batch requests are not supported")
    if not isinstance(data, dict):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request")

    has_id = "id" in data
    request_id = data.get("id")
    if not isinstance(request_id, str | int | None) or isinstance(request_id, bool):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: id must be a string or number")
    if data.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'", request_id)

    method = data.get("method")
    if method is not None and not isinstance(method, str):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: method must be a string", request_id)
    if method is None and not ("result" in data or "error" in data):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: missing method", request_id)

    return Message(raw=data, method=method, request_id=request_id, has_id=has_id)
