"""Method-level authorization for MCP messages.

The policy is a pure function of the caller and the parsed message, so it can
be tested without HTTP.

- ``tools/list``: always forwarded, unchanged.
- ``tools/call``: ``params.name`` must be a string. Names starting with
  ``admin_`` (compared case-insensitively, so ``ADMIN_reset_key`` cannot slip
  through) require the ``admin`` role; otherwise the call is refused with
  ``-32001``.
- Everything else (``initialize``, ``ping``, notifications, responses to
  server requests): forwarded, so the gateway never breaks the MCP handshake.
"""

from mcp_gateway.jsonrpc import INVALID_PARAMS, UNAUTHORIZED, JsonRpcError, Message
from mcp_gateway.tokens import Principal

ADMIN_TOOL_PREFIX = "admin_"


def is_admin_tool(name: str) -> bool:
    return name.casefold().startswith(ADMIN_TOOL_PREFIX)


def authorize(principal: Principal, message: Message) -> None:
    """Return if the message may be forwarded; raise ``JsonRpcError`` if not."""
    if message.method != "tools/call":
        return

    params = message.params
    name = params.get("name") if isinstance(params, dict) else None
    if not isinstance(name, str):
        raise JsonRpcError(
            INVALID_PARAMS, "Invalid params: params.name must be a string", message.request_id
        )
    if is_admin_tool(name) and not principal.is_admin:
        raise JsonRpcError(UNAUTHORIZED, "Unauthorized Tool Call", message.request_id)
