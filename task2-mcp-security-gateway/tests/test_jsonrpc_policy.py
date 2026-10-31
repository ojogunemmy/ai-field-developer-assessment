import json

import pytest

from mcp_gateway.jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    PARSE_ERROR,
    UNAUTHORIZED,
    JsonRpcError,
    parse_message,
)
from mcp_gateway.policy import authorize, is_admin_tool
from mcp_gateway.tokens import Principal

ADMIN = Principal("root", "admin")
VIEWER = Principal("guest", "viewer")


def parse(obj: object):
    return parse_message(json.dumps(obj).encode())


def code_of(body: bytes) -> int:
    with pytest.raises(JsonRpcError) as exc_info:
        parse_message(body)
    return exc_info.value.code


# --- parsing --------------------------------------------------------------


def test_parses_request():
    message = parse({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert message.method == "tools/list"
    assert message.request_id == 7
    assert message.has_id


def test_parses_notification_and_response():
    assert not parse({"jsonrpc": "2.0", "method": "notifications/initialized"}).has_id
    assert parse({"jsonrpc": "2.0", "id": "a", "result": {}}).method is None


@pytest.mark.parametrize("body", [b"", b"not json", b"{", b'{"a":}'])
def test_invalid_json_is_parse_error(body):
    assert code_of(body) == PARSE_ERROR


@pytest.mark.parametrize(
    "obj",
    [
        "a string",
        42,
        None,
        {"id": 1, "method": "tools/list"},
        {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": {"x": 1}, "method": "ping"},
    ],
)
def test_invalid_envelope_is_invalid_request(obj):
    assert code_of(json.dumps(obj).encode()) == INVALID_REQUEST


def test_batch_is_rejected():
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    assert code_of(json.dumps(batch).encode()) == INVALID_REQUEST


def test_duplicate_keys_are_rejected():
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"get_status","name":"admin_reset_key"}}'
    )
    assert code_of(body) == INVALID_REQUEST


def test_error_keeps_request_id_when_known():
    with pytest.raises(JsonRpcError) as exc_info:
        parse_message(b'{"jsonrpc":"1.0","id":"abc","method":"x"}')
    assert exc_info.value.to_response()["id"] == "abc"


# --- policy ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("admin_reset_key", True),
        ("ADMIN_reset_key", True),
        ("Admin_list_users", True),
        ("admin_", True),
        ("get_status", False),
        ("reset_admin_key", False),
        ("administrator", False),
        ("admin-reset", False),
    ],
)
def test_is_admin_tool(name, expected):
    assert is_admin_tool(name) is expected


def tool_call(name: object, request_id: object = 1):
    return parse(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name}}
    )


@pytest.mark.parametrize("principal", [ADMIN, VIEWER])
def test_tools_list_always_allowed(principal):
    authorize(principal, parse({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))


@pytest.mark.parametrize("principal", [ADMIN, VIEWER])
def test_regular_tool_allowed_for_everyone(principal):
    authorize(principal, tool_call("get_status"))


def test_admin_tool_allowed_for_admin():
    authorize(ADMIN, tool_call("admin_reset_key"))


@pytest.mark.parametrize("name", ["admin_reset_key", "ADMIN_RESET_KEY", "Admin_list_users"])
def test_admin_tool_denied_for_viewer(name):
    with pytest.raises(JsonRpcError) as exc_info:
        authorize(VIEWER, tool_call(name, request_id=99))
    assert exc_info.value.code == UNAUTHORIZED
    assert exc_info.value.to_response() == {
        "jsonrpc": "2.0",
        "id": 99,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }


@pytest.mark.parametrize("params", [None, {}, {"name": 5}, {"name": None}, ["admin_x"], "admin_x"])
def test_tools_call_without_string_name_is_invalid_params(params):
    message = parse({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    with pytest.raises(JsonRpcError) as exc_info:
        authorize(ADMIN, message)
    assert exc_info.value.code == INVALID_PARAMS


@pytest.mark.parametrize("method", ["initialize", "ping", "resources/list", "prompts/list"])
def test_other_methods_pass_through(method):
    authorize(VIEWER, parse({"jsonrpc": "2.0", "id": 1, "method": method}))
