"""Real servers on local ports, driven by the official MCP client.

Mock downstream MCP server (SDK, Streamable HTTP) <- gateway <- MCP SDK client
"""

import socket
import threading
import time

import httpx
import httpx2
import pytest
import uvicorn
from conftest import SECRET, token
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from mcp_gateway import mock_server
from mcp_gateway.config import Settings
from mcp_gateway.proxy import create_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerThread:
    def __init__(self, app: object, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "ServerThread":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture(scope="module")
def gateway_url():
    mock_port, gateway_port = free_port(), free_port()
    settings = Settings(jwt_secret=SECRET, downstream_url=f"http://127.0.0.1:{mock_port}/mcp")
    with (
        ServerThread(mock_server.create_mock_app(), mock_port),
        ServerThread(create_app(settings), gateway_port),
    ):
        yield f"http://127.0.0.1:{gateway_port}/mcp"


@pytest.fixture(autouse=True)
def reset_calls():
    mock_server.CALLS.clear()


def connect(url: str, role: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token(role)}"})
    return Client(streamable_http_client(url, http_client=http))


async def test_viewer_lists_all_tools(gateway_url):
    async with connect(gateway_url, "viewer") as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {"get_status", "admin_reset_key", "admin_list_users"}


async def test_viewer_can_call_regular_tool(gateway_url):
    async with connect(gateway_url, "viewer") as client:
        result = await client.call_tool("get_status", {})
    assert result.is_error is False
    assert mock_server.CALLS == ["get_status"]


async def test_viewer_admin_call_is_refused_and_never_runs(gateway_url):
    async with connect(gateway_url, "viewer") as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("admin_reset_key", {"key_id": "k1"})
    assert exc_info.value.error.code == -32001
    assert exc_info.value.error.message == "Unauthorized Tool Call"
    assert mock_server.CALLS == []


async def test_admin_call_runs_for_admin(gateway_url):
    async with connect(gateway_url, "admin") as client:
        result = await client.call_tool("admin_reset_key", {"key_id": "k1"})
    assert result.is_error is False
    assert mock_server.CALLS == ["admin_reset_key"]


def test_raw_http_without_token_is_401(gateway_url):
    response = httpx.post(gateway_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
