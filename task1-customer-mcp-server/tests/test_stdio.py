"""End-to-end tests against the server running as a subprocess over STDIO."""

import json
import select
import signal
import subprocess
import sys

import pytest
from mcp import Client, StdioServerParameters

SERVER_CMD = [sys.executable, "-m", "customer_mcp_server"]
READ_TIMEOUT = 10


@pytest.fixture
def server_process():
    proc = subprocess.Popen(
        SERVER_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    yield proc
    # Shut down by closing stdin, as a real client does, so the server exits
    # cleanly (and coverage data from the subprocess is saved).
    if proc.poll() is None:
        if not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(READ_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
    proc.stdout.close()
    proc.stderr.close()


def send(proc, message):
    line = message if isinstance(message, str) else json.dumps(message)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def read_message(proc):
    ready, _, _ = select.select([proc.stdout], [], [], READ_TIMEOUT)
    assert ready, "timed out waiting for a reply on stdout"
    line = proc.stdout.readline()
    message = json.loads(line)  # every stdout line must be valid JSON
    assert message["jsonrpc"] == "2.0"
    return message


def initialize(proc):
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )
    reply = read_message(proc)
    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return reply


def call(proc, request_id, name, arguments):
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    return read_message(proc)


async def test_sdk_client_over_stdio():
    params = StdioServerParameters(command=SERVER_CMD[0], args=SERVER_CMD[1:])
    async with Client(params) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {"get_customer_record", "trigger_refund"}

        lookup = await client.call_tool("get_customer_record", {"customer_id": "CUST-00002"})
        assert lookup.is_error is False
        assert lookup.structured_content["name"] == "Liam Chen"

        refund = await client.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-00002", "amount": 12.0, "reason": "Late delivery refund"},
        )
        assert refund.is_error is False
        assert refund.structured_content["status"] == "processed"


def test_initialize_handshake(server_process):
    reply = initialize(server_process)
    assert reply["id"] == 0
    assert reply["result"]["serverInfo"]["name"] == "customer-mcp-server"
    assert "tools" in reply["result"]["capabilities"]


def test_raw_tools_list(server_process):
    initialize(server_process)
    send(server_process, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    reply = read_message(server_process)
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names == {"get_customer_record", "trigger_refund"}
    for tool in reply["result"]["tools"]:
        assert tool["inputSchema"]["additionalProperties"] is False


def test_raw_validation_error_is_jsonrpc_invalid_params(server_process):
    initialize(server_process)
    reply = call(
        server_process,
        2,
        "trigger_refund",
        {"customer_id": "CUST-00001", "amount": -5, "reason": "bad"},
    )
    assert reply["id"] == 2
    assert "result" not in reply
    assert reply["error"]["code"] == -32602
    assert [p["field"] for p in reply["error"]["data"]] == ["amount", "reason"]


def test_raw_unknown_customer_is_tool_error(server_process):
    initialize(server_process)
    reply = call(server_process, 3, "get_customer_record", {"customer_id": "CUST-99999"})
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"] == "Customer not found: CUST-99999"


def test_raw_unknown_tool_is_jsonrpc_error(server_process):
    initialize(server_process)
    reply = call(server_process, 3, "missing_tool", {})
    assert reply["error"]["code"] == -32602


def test_raw_unknown_method_is_jsonrpc_error(server_process):
    initialize(server_process)
    send(server_process, {"jsonrpc": "2.0", "id": 4, "method": "no/such/method"})
    reply = read_message(server_process)
    assert reply["error"]["code"] == -32601


def test_invalid_json_gets_parse_error(server_process):
    initialize(server_process)
    send(server_process, "this is not json")
    reply = read_message(server_process)
    assert reply["id"] is None
    assert reply["error"]["code"] == -32700


def test_non_jsonrpc_message_gets_invalid_request(server_process):
    initialize(server_process)
    send(server_process, '{"hello": "world"}')
    reply = read_message(server_process)
    assert reply["id"] is None
    assert reply["error"]["code"] == -32600


def test_server_survives_malformed_input(server_process):
    initialize(server_process)
    send(server_process, "this is not json")
    assert read_message(server_process)["error"]["code"] == -32700
    reply = call(server_process, 5, "get_customer_record", {"customer_id": "CUST-00001"})
    assert reply["id"] == 5
    assert reply["result"]["structuredContent"]["customer_id"] == "CUST-00001"


def test_stdout_has_only_protocol_messages_and_logs_go_to_stderr(server_process):
    initialize(server_process)
    call(server_process, 6, "get_customer_record", {"customer_id": "CUST-00001"})
    call(
        server_process,
        7,
        "trigger_refund",
        {
            "customer_id": "CUST-00001",
            "amount": 5,
            "reason": "Damaged packaging",
        },
    )
    server_process.stdin.close()
    server_process.wait(READ_TIMEOUT)

    remaining = server_process.stdout.read()
    for line in remaining.splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"

    stderr = server_process.stderr.read()
    assert "Starting customer-mcp-server" in stderr
    assert "Calling tool trigger_refund" in stderr
    assert "processed for CUST-00001" in stderr
    assert server_process.returncode == 0


def test_sigint_shuts_down_cleanly(server_process):
    initialize(server_process)
    server_process.send_signal(signal.SIGINT)
    server_process.wait(READ_TIMEOUT)
    assert server_process.returncode == 0
    assert "Interrupted, shutting down" in server_process.stderr.read()
