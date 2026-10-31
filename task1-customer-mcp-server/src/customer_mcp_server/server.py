"""MCP server wiring: tool registry, request handlers and STDIO transport.

Built on the SDK's low-level ``Server`` rather than ``MCPServer``. MCPServer
derives a lenient argument model from each function signature, which ignores
unknown fields and coerces JSON strings such as "25.50" into numbers. Using the
low-level server hands the raw arguments to our strict Pydantic models, so
invalid input can never reach a business operation.

Error mapping:
- Unknown tool name: JSON-RPC error ``-32602`` (Invalid params).
- Arguments that fail schema validation: JSON-RPC error ``-32602`` (Invalid
  params), with one ``{"field", "message"}`` entry per problem in ``data``.
- Valid arguments for a customer that does not exist: a tool result with
  ``isError: true``, so the calling model can read the message.
- Unexpected exceptions: logged with a traceback, then reported as an
  ``isError`` result with a generic message that leaks no internals.
- Unparseable input lines: JSON-RPC error ``-32700`` (Parse error), or
  ``-32600`` (Invalid Request) for valid JSON that is not a JSON-RPC message.
  The SDK drops these silently, so ``run_stdio`` answers them itself.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.shared._stream_protocols import ReadStream, WriteStream
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from customer_mcp_server import __version__, tools
from customer_mcp_server.models import (
    CustomerLookupInput,
    CustomerRecord,
    RefundInput,
    RefundResult,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "customer-mcp-server"


@dataclass(frozen=True)
class RegisteredTool:
    definition: types.Tool
    handler: Callable[[dict[str, Any]], dict[str, Any]]


TOOLS: dict[str, RegisteredTool] = {
    "get_customer_record": RegisteredTool(
        definition=types.Tool(
            name="get_customer_record",
            title="Get customer record",
            description="Retrieve a customer's record by customer ID (format CUST-XXXXX).",
            input_schema=CustomerLookupInput.model_json_schema(),
            output_schema=CustomerRecord.model_json_schema(),
            annotations=types.ToolAnnotations(
                read_only_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        ),
        handler=tools.get_customer_record,
    ),
    "trigger_refund": RegisteredTool(
        definition=types.Tool(
            name="trigger_refund",
            title="Trigger refund",
            description=(
                "Issue a mock refund to an existing customer. Requires a customer ID "
                "(CUST-XXXXX), a finite positive amount, and a reason of at least 10 "
                "non-whitespace characters. Returns the refund reference and status."
            ),
            input_schema=RefundInput.model_json_schema(),
            output_schema=RefundResult.model_json_schema(),
            annotations=types.ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
        handler=tools.trigger_refund,
    ),
}


def _text_result(payload: dict[str, Any] | str, is_error: bool = False) -> types.CallToolResult:
    if isinstance(payload, str):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=payload)],
            is_error=is_error,
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=is_error,
    )


async def handle_list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[tool.definition for tool in TOOLS.values()])


async def handle_call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    tool = TOOLS.get(params.name)
    if tool is None:
        logger.warning("Unknown tool requested: %r", params.name)
        raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name}")

    # Argument values are deliberately not logged: they may contain customer data.
    logger.info("Calling tool %s", params.name)
    try:
        result = tool.handler(params.arguments or {})
    except tools.InvalidArguments as exc:
        logger.info("Tool %s rejected invalid params: %s", params.name, exc)
        raise MCPError(types.INVALID_PARAMS, str(exc), data=exc.problems) from exc
    except tools.ToolError as exc:
        logger.info("Tool %s rejected: %s", params.name, exc)
        return _text_result(str(exc), is_error=True)
    except Exception:
        logger.exception("Tool %s failed unexpectedly", params.name)
        return _text_result("Internal error while running the tool.", is_error=True)
    return _text_result(result)


def _tool_input_schema(name: str) -> dict[str, Any] | None:
    tool = TOOLS.get(name)
    return None if tool is None else tool.definition.input_schema


def create_server() -> Server:
    return Server(
        SERVER_NAME,
        version=__version__,
        instructions="Look up mock customer records and issue mock refunds.",
        get_tool_input_schema=_tool_input_schema,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


def _parse_error_response(exc: Exception) -> types.JSONRPCError:
    if isinstance(exc, ValidationError) and any(e["type"] == "json_invalid" for e in exc.errors()):
        error = types.ErrorData(code=types.PARSE_ERROR, message="Parse error")
    else:
        error = types.ErrorData(code=types.INVALID_REQUEST, message="Invalid Request")
    # The request id is unknown when the message could not be parsed.
    return types.JSONRPCError(jsonrpc="2.0", id=None, error=error)


async def _answer_unparseable(
    source: ReadStream[SessionMessage | Exception],
    sink: WriteStream[SessionMessage | Exception],
    write_stream: WriteStream[SessionMessage],
) -> None:
    """Forward valid messages to the server and answer the rest directly.

    The SDK's STDIO reader turns a line it cannot parse into an exception on
    the read stream, which the server only logs. JSON-RPC 2.0 requires an
    error response, so this filter sends one.
    """
    async with sink:
        async for item in source:
            if isinstance(item, Exception):
                response = _parse_error_response(item)
                logger.warning("Rejected unparseable message: %s", response.error.message)
                await write_stream.send(SessionMessage(response))
                continue
            await sink.send(item)


async def run_stdio() -> None:
    server = create_server()
    logger.info("Starting %s %s on STDIO", SERVER_NAME, __version__)
    async with stdio_server() as (read_stream, write_stream):
        sink, filtered = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_answer_unparseable, read_stream, sink, write_stream)
            await server.run(filtered, write_stream, server.create_initialization_options())
            tg.cancel_scope.cancel()
    logger.info("STDIO session closed")
