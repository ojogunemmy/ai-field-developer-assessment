# Customer MCP Server

A Python [Model Context Protocol](https://modelcontextprotocol.io) server that
lets AI agents look up customer records and trigger mock refunds. It runs over
STDIO, validates every input strictly with Pydantic, and keeps stdout free of
anything except MCP protocol messages.

All data and refund processing are simulated. No real payments are made.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Running the Server](#running-the-server)
- [Tools](#tools)
- [Validation Rules](#validation-rules)
- [Error Handling](#error-handling)
- [Testing](#testing)
- [Testing with MCP Inspector](#testing-with-mcp-inspector)
- [Connecting an MCP Client](#connecting-an-mcp-client)
- [Architecture](#architecture)
- [Logging](#logging)
- [Assumptions and Limitations](#assumptions-and-limitations)

## Requirements

- Python 3.11 or later. Tested on 3.11.18 and 3.15.2; CI covers 3.11–3.15 on
  Linux and macOS
- Node.js, only for MCP Inspector

## Installation

```bash
cd ai-field-developer-assessment/task1-customer-mcp-server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
```

To install the exact versions the project was tested with, use the lockfile:

```bash
pip install -r requirements-dev.lock   # or requirements.lock for runtime only
pip install --no-deps -e .
```

Dependencies are declared in `pyproject.toml`. Each minimum is the lowest
version the test suite passes with, which CI checks:

| Package | Constraint | Purpose |
|---|---|---|
| `mcp` | `>=2.3,<3` | Official MCP Python SDK. 2.0–2.2 lack an API the server uses |
| `pydantic` | `>=2.12` | Input and output validation |
| `anyio` | `>=4.9` | Async runtime used by the SDK |
| `pytest`, `pytest-asyncio`, `pytest-cov` | dev | Tests and coverage |
| `ruff`, `mypy` | dev | Linting, formatting and type checking |

The lockfiles are generated with
`uv pip compile pyproject.toml [--extra dev] --universal --python-version 3.11`.

## Running the Server

```bash
python -m customer_mcp_server
# or
customer-mcp-server
```

The server reads JSON-RPC messages from stdin and writes replies to stdout. It
is meant to be launched by an MCP client, not used interactively. It exits when
stdin closes or on Ctrl+C (SIGINT).

Set the log level with `LOG_LEVEL`. The default is `INFO`.

```bash
LOG_LEVEL=DEBUG python -m customer_mcp_server
```

## Tools

### `get_customer_record`

Retrieves a customer's record, whatever its status. This tool is read-only and
idempotent.

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "customer_id": { "type": "string", "pattern": "^CUST-[0-9]{5}$" }
  },
  "required": ["customer_id"],
  "additionalProperties": false
}
```

**Example call:**

```json
{ "customer_id": "CUST-00001" }
```

**Successful result (`structuredContent`):**

```json
{
  "customer_id": "CUST-00001",
  "name": "Ada Okafor",
  "email": "ada.okafor@example.com",
  "status": "active",
  "tier": "gold",
  "created_at": "2023-02-14",
  "total_spent": 4820.5
}
```

The same JSON is also returned as a text block in `content`, for clients that
don't read `structuredContent`. The shape is published as the tool's
`outputSchema`, generated from the `CustomerRecord` model.

### `trigger_refund`

Issues a mock refund to an existing customer. The refund runs only after the
input passes validation (see [Validation Rules](#validation-rules)) and the
customer is confirmed to exist. This tool is not idempotent: each call creates a
new refund with a new reference.

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "customer_id": { "type": "string", "pattern": "^CUST-[0-9]{5}$" },
    "amount":      { "type": "number", "exclusiveMinimum": 0 },
    "reason":      { "type": "string" }
  },
  "required": ["customer_id", "amount", "reason"],
  "additionalProperties": false
}
```

**Example call:**

```json
{ "customer_id": "CUST-00002", "amount": 49.99, "reason": "Order cancelled before shipping" }
```

**Successful result (`structuredContent`):**

```json
{
  "refund_reference": "RF-2FE683C2356A",
  "status": "processed",
  "customer_id": "CUST-00002",
  "amount": 49.99,
  "reason": "Order cancelled before shipping",
  "processed_at": "2026-10-31T04:49:46+00:00"
}
```

The shape is published as the tool's `outputSchema`, generated from the
`RefundResult` model.

### Mock Customers

| ID | Name | Status | Tier |
|---|---|---|---|
| CUST-00001 | Ada Okafor | active | gold |
| CUST-00002 | Liam Chen | active | silver |
| CUST-00003 | Sofia Martins | suspended | bronze |
| CUST-00004 | Noah Adeyemi | active | platinum |
| CUST-00005 | Emma Schulz | closed | bronze |

The `status` field is informational. Any existing customer can be looked up and
refunded.

## Validation Rules

All tool input is validated by strict Pydantic models in `models.py` **before**
any lookup or refund runs. Strict mode means no type coercion. For example,
the string `"25.50"` is not accepted as a number.

| Field | Rule | Rejected examples |
|---|---|---|
| `customer_id` | `CUST-` followed by exactly 5 ASCII digits. Case-sensitive, no surrounding whitespace | `CUST-1234`, `cust-00001`, `CUST-ABCDE`, `" CUST-00001"`, `CUST-١٢٣٤٥`, `12345` |
| `amount` | A JSON number (a positive float), greater than 0, finite | `0`, `-1`, `Infinity`, `NaN`, `"25.50"`, `true`, `null` |
| `reason` | A string with at least 10 non-whitespace characters. Surrounding whitespace is trimmed | `"short"`, `"          "`, `"a b c d e f g h i"` |
| any other field | Rejected | `{"currency": "USD"}` |

The input schemas published through `tools/list` are generated from the same
models, so clients see the same rules the server enforces. JSON Schema cannot
express "finite" or "non-whitespace characters", so those two rules are stated
in the field descriptions and enforced on the server.

## Error Handling

| Situation | Response |
|---|---|
| Invalid, missing or unexpected arguments | JSON-RPC error `-32602` (Invalid params). `data` lists each failing field |
| Unknown tool name | JSON-RPC error `-32602` (Invalid params): `Unknown tool: <name>` |
| Unknown method | JSON-RPC error `-32601` (Method not found), from the SDK |
| Line on stdin that is not valid JSON | JSON-RPC error `-32700` (Parse error) with `id: null` |
| Valid JSON that is not a JSON-RPC message | JSON-RPC error `-32600` (Invalid Request) with `id: null` |
| Valid arguments, but the customer does not exist | Tool result with `isError: true`: `Customer not found: CUST-XXXXX` |
| Unexpected exception in a tool | Traceback logged to stderr. Tool result with `isError: true` and a generic `Internal error while running the tool.` |

The rule is: **requests that break the protocol or the input schema get a
JSON-RPC error**, and **valid requests that fail while running get a tool result
with `isError: true`**. Error messages name the fields that failed but never
echo the rejected values.

In both parse-error cases the server keeps running and answers later requests.
The SDK on its own drops such lines silently, so `server.py` puts a small filter
between the transport and the server that answers them.

**A refund runs only after** validation passes and the customer is confirmed to
exist. Tests check that the refund operation is never called for any rejected
request.

### Example validation error

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "error": {
    "code": -32602,
    "message": "Invalid params: amount: Input should be greater than 0; reason: reason must contain at least 10 non-whitespace characters",
    "data": [
      { "field": "amount", "message": "Input should be greater than 0" },
      { "field": "reason", "message": "reason must contain at least 10 non-whitespace characters" }
    ]
  }
}
```

### Example tool execution error

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "content": [{ "type": "text", "text": "Customer not found: CUST-99999" }],
    "isError": true
  }
}
```

## Testing

```bash
pytest               # 151 tests
pytest -v            # one line per test
pytest --cov         # with coverage (fails below 95%)
```

| File | Tests | Covers |
|---|---:|---|
| `test_logging.py` | 3 | Logs go to stderr, `LOG_LEVEL`, empty stdout on startup |
| `test_data.py` | 5 | Mock data and lookup |
| `test_models.py` | 64 | Every validation rule, including edge cases |
| `test_refunds.py` | 3 | Refund details, recording, unique references |
| `test_tools.py` | 39 | Tool handlers; rejected input never reaches the refund operation |
| `test_server.py` | 25 | `tools/list` and `tools/call` through the SDK client, in-process, including `-32602` errors and output schemas |
| `test_stdio.py` | 12 | The real subprocess over STDIO: raw JSON-RPC, every error code, stdout and stderr separation, Ctrl+C shutdown |

Coverage is 99%. It includes the server subprocesses started by the STDIO
tests, because `pyproject.toml` sets `patch = ["subprocess"]` for coverage.

### Code Quality

```bash
ruff check .          # lint
ruff format --check . # formatting
mypy                  # strict type checking of src/
```

All three pass. CI ([../.github/workflows/ci.yml](../.github/workflows/ci.yml), at the repository root) runs
them, the test suite on Python 3.11–3.15 on Linux and macOS, and a run with the
minimum allowed dependency versions.

## Testing with MCP Inspector

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the
official interactive testing tool. Run these commands from the project root
with the virtual environment created.

**Browser UI:**

```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m customer_mcp_server
```

These steps match Inspector v2.13.0.

1. Open the full URL printed in the terminal. It includes the access token.
2. On the **Servers** tab, the server card (labelled `python`) should show
   **Connected** over STDIO. If not, switch it on with its toggle.
3. Open the **Tools** tab. It should list **Tools (2)**.
4. Select **Get customer record**, enter `CUST-00001` and click
   **Execute Tool**. The record appears under Results and Structured Output.
5. Select **Trigger refund** and try valid and invalid input. To send values
   the form would block, such as amount `0`, switch on **Edit as JSON**:

   ```json
   { "customer_id": "CUST-00001", "amount": 0, "reason": "short" }
   ```

6. The **Protocol** panel on the right shows each JSON-RPC message. Expand one
   to see the full response, including `isError` and `structuredContent`.

**CLI mode (no browser):**

```bash
npx @modelcontextprotocol/inspector --cli .venv/bin/python -m customer_mcp_server \
  --method tools/list

npx @modelcontextprotocol/inspector --cli .venv/bin/python -m customer_mcp_server \
  --method tools/call --tool-name get_customer_record --tool-arg customer_id=CUST-00001

npx @modelcontextprotocol/inspector --cli .venv/bin/python -m customer_mcp_server \
  --method tools/call --tool-name trigger_refund \
  --tool-arg customer_id=CUST-00004 --tool-arg amount=19.99 \
  --tool-arg "reason=Wrong size delivered"
```

The server's log lines appear in the Inspector's terminal because it passes the
server's stderr through. They are not part of the protocol stream.

## Connecting an MCP Client

Any STDIO MCP client can launch the server. Use the absolute path to the
virtual environment's Python. For example, in a Claude Desktop-style config:

```json
{
  "mcpServers": {
    "customer": {
      "command": "/absolute/path/to/ai-field-developer-assessment/task1-customer-mcp-server/.venv/bin/python",
      "args": ["-m", "customer_mcp_server"]
    }
  }
}
```

For Claude Code:

```bash
claude mcp add customer -- /absolute/path/to/ai-field-developer-assessment/task1-customer-mcp-server/.venv/bin/python -m customer_mcp_server
```

## Architecture

```
src/customer_mcp_server/
├── __main__.py        Entry point: configures logging, runs the STDIO server
├── server.py          MCP layer: tool registry, tools/list and tools/call handlers, error mapping
├── tools.py           Tool handlers: validate raw arguments, then call the business layer
├── models.py          Strict Pydantic input models and the output models (source of the published schemas)
├── data.py            Mock customer store and lookup
├── refunds.py         Mock refund processor and reference generator
└── logging_config.py  stderr-only logging
```

Request flow for `tools/call`:

```
stdin ─▶ SDK (JSON-RPC parsing)
            │ unparseable line ─▶ server._answer_unparseable ─▶ JSON-RPC error -32700 / -32600
            ▼
         server.handle_call_tool
            │ unknown tool ─▶ JSON-RPC error -32602
            ▼
         tools.<handler>(raw arguments)
            │ Pydantic validation fails ─▶ JSON-RPC error -32602 (field details in data)
            │ customer missing          ─▶ isError result
            ▼
         data.get_customer / refunds.process_refund
            ▼
         output model (CustomerRecord / RefundResult)
            ▼
stdout ◀─ SDK ◀─ CallToolResult (text + structuredContent)
```

Each layer can be tested on its own: models without MCP, tools without a
transport, the server in-process, and the full stack as a subprocess.

### Why the low-level SDK `Server`

The SDK's higher-level `MCPServer` (formerly `FastMCP`) builds an argument
model from each function's signature. That model **ignores unknown fields** and
**pre-parses JSON strings**, so `"25.50"` becomes `25.5`. Both behaviours
conflict with the strict-validation requirement. The low-level `Server` passes
the raw `arguments` dict to our handlers, so our strict models are the only
validation layer and invalid input cannot slip through.

## Logging

When running over STDIO, stdout carries only JSON-RPC messages, and any other
output there would corrupt the protocol. Logging is therefore protected in two
ways:

1. `configure_logging()` attaches a single stderr handler to the root logger
   and removes any others.
2. The SDK's `stdio_server` points file descriptor 1 at stderr while it is
   serving, so even a stray `print()` cannot reach the protocol stream.

Logs record tool names, outcomes and refund references. Argument values are not
logged, because they may contain customer data.

## Assumptions and Limitations

- **Mock data only.** Customers live in an in-memory dict, and refunds are
  recorded in an in-memory list that is lost on restart.
- **Only the rules the task asks for.** Refunds need valid input and an
  existing customer, nothing more. There are no amount caps, status checks,
  duplicate detection or idempotency keys, so repeating a call creates a second
  refund.
- **No currency.** Amounts are plain floats, stored and returned as received.
- **Customer ID format.** IDs are `CUST-` plus 5 digits, case-sensitive.
  Lowercase IDs are rejected rather than normalised.
- **Out of scope.** Authentication, HTTP transport, real payment providers and
  external databases.
- **Shutdown.** The server stops when stdin closes. Replies to requests still in
  flight at that moment may be lost; normal MCP clients keep stdin open until
  they have their replies. Ctrl+C exits immediately, because the SDK reads stdin
  in a worker thread that cannot be cancelled and nothing needs saving.
- **No Windows CI.** The STDIO tests use `select()` on pipes, which Windows
  does not support. The server itself has no platform-specific code.
