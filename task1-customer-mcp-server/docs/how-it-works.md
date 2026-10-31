# How the Customer MCP Server Works

This guide explains the project's structure, what each file does, and how a
request travels through the code from start to finish.

## 1. The Big Picture

```
┌────────────────────┐   JSON-RPC over stdin/stdout   ┌──────────────────────────┐
│ MCP client         │ ─────────────────────────────▶ │ customer-mcp-server      │
│ (Claude, Inspector,│ ◀───────────────────────────── │ (a Python process)       │
│  Claude Code ...)  │        one JSON per line       └──────────────────────────┘
└────────────────────┘                                     │ logs go to stderr only
```

There's no web server and no URL. The client **launches** the server as a child
process (`python -m customer_mcp_server`) and talks to it by writing JSON lines
to its **stdin** and reading replies from its **stdout**. That's why stdout must
carry only protocol messages and every log goes to stderr.

## 2. The File Tree

```
task1-customer-mcp-server/
├── pyproject.toml            Project definition: dependencies, `customer-mcp-server` command, tool settings
├── requirements.lock         Exact runtime versions (tested)
├── requirements-dev.lock     Exact runtime + dev versions (tested)
├── README.md                 User guide
├── .gitignore
│
├── src/customer_mcp_server/  ◀── THE APPLICATION
│   ├── __init__.py           Package version (0.1.0)
│   ├── __main__.py           ① Entry point: sets up logging, Ctrl+C, starts the server
│   ├── logging_config.py     ② Sends all logs to stderr
│   ├── server.py             ③ MCP layer: tool list, request handling, error replies, STDIO
│   ├── tools.py              ④ Tool logic: validate → check rules → run
│   ├── models.py             ⑤ Pydantic rules for inputs and outputs
│   ├── data.py               ⑥ Mock customer "database"
│   └── refunds.py            ⑦ Mock refund processor
│
├── tests/                    ◀── 151 TESTS
│   ├── conftest.py           Shared fixtures (refund spy, reset refunds)
│   ├── test_logging.py       ②
│   ├── test_models.py        ⑤
│   ├── test_data.py          ⑥
│   ├── test_refunds.py       ⑦
│   ├── test_tools.py         ④
│   ├── test_server.py        ③ through an in-memory MCP client
│   └── test_stdio.py         Everything, as a real subprocess
│
└── docs/                     This guide
```

The code is layered. Each layer only calls the one below it:

```
__main__  →  server  →  tools  →  models (validation)
                                →  data / refunds (business operations)
```

## 3. What Each File Does

### ① `__main__.py`: Startup

When you run `python -m customer_mcp_server`, Python runs `main()`, which:

1. calls `configure_logging()` so logs go to stderr;
2. installs a Ctrl+C handler that exits immediately (without it, Ctrl+C would
   wait for the next line of input);
3. calls `anyio.run(run_stdio)` to start the server loop.

### ② `logging_config.py`: Keeping stdout Clean

It attaches a single log handler pointing at `sys.stderr` and removes any
others. The level comes from the `LOG_LEVEL` environment variable and defaults
to INFO.

### ③ `server.py`: The MCP Layer

This is the bridge between the protocol and the code.

- **`TOOLS`** is a registry with one entry per tool. Each entry holds:
  - the tool's **definition** (name, description, `input_schema`,
    `output_schema`, hints like "read-only");
  - the Python **handler** function in `tools.py` to call.

  The schemas are generated from the Pydantic models, so the rules the client
  sees always match what the server enforces.
- **`handle_list_tools`** answers `tools/list` by returning every definition.
- **`handle_call_tool`** answers `tools/call`:
  - unknown tool name → JSON-RPC error `-32602`;
  - otherwise it runs the handler. `InvalidArguments` becomes JSON-RPC error
    `-32602`, with each failing field in `data`. A `ToolError` becomes a result
    with `isError: true` and the message. Any other crash is logged, and the
    client gets a generic "Internal error" so internal details don't leak.
  - success → the result is sent twice: as JSON text in `content`, and as an
    object in `structuredContent`.
- **`_answer_unparseable`** sits between stdin and the server. It catches lines
  that aren't valid JSON (→ `-32700`) or aren't JSON-RPC (→ `-32600`). The SDK
  alone would drop these silently.
- **`run_stdio`** wires it all together: it opens the SDK's stdio streams,
  starts the filter, and runs the server until stdin closes.

It uses the SDK's **low-level `Server`**, not the simpler `MCPServer`.
`MCPServer` quietly ignores unknown fields and turns `"25.50"` into `25.5`, which
would break the strict-validation requirement.

### ④ `tools.py`: The Tool Logic

Each tool receives the **raw** `arguments` dict, exactly as the client sent it.

**`get_customer_record`**

1. Validates with `CustomerLookupInput` and raises `InvalidArguments` if that
   fails.
2. Looks the customer up and raises `ToolError("Customer not found: ...")` if
   missing.
3. Checks the record against `CustomerRecord` and returns it.

**`trigger_refund`**: the checks run in order, and the first failure stops
everything.

1. Validate with `RefundInput` (raises `InvalidArguments`).
2. The customer exists (raises `ToolError`).
3. **Only then** call `refunds.process_refund()`.

`validation_problems` turns Pydantic errors into a list like
`[{"field": "amount", "message": "Input should be greater than 0"}]`. It never
repeats back the value the client sent.

### ⑤ `models.py`: The Rules

**Input models** (`StrictInput` base: `extra="forbid"` rejects unknown fields,
`strict=True` means no type conversion):

| Model | Field | Rule |
|---|---|---|
| `CustomerLookupInput` | `customer_id` | Matches `^CUST-[0-9]{5}$` |
| `RefundInput` | `customer_id` | Same pattern |
| | `amount` | Number (positive float), > 0, finite |
| | `reason` | ≥ 10 non-whitespace characters, then trimmed |

**Output models**: `CustomerRecord` and `RefundResult` describe what each tool
returns. They're published as `outputSchema`.

### ⑥ `data.py`: Mock Database

A dict of 5 customers. `get_customer()` returns a **copy**, so nothing can change
the stored data by accident.

### ⑦ `refunds.py`: Mock Payments

`process_refund()` creates a reference like `RF-69D59076AD05` from a random
UUID, stamps the time, appends the refund to the `PROCESSED_REFUNDS` list and
logs it. No money moves. The list lives in memory, so it resets when the server
restarts. Each call creates a new refund.

## 4. One Request, End to End

A successful refund, step by step:

```
1. The client writes to stdin:
   {"jsonrpc":"2.0","id":7,"method":"tools/call",
    "params":{"name":"trigger_refund",
              "arguments":{"customer_id":"CUST-00004","amount":19.99,"reason":"Wrong size delivered"}}}

2. SDK stdio reader      parses the line → valid JSON-RPC
3. _answer_unparseable   valid → passes it on
4. SDK Server            method is "tools/call" → handle_call_tool
5. handle_call_tool      "trigger_refund" is in TOOLS → calls tools.trigger_refund(arguments)
6. tools.trigger_refund
     RefundInput           ✔ format, ✔ 19.99 > 0 and finite, ✔ 20 chars
     customer exists       ✔ CUST-00004
     process_refund()      → RF-69D59076AD05, saved to PROCESSED_REFUNDS, logged to stderr
     RefundResult          ✔ shape matches the output schema
7. handle_call_tool      wraps it: content=[JSON text], structuredContent={...}, isError=false
8. SDK                   writes one JSON line to stdout:
   {"jsonrpc":"2.0","id":7,"result":{"content":[...],"structuredContent":{...},"isError":false}}
```

If the request had `"amount": 0`, step 6 would stop at **RefundInput** and raise
`InvalidArguments`. Step 7 would turn that into a JSON-RPC error instead of a
result, and `process_refund()` would never run:

```
{"jsonrpc":"2.0","id":7,"error":{"code":-32602,
 "message":"Invalid params: amount: Input should be greater than 0",
 "data":[{"field":"amount","message":"Input should be greater than 0"}]}}
```

## 5. How Errors Are Reported

| Problem | Where it's caught | What the client gets |
|---|---|---|
| Line isn't JSON | `_answer_unparseable` | JSON-RPC error `-32700` |
| JSON but not JSON-RPC | `_answer_unparseable` | JSON-RPC error `-32600` |
| Unknown method | SDK | JSON-RPC error `-32601` |
| Unknown tool | `handle_call_tool` | JSON-RPC error `-32602` |
| Bad, missing or unexpected arguments | `tools.py` (`InvalidArguments`) | JSON-RPC error `-32602`, failing fields in `data` |
| Valid arguments, customer not found | `tools.py` (`ToolError`) | A normal result with `isError: true` |
| Unexpected crash | `handle_call_tool` | `isError: true`, "Internal error..." (details only in the log) |

The split is deliberate. Requests that break the **protocol or the input
schema** become JSON-RPC errors, as the assessment requires. Valid requests that
**fail while running** become results with `isError: true`.

## 6. How the Tests Map to the Layers

| Test file | Layer it checks | How |
|---|---|---|
| `test_models.py` | ⑤ rules | Calls the Pydantic models directly with good and bad values |
| `test_data.py`, `test_refunds.py` | ⑥ ⑦ | Calls the functions directly |
| `test_tools.py` | ④ | Calls the handlers with raw dicts |
| `test_server.py` | ③ | A real MCP client connected in memory, with no subprocess |
| `test_stdio.py` | Everything | Starts the actual process and sends raw JSON lines |
| `test_logging.py` | ② | Checks stderr vs stdout |

A key piece is the **`refund_spy`** fixture in `tests/conftest.py`. It wraps
`process_refund` and records every call. Every rejection test then asserts the
spy recorded **nothing**, which proves bad requests never reach the refund step.

## 7. Running It Yourself

```bash
source .venv/bin/activate
pytest                                   # run all 151 tests
python -m customer_mcp_server            # start the server (waits for JSON on stdin; Ctrl+C to stop)
npx @modelcontextprotocol/inspector .venv/bin/python -m customer_mcp_server   # browser UI
```

The quickest way to learn the code is to open `src/customer_mcp_server/tools.py`,
which holds the business logic and is short, then follow its calls into
`models.py`, `data.py` and `refunds.py`. Read `server.py` last.
