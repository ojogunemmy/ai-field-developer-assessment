"""Mock downstream MCP server, built with the official MCP SDK.

It exposes one ordinary tool and two ``admin_`` tools over Streamable HTTP.
``CALLS`` records every tool that actually ran, so tests can prove that a
blocked call never reached this server.

Run it with ``mcp-gateway-mock-server`` (listens on 127.0.0.1:9001/mcp).
"""

import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

CALLS: list[str] = []


def create_mock_server() -> MCPServer:
    server = MCPServer("mock-downstream")

    @server.tool()
    def get_status() -> str:
        """Report service health."""
        CALLS.append("get_status")
        return "all systems operational"

    @server.tool()
    def admin_reset_key(key_id: str) -> str:
        """Rotate an API key (admin only)."""
        CALLS.append("admin_reset_key")
        return f"key {key_id} rotated"

    @server.tool()
    def admin_list_users() -> list[str]:
        """List user accounts (admin only)."""
        CALLS.append("admin_list_users")
        return ["alice", "bob"]

    return server


def create_mock_app(server: MCPServer | None = None) -> Starlette:
    server = server or create_mock_server()
    # Stateless JSON responses keep the mock simple; the gateway also streams SSE.
    return server.streamable_http_app(stateless_http=True, json_response=True)


def main() -> None:
    port = int(os.environ.get("MOCK_PORT", "9001"))
    uvicorn.run(create_mock_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
