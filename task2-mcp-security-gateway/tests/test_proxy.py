"""The gateway over HTTP, against a recording fake downstream server."""

import json

import httpx
import pytest
from conftest import auth, call_tool, rpc, token

from mcp_gateway.config import ConfigError, Settings

# --- authentication -------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {token('admin', secret=b'x' * 40)}"},
        {"Authorization": f"Bearer {token('admin', ttl=-5)}"},
    ],
)
async def test_unauthenticated_requests_get_401(gateway, downstream, headers):
    response = await gateway.post("/mcp", json=rpc("tools/list"), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == -32001
    assert downstream.requests == []


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_get_and_delete_also_require_auth(gateway, downstream, method):
    response = await gateway.request(method, "/mcp")
    assert response.status_code == 401
    assert downstream.requests == []


# --- tools/list -----------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "viewer"])
async def test_tools_list_is_forwarded_transparently(gateway, downstream, role):
    request = rpc("tools/list", request_id="list-1")
    response = await gateway.post("/mcp", json=request, headers=auth(role))
    assert response.status_code == 200
    assert downstream.bodies == [request]
    assert response.json()["result"]["echo"] == request


# --- tools/call -----------------------------------------------------------


async def test_viewer_admin_call_is_blocked_without_reaching_downstream(gateway, downstream):
    response = await gateway.post(
        "/mcp", json=call_tool("admin_reset_key", {"key_id": "k1"}, 42), headers=auth("viewer")
    )
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 42,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert downstream.requests == []


async def test_admin_can_call_admin_tool(gateway, downstream):
    request = call_tool("admin_reset_key", {"key_id": "k1"})
    response = await gateway.post("/mcp", json=request, headers=auth("admin"))
    assert response.status_code == 200
    assert downstream.bodies == [request]


@pytest.mark.parametrize("role", ["admin", "viewer"])
async def test_regular_tool_is_forwarded_for_any_role(gateway, downstream, role):
    response = await gateway.post("/mcp", json=call_tool("get_status"), headers=auth(role))
    assert response.status_code == 200
    assert len(downstream.requests) == 1


async def test_case_variant_admin_tool_is_blocked(gateway, downstream):
    response = await gateway.post("/mcp", json=call_tool("ADMIN_reset_key"), headers=auth("viewer"))
    assert response.json()["error"]["code"] == -32001
    assert downstream.requests == []


async def test_duplicate_name_smuggling_is_blocked(gateway, downstream):
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"get_status","name":"admin_reset_key"}}'
    )
    response = await gateway.post(
        "/mcp", content=body, headers={**auth("viewer"), "content-type": "application/json"}
    )
    assert response.json()["error"]["code"] == -32600
    assert downstream.requests == []


# --- malformed input ------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not json", -32700),
        (b"[]", -32600),
        (json.dumps([call_tool("get_status")]).encode(), -32600),
        (b'{"jsonrpc":"2.0","id":1}', -32600),
        (json.dumps(rpc("tools/call", {"arguments": {}})).encode(), -32602),
    ],
)
async def test_malformed_messages_are_answered_locally(gateway, downstream, body, code):
    response = await gateway.post("/mcp", content=body, headers=auth("admin"))
    assert response.status_code == 200
    assert response.json()["error"]["code"] == code
    assert downstream.requests == []


async def test_oversized_body_is_rejected(gateway, downstream):
    big = call_tool("get_status", {"blob": "x" * 20_000})
    response = await gateway.post("/mcp", json=big, headers=auth("admin"))
    assert response.status_code == 413
    assert downstream.requests == []


# --- forwarding details ---------------------------------------------------


async def test_authorization_header_is_not_forwarded(gateway, downstream):
    await gateway.post(
        "/mcp",
        json=rpc("tools/list"),
        headers={**auth("admin"), "Cookie": "session=secret", "X-Custom": "1"},
    )
    forwarded = downstream.requests[0].headers
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded
    assert "x-custom" not in forwarded


async def test_mcp_headers_are_forwarded_both_ways(gateway, downstream):
    response = await gateway.post(
        "/mcp",
        json=rpc("tools/list"),
        headers={
            **auth("admin"),
            "Mcp-Session-Id": "abc",
            "MCP-Protocol-Version": "2025-11-25",
            "Accept": "application/json, text/event-stream",
        },
    )
    forwarded = downstream.requests[0].headers
    assert forwarded["mcp-session-id"] == "abc"
    assert forwarded["mcp-protocol-version"] == "2025-11-25"
    assert forwarded["accept"] == "application/json, text/event-stream"
    assert response.headers["mcp-session-id"] == "session-123"


async def test_forwarded_body_is_the_checked_message(gateway, downstream):
    request = call_tool("get_status", {"verbose": True}, "abc")
    await gateway.post("/mcp", json=request, headers=auth("viewer"))
    assert downstream.bodies == [request]


async def test_notifications_are_forwarded(gateway, downstream):
    downstream.status_code = 202
    downstream.body = b""
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = await gateway.post("/mcp", json=note, headers=auth("viewer"))
    assert response.status_code == 202
    assert downstream.bodies == [note]


async def test_sse_responses_are_streamed_back(gateway, downstream):
    downstream.content_type = "text/event-stream"
    downstream.body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    response = await gateway.post("/mcp", json=rpc("tools/list"), headers=auth("admin"))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == downstream.body


async def test_downstream_status_is_preserved(gateway, downstream):
    downstream.status_code = 404
    downstream.body = (
        b'{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Session not found"}}'
    )
    response = await gateway.post("/mcp", json=rpc("tools/list"), headers=auth("admin"))
    assert response.status_code == 404


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_get_and_delete_are_forwarded(gateway, downstream, method):
    response = await gateway.request(method, "/mcp", headers=auth("viewer"))
    assert response.status_code == 200
    assert downstream.requests[0].method == method


@pytest.mark.parametrize(
    ("error", "status"),
    [(httpx.ConnectError("refused"), 502), (httpx.ReadTimeout("slow"), 504)],
)
async def test_downstream_failures_are_sanitized(gateway, downstream, error, status):
    downstream.error = error
    response = await gateway.post(
        "/mcp", json=rpc("tools/list", request_id=5), headers=auth("admin")
    )
    assert response.status_code == status
    body = response.json()
    assert body["id"] == 5
    assert body["error"]["code"] == -32603
    assert "refused" not in body["error"]["message"]


async def test_health_endpoint_needs_no_auth(gateway):
    assert (await gateway.get("/healthz")).json() == {"status": "ok"}


# --- configuration --------------------------------------------------------


def test_short_secret_is_refused():
    with pytest.raises(ConfigError):
        Settings(jwt_secret=b"short")


def test_missing_secret_is_refused(monkeypatch):
    monkeypatch.delenv("GATEWAY_JWT_SECRET", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_JWT_SECRET", "s" * 40)
    monkeypatch.setenv("GATEWAY_DOWNSTREAM_URL", "http://x/mcp")
    monkeypatch.setenv("GATEWAY_MAX_BODY_BYTES", "123")
    settings = Settings.from_env()
    assert settings.downstream_url == "http://x/mcp"
    assert settings.max_body_bytes == 123


def test_gateway_refuses_to_start_without_secret():
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "GATEWAY_JWT_SECRET"}
    result = subprocess.run(
        [sys.executable, "-m", "mcp_gateway"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0
    assert "GATEWAY_JWT_SECRET is not set" in result.stderr
