# MCP Security Gateway

An HTTP reverse proxy that sits between an AI agent and a downstream MCP server.
It authenticates every request with a Bearer token and blocks `admin_*` tool
calls unless the caller's role is `admin`. Blocked calls are answered by the
gateway itself and **never reach the downstream server**.

Assessment Task 2: *Implement an MCP Security Gateway Proxy (Tool Filtering & Auth)*.

```
AI agent ──HTTP + Bearer token──▶ Gateway ──HTTP (no token)──▶ Downstream MCP server
                                    │
                                    ├─ no/invalid token          → 401, JSON-RPC -32001
                                    ├─ tools/list                → forwarded unchanged
                                    ├─ tools/call admin_*, viewer → JSON-RPC -32001, not forwarded
                                    └─ everything else            → forwarded
```

## Quick Start

```bash
cd ai-field-developer-assessment/task2-mcp-security-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export GATEWAY_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

mcp-gateway-mock-server &          # mock downstream MCP server on :9001
mcp-gateway &                      # gateway on :8080, forwarding to :9001

VIEWER=$(mcp-gateway-token --role viewer --sub alice)
ADMIN=$(mcp-gateway-token --role admin --sub root)
```

Then:

```bash
H=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")

# Viewer calling an admin tool: blocked by the gateway
curl -s localhost:8080/mcp "${H[@]}" -H "Authorization: Bearer $VIEWER" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
# {"jsonrpc":"2.0","id":3,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}

# Admin calling the same tool: forwarded
curl -s localhost:8080/mcp "${H[@]}" -H "Authorization: Bearer $ADMIN" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
# {"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"key k1 rotated"}],...}}
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_JWT_SECRET` | **required** | HMAC key for verifying tokens, at least 32 bytes. The gateway refuses to start without it |
| `GATEWAY_DOWNSTREAM_URL` | `http://127.0.0.1:9001/mcp` | The MCP server to forward to |
| `GATEWAY_HOST`, `GATEWAY_PORT` | `127.0.0.1`, `8080` | Listen address |
| `GATEWAY_MAX_BODY_BYTES` | `1000000` | Larger request bodies get HTTP 413 |
| `GATEWAY_DOWNSTREAM_TIMEOUT` | `30` | Seconds to wait for the downstream server |
| `LOG_LEVEL` | `INFO` | Logs go to stderr. Tokens are never logged |

## How a Request Is Handled

Every request to `/mcp` goes through four steps in order. A failure at any step
ends the request there.

| # | Step | On failure |
|---|---|---|
| 1 | **Authenticate**: read `Authorization: Bearer <token>`, verify it, extract the role | HTTP 401, `WWW-Authenticate: Bearer`, JSON-RPC `-32001` |
| 2 | **Parse** the body as one JSON-RPC 2.0 message (POST only) | JSON-RPC `-32700` (bad JSON) or `-32600` (bad envelope, batch, duplicate keys) |
| 3 | **Authorize** with `policy.authorize` | JSON-RPC `-32001 Unauthorized Tool Call`, or `-32602` if `params.name` isn't a string |
| 4 | **Forward** the checked message and stream the response back | HTTP 502 or 504 with JSON-RPC `-32603`, with no upstream details |

GET (the server-to-client event stream) and DELETE (ending a session) carry no
JSON-RPC body. They are authenticated, then forwarded.

### Authorization Rules (`policy.py`)

| Method | Viewer | Admin |
|---|---|---|
| `tools/list` | forwarded unchanged | forwarded unchanged |
| `tools/call` with a regular name | forwarded | forwarded |
| `tools/call` with a name starting `admin_` | **`-32001`, not forwarded** | forwarded |
| anything else (`initialize`, `ping`, notifications) | forwarded | forwarded |

### Tokens (`tokens.py`)

Tokens are HS256 JWTs with `sub`, `role` (`admin` or `viewer`) and `exp` claims,
verified with the Python standard library. A token is rejected if:

- its algorithm isn't `HS256`, including `alg: none`;
- its signature doesn't match (checked in constant time);
- it has no `exp` claim, or has expired;
- its `role` isn't `admin` or `viewer`.

Issue tokens with `mcp-gateway-token --role viewer --sub alice [--ttl 3600]`.

## Security Decisions

| Decision | Why |
|---|---|
| The downstream server receives the **re-serialized** message, not the raw bytes | The downstream server sees exactly what the gateway checked, so parser differences can't be exploited |
| **Duplicate JSON keys are rejected** | `{"name":"get_status","name":"admin_reset_key"}` could be read differently by two parsers |
| **`admin_` matching ignores case** | `ADMIN_reset_key` can't slip past the check |
| **Batches are rejected** | MCP removed JSON-RPC batching in 2025-06-18, and a batch could hide an admin call among allowed ones |
| **Request headers use an allowlist** | `Authorization`, `Cookie` and other headers never reach the downstream server. Only `Accept`, `Content-Type`, `Mcp-Session-Id`, `MCP-Protocol-Version` and `Last-Event-ID` are forwarded |
| **No default secret** | A missing or short `GATEWAY_JWT_SECRET` stops the gateway from starting |
| **Downstream errors are sanitized** | Clients get a generic `-32603`. Details only go to the log |
| **Body size limit** | Oversized bodies are rejected before parsing |

### Why `-32001` Comes Back with HTTP 200

A refused tool call is a JSON-RPC-level outcome for an authenticated caller, so
the gateway returns it as a normal JSON-RPC error response. MCP clients then
surface it as an error for that call. The official Python MCP client, for
example, raises `MCPError(code=-32001, message="Unauthorized Tool Call")`, which
the end-to-end tests check. A missing or invalid token is an HTTP
authentication failure, so that case uses HTTP 401.

## Project Layout

```
src/mcp_gateway/
├── __main__.py     Entry point (uvicorn)
├── config.py       Settings from environment variables
├── tokens.py       Bearer parsing, JWT verification and issuing, CLI
├── jsonrpc.py      JSON-RPC parsing and error construction
├── policy.py       Authorization decision (no HTTP)
├── proxy.py        The Starlette app: authenticate → parse → authorize → forward
└── mock_server.py  Mock downstream MCP server (official SDK): get_status, admin_reset_key, admin_list_users
```

## Testing

```bash
pytest --cov        # 110 tests, 91% coverage
ruff check . && ruff format --check . && mypy
```

| File | Covers |
|---|---|
| `test_tokens.py` | Valid tokens, forged signatures, tampered roles, `alg: none`, expiry, malformed tokens, header parsing, CLI |
| `test_jsonrpc_policy.py` | JSON-RPC parsing (parse errors, envelopes, batches, duplicate keys) and every authorization rule |
| `test_proxy.py` | The full HTTP pipeline against a recording fake downstream: blocked calls send **zero** downstream requests; header allowlist; SSE pass-through; status codes; sanitized failures |
| `test_end_to_end.py` | Real servers on local ports. The **official MCP client** connects through the gateway to the SDK-built mock server and checks that blocked admin calls never run |

## Limitations

- **One downstream server.** Routing to several servers by path or tool name is
  out of scope.
- **Static role model.** Two roles, defined in `tokens.py`. There is no
  per-tool access list beyond the `admin_` prefix rule.
- **Symmetric tokens (HS256).** Gateway and token issuer share a secret. A
  production setup would more likely verify RS256/ES256 tokens from an identity
  provider using its public keys (JWKS).
- **`tools/list` is not filtered.** As the task specifies, viewers still see the
  `admin_` tools; they just can't call them.
