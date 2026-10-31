# How the Four Tasks Work Together

The four tasks are building blocks of one system: a platform that lets AI
agents **call tools** and **call models** safely. Each task is a standalone,
separately tested project. This document explains:

- where each task sits in that platform;
- how a request moves through it;
- which connections work today, and what integrating the rest would take.

## 1. The Big Picture

An AI agent does two kinds of work, and each has its own path:

- **Tool path (MCP):** the agent calls tools that act on business systems, such
  as looking up a customer or issuing a refund.
- **Model path (LLM):** the agent sends prompts to a language model and gets
  text back.

```
                              ┌──────────────────────────────────────────────┐
                              │                 AI AGENT                     │
                              └───────────────┬──────────────────┬──────────┘
                                   tool calls │                  │ model calls
                                  (MCP / HTTP)│                  │ (OpenAI-style HTTP)
                                              ▼                  ▼
                      ┌───────────────────────────────┐  ┌─────────────────────────────────┐
  TASK 2              │   MCP Security Gateway        │  │   LLM Guardrail Gateway         │  TASK 3
                      │   • verifies Bearer token     │  │   • streams the response        │
                      │   • blocks admin_* for viewers│  │   • redacts emails, SSNs, cards │
                      └───────────────┬───────────────┘  └───────────────┬─────────────────┘
                                      ▼                                  ▼
                      ┌───────────────────────────────┐  ┌─────────────────────────────────┐
  TASK 1              │   Customer MCP Server         │  │   Fallback Router               │  TASK 4
                      │   • get_customer_record       │  │   • 50k tokens/min per tenant   │
                      │   • trigger_refund            │  │     (SQLite sliding window)     │
                      │   • strict validation         │  │   • primary → secondary on 429  │
                      └───────────────────────────────┘  │     or 3000 ms timeout          │
                                                         └───────┬───────────────┬─────────┘
                                                                 ▼               ▼
                                                          primary model   secondary model
```

| Layer | Task | Question it answers |
|---|---|---|
| Tool access control | 2 | *Is this caller allowed to call this tool?* |
| Tool implementation | 1 | *Are these arguments valid, and what is the result?* |
| Model output safety | 3 | *Does this response contain PII that must not reach the client?* |
| Model access and resilience | 4 | *Does this tenant have budget left, and which model can serve it right now?* |

## 2. The Tool Path: Task 2 → Task 1

```
agent ──POST /mcp + Bearer token──▶ Task 2 gateway ──POST /mcp──▶ MCP server (Task 1 role)
```

The two tasks split the security work between them:

| Concern | Handled by | How |
|---|---|---|
| Who is calling? | **Task 2** | Verifies an HS256 JWT and reads its `role` claim |
| Which tools may they call? | **Task 2** | `admin_*` tools need `role: admin`; otherwise `-32001 Unauthorized Tool Call`, and the call is never forwarded |
| Is the message well-formed JSON-RPC? | **Task 2** and **Task 1** | Both answer `-32700` and `-32600` themselves |
| Are the tool's arguments valid? | **Task 1** | Strict Pydantic models; failures return `-32602` with each failing field |
| Does the customer exist? | **Task 1** | `isError: true` tool result |
| Does the business operation run? | **Task 1** | Only after validation passes |

This is **defence in depth**. The gateway decides *whether* a call may happen;
the server decides *whether its arguments make sense*. Neither trusts the other
to have done its job.

## 3. The Model Path: Task 3 → Task 4

```
agent ──POST /v1/chat/completions──▶ Task 3 guardrail ──Bearer tenant key──▶ Task 4 router ──▶ primary / secondary
agent ◀──── redacted response ────── Task 3 guardrail ◀───── completion ──── Task 4 router ◀──┘
```

The order matters:

- **Task 4 sits next to the providers.** It checks the tenant's token budget
  *before* any money is spent, and it hides provider outages behind failover.
- **Task 3 sits next to the client.** It is the last thing the response passes
  through, so whichever model answered (primary or fallback), the text is
  redacted before the agent sees it.

| Concern | Handled by |
|---|---|
| Tenant identification (API key) | Task 4 |
| Token budget: 50,000 per rolling minute | Task 4 |
| Primary returns 429 or takes over 3000 ms → use secondary | Task 4 |
| Sanitized errors with a `request_id` | Task 4 and Task 3 |
| PII redaction in the response | Task 3 |
| Low time to first token, bounded memory | Task 3 |

## 4. One Request, End to End

An agent is asked to *"Refund CUST-00004 $19.99 for a wrong-size delivery."*

This walkthrough describes the **target deployment**. Steps 2 and 3 need Task 2
connected to Task 1 over HTTP, which isn't built yet; see
[section 6](#6-what-is-connected-today-and-what-integration-needs).

| # | Step | Task | What happens |
|---|---|---|---|
| 1 | Agent asks the model what to do | 3 → 4 | The router checks the tenant's budget and reserves tokens. The primary is rate limited (429), so the router fails over to the secondary. The guardrail redacts any email or card number in the reply as it streams |
| 2 | Agent looks up the customer | 2 → 1 | The gateway verifies the agent's token and forwards `tools/call get_customer_record`. The server validates `CUST-00004` and returns the record |
| 3 | Agent issues the refund | 2 → 1 | The gateway checks the tool name. The server validates the amount and reason, confirms the customer exists, and returns `RF-…` with `status: processed` |
| 4 | Agent tries a forbidden tool | 2 | A viewer-role agent calls `admin_reset_key`. The gateway answers `-32001 Unauthorized Tool Call`, and the downstream server never sees the request |
| 5 | Agent writes the confirmation | 3 → 4 | The router settles step 1's reservation to the real token count. Any customer email the model repeats becomes `[REDACTED]` |

## 5. Shared Design Principles

All four tasks follow the same rules:

| Principle | Task 1 | Task 2 | Task 3 | Task 4 |
|---|---|---|---|---|
| **Validate before acting**: invalid input never reaches the operation | Pydantic, strict mode | Parse and authorize before forwarding | Body validated before calling upstream | Auth and budget checked before any provider call |
| **Standard error codes** | JSON-RPC `-32700`, `-32600`, `-32601`, `-32602` | JSON-RPC `-32001`, `-32600`, `-32602`, `-32603` | One error shape | One error shape with `request_id` |
| **No leaked internals** | Generic "Internal error" | Generic `-32603` for downstream failures | Upstream bodies never forwarded | Stack traces and upstream bodies only in logs |
| **Logs off the data path** | stderr only; stdout is the protocol | stderr; tokens never logged | stderr | stderr; raw API keys never stored |
| **Fail closed** | Unknown fields rejected | No secret → won't start; bad token → 401 | Partial PII at a stream break is discarded, not sent | No tenant keys → won't start |
| **Tests prove "blocked means never reached"** | Spy on `process_refund` | Recording fake downstream | Every split point checked for leaks | Provider hit counters, refund on failure |

## 6. What Is Connected Today, and What Integration Needs

Each task meets its own requirements on its own. The table shows which pairs
can be chained **as they are now**, checked by running them together:

| Connection | Works today? | Notes |
|---|---|---|
| **Task 3 → Task 4**, non-streaming | ✅ Yes, verified | Set Task 3's `UPSTREAM_BASE_URL` to Task 4 and `UPSTREAM_API_KEY` to a Task 4 tenant key. In a test run the primary returned 429, Task 4 failed over, and the completion came back through Task 3 |
| **Task 3 → Task 4**, streaming | ❌ No | Task 4 rejects `stream: true`, and Task 3 reports it as a 400. Task 4 would need streaming support, settling the reservation when the stream ends |
| **Task 2 → Task 1** | ❌ Not directly | Task 2 forwards **HTTP**; Task 1 speaks **STDIO** only, as Task 1 requires. Options below |
| **Task 2 → any HTTP MCP server** | ✅ Yes, verified | Task 2's end-to-end tests use a mock MCP server built with the official SDK |

### Connecting Task 2 to Task 1

1. **Add an HTTP mode to Task 1.** The SDK's low-level `Server`, which Task 1
   already uses, can serve Streamable HTTP. Task 1 would keep STDIO as its
   default and gain a flag to serve on a port instead.
2. **Or run a STDIO-to-HTTP bridge** that starts Task 1 as a subprocess and
   exposes it over HTTP.

Two policy points would also need settling:

- **`trigger_refund` is not `admin_`-prefixed,** so Task 2 would let a viewer
  call it. Either rename it `admin_trigger_refund`, or extend Task 2's policy
  from a prefix rule to an explicit per-tool allowlist.
- **Identity is different on each path.** Task 2 uses JWT roles; Task 4 uses
  tenant API keys. A unified platform would issue one token carrying both
  `role` and `tenant` claims.

## 7. Running Everything Together

The default ports don't overlap, so all four can run at once:

| Component | Command | Port |
|---|---|---|
| Task 1 MCP server | `python -m customer_mcp_server`, started by an MCP client | STDIO |
| Task 2 gateway | `mcp-gateway` (needs `GATEWAY_JWT_SECRET`) | 8080 |
| Task 2 mock MCP server | `mcp-gateway-mock-server` | 9001 |
| Task 3 guardrail | `llm-guardrail-gateway` | 8090 |
| Task 3 mock provider | `llm-mock-provider` | 9100 |
| Task 4 router | `llm-router` (needs `ROUTER_TENANT_KEYS`) | 8070 |
| Task 4 mock providers | `llm-router-mock-provider` (primary and secondary) | 9201, 9202 |

To chain Task 3 in front of Task 4, using Task 4's virtual environment for the
router and Task 3's for the guardrail:

```bash
# Task 4: a primary that returns 429, a healthy secondary, and the router
MOCK_NAME=primary MOCK_BEHAVIOR=429 MOCK_PORT=9201 llm-router-mock-provider &
MOCK_NAME=secondary MOCK_PORT=9202 llm-router-mock-provider &
ROUTER_TENANT_KEYS=guardrail-key llm-router &

# Task 3, pointed at the router
UPSTREAM_BASE_URL=http://127.0.0.1:8070/v1 UPSTREAM_API_KEY=guardrail-key llm-guardrail-gateway &

curl -s localhost:8090/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"m","messages":[{"role":"user","content":"Hi"}]}'
# {"id":"cmpl-secondary-1", ... "content":"Answer from secondary." ...}
```

## 8. Error Codes Across the System

| Code / status | Meaning | Returned by |
|---|---|---|
| JSON-RPC `-32700` | Parse error: not valid JSON | Tasks 1, 2 |
| JSON-RPC `-32600` | Invalid Request: bad envelope, batch, duplicate keys | Tasks 1, 2 |
| JSON-RPC `-32601` | Method not found | Task 1 (SDK) |
| JSON-RPC `-32602` | Invalid params: bad tool arguments or unknown tool | Tasks 1, 2 |
| JSON-RPC `-32603` | Internal error: downstream MCP server unavailable | Task 2 |
| JSON-RPC `-32001` | Unauthorized (bad token) or Unauthorized Tool Call | Task 2 |
| MCP `isError: true` | Valid call that failed, e.g. customer not found | Task 1 |
| HTTP 401 | Missing or invalid credentials | Tasks 2, 4 |
| HTTP 429 + `Retry-After` | Tenant over its token budget, or provider rate limit | Tasks 3, 4 |
| HTTP 413 | Body too large, or request bigger than the whole budget | Tasks 2, 3, 4 |
| HTTP 502 / 503 / 504 | Upstream unavailable or timed out (sanitized) | Tasks 2, 3, 4 |

## 9. Test Totals

| Task | Tests | Coverage | Python versions tested |
|---|---:|---:|---|
| 1 | 151 | 99% | 3.11, 3.15 (plus minimum dependency versions) |
| 2 | 110 | 91% | 3.11, 3.15 |
| 3 | 406 | 90% | 3.11, 3.15 |
| 4 | 68 | 91% | 3.11, 3.15 |
| **Total** | **735** | | |
