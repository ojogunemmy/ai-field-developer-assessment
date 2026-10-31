"""Protocol-level tests using the SDK client connected in-process."""

import json

import mcp_types as types
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from customer_mcp_server import server as server_module
from customer_mcp_server import tools
from customer_mcp_server.models import CustomerRecord, RefundResult
from customer_mcp_server.server import create_server

VALID_REFUND = {
    "customer_id": "CUST-00001",
    "amount": 25.5,
    "reason": "Item arrived damaged",
}


def connect():
    # Opened inside each test rather than in a fixture: the client's task group
    # must be entered and exited in the same task, which async fixtures break.
    return Client(create_server())


# --- tools/list ----------------------------------------------------------


async def test_both_tools_are_discoverable():
    async with connect() as client:
        result = await client.list_tools()
        assert {tool.name for tool in result.tools} == {"get_customer_record", "trigger_refund"}


async def test_lookup_schema():
    async with connect() as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "get_customer_record")
        schema = tool.input_schema
        assert schema["required"] == ["customer_id"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"
        assert tool.annotations.read_only_hint is True


async def test_refund_schema():
    async with connect() as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "trigger_refund")
        schema = tool.input_schema
        assert sorted(schema["required"]) == ["amount", "customer_id", "reason"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["amount"]["type"] == "number"
        assert schema["properties"]["amount"]["exclusiveMinimum"] == 0
        assert tool.annotations.read_only_hint is False


async def test_output_schemas_are_published():
    async with connect() as client:
        tools_by_name = {t.name: t for t in (await client.list_tools()).tools}
        lookup, refund = tools_by_name["get_customer_record"], tools_by_name["trigger_refund"]
        assert lookup.output_schema == CustomerRecord.model_json_schema()
        assert refund.output_schema == RefundResult.model_json_schema()


# --- tools/call: get_customer_record ------------------------------------


async def test_lookup_success():
    async with connect() as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
        assert result.is_error is False
        assert result.structured_content["name"] == "Ada Okafor"
        assert json.loads(result.content[0].text) == result.structured_content


async def test_lookup_not_found():
    async with connect() as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
        assert result.is_error is True
        assert result.content[0].text == "Customer not found: CUST-99999"


async def call_expecting_invalid_params(tool, arguments):
    """Call a tool that must fail validation and return the JSON-RPC error."""
    async with connect() as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool(tool, arguments)
    assert exc_info.value.error.code == types.INVALID_PARAMS
    return exc_info.value.error


async def test_lookup_invalid_id():
    error = await call_expecting_invalid_params("get_customer_record", {"customer_id": "CUST-1"})
    assert error.message.startswith("Invalid params: customer_id:")
    assert [p["field"] for p in error.data] == ["customer_id"]


async def test_lookup_without_arguments():
    error = await call_expecting_invalid_params("get_customer_record", None)
    assert error.data == [{"field": "customer_id", "message": "Field required"}]


async def test_lookup_unexpected_field():
    error = await call_expecting_invalid_params(
        "get_customer_record", {"customer_id": "CUST-00001", "verbose": True}
    )
    assert error.data == [{"field": "verbose", "message": "unexpected field"}]


# --- tools/call: trigger_refund -----------------------------------------


async def test_refund_success(refund_spy):
    async with connect() as client:
        result = await client.call_tool("trigger_refund", VALID_REFUND)
        assert result.is_error is False
        assert result.structured_content["status"] == "processed"
        assert result.structured_content["refund_reference"].startswith("RF-")
        assert len(refund_spy) == 1


@pytest.mark.parametrize(
    ("arguments", "bad_fields"),
    [
        ({**VALID_REFUND, "amount": 0}, ["amount"]),
        ({**VALID_REFUND, "amount": -1}, ["amount"]),
        ({**VALID_REFUND, "amount": "25.50"}, ["amount"]),
        ({**VALID_REFUND, "reason": "short"}, ["reason"]),
        ({**VALID_REFUND, "customer_id": "CUST-XYZ"}, ["customer_id"]),
        ({**VALID_REFUND, "notify": True}, ["notify"]),
        ({"customer_id": "CUST-00001"}, ["amount", "reason"]),
        ({}, ["customer_id", "amount", "reason"]),
    ],
)
async def test_invalid_refund_is_jsonrpc_invalid_params(refund_spy, arguments, bad_fields):
    error = await call_expecting_invalid_params("trigger_refund", arguments)
    assert [p["field"] for p in error.data] == bad_fields
    assert refund_spy == []


async def test_refund_for_unknown_customer_is_tool_error(refund_spy):
    async with connect() as client:
        result = await client.call_tool(
            "trigger_refund", {**VALID_REFUND, "customer_id": "CUST-99999"}
        )
        assert result.is_error is True
        assert result.content[0].text == "Customer not found: CUST-99999"
        assert refund_spy == []


async def test_repeated_refund_is_processed_again(refund_spy):
    async with connect() as client:
        first = await client.call_tool("trigger_refund", VALID_REFUND)
        second = await client.call_tool("trigger_refund", VALID_REFUND)
        assert first.is_error is False and second.is_error is False
        assert (
            first.structured_content["refund_reference"]
            != second.structured_content["refund_reference"]
        )
        assert len(refund_spy) == 2


async def test_structured_content_matches_output_models():
    async with connect() as client:
        lookup = await client.call_tool("get_customer_record", {"customer_id": "CUST-00002"})
        refund = await client.call_tool("trigger_refund", VALID_REFUND)
        CustomerRecord.model_validate(lookup.structured_content)
        RefundResult.model_validate(refund.structured_content)


# --- errors --------------------------------------------------------------


async def test_unknown_tool_is_jsonrpc_invalid_params():
    async with connect() as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("delete_customer", {"customer_id": "CUST-00001"})
        assert exc_info.value.error.code == types.INVALID_PARAMS
        assert "Unknown tool" in exc_info.value.error.message


async def test_unexpected_exception_is_hidden(monkeypatch):
    async with connect() as client:

        def boom(arguments):
            raise RuntimeError("database password is hunter2")

        original = server_module.TOOLS["get_customer_record"]
        monkeypatch.setitem(
            server_module.TOOLS,
            "get_customer_record",
            server_module.RegisteredTool(definition=original.definition, handler=boom),
        )
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
        assert result.is_error is True
        assert result.content[0].text == "Internal error while running the tool."
        assert "hunter2" not in result.content[0].text


def test_registry_handlers_point_at_tool_functions():
    assert server_module.TOOLS["get_customer_record"].handler is tools.get_customer_record
    assert server_module.TOOLS["trigger_refund"].handler is tools.trigger_refund


def test_tool_input_schema_lookup():
    schema = server_module._tool_input_schema("trigger_refund")
    assert schema == server_module.TOOLS["trigger_refund"].definition.input_schema
    assert server_module._tool_input_schema("missing_tool") is None
