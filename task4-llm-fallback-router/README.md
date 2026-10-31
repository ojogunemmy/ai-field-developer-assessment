# LLM Fallback Router

The model-routing module of an LLM gateway. For each completion request it:

1. **rate-limits per tenant API key** with a token-aware sliding window
   (default 50,000 tokens per minute), stored in **SQLite on disk**;
2. sends the request to the **primary** model, with a hard **3000 ms** deadline;
3. **fails over to the secondary** model if the primary returns **429**, times
   out, or is otherwise unavailable;
4. returns either the completion or a **standardized, sanitized error**.

Assessment Task 4: *Build a Rate-Limiting & Model Fallback Router for LLM Gateways*.

```
POST /v1/chat/completions (Bearer tenant key)
        │
        ▼
  reserve tokens in SQLite ──over limit──▶ 429 rate_limit_exceeded + Retry-After
        │
        ▼
  primary (≤3000 ms) ──200──▶ settle real usage ──▶ completion
        │ 429 / timeout / 5xx / unreachable
        ▼
  secondary (≤3000 ms) ──200──▶ settle real usage ──▶ completion
        │ fails too
        ▼
  release reservation ──▶ 503 upstream_unavailable
```

## Quick Start

```bash
cd ai-field-developer-assessment/task4-llm-fallback-router
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# A primary that is too slow, and a healthy secondary
MOCK_NAME=primary MOCK_BEHAVIOR=slow MOCK_DELAY=10 MOCK_PORT=9201 llm-router-mock-provider &
MOCK_NAME=secondary MOCK_PORT=9202 llm-router-mock-provider &

ROUTER_TENANT_KEYS=key-acme,key-globex llm-router &    # :8070, database ./router.db

curl -s -i localhost:8070/v1/chat/completions -H "Authorization: Bearer key-acme" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-x","max_tokens":100,"messages":[{"role":"user","content":"Hi"}]}'
# HTTP 200 after ~3.0 s
# x-router-provider: secondary
# x-ratelimit-remaining-tokens: 49988
```

Mock behaviours: `ok`, `slow` (waits `MOCK_DELAY` seconds), `429`, `500`,
`garbage` (HTTP 200 with a non-JSON body).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ROUTER_TENANT_KEYS` | **required** | Comma-separated tenant API keys. The router refuses to start without them |
| `ROUTER_TOKENS_PER_MINUTE` | `50000` | Per-tenant budget in any rolling 60 seconds |
| `ROUTER_DB_PATH` | `router.db` | SQLite file. Share it between worker processes to share limits |
| `ROUTER_PRIMARY_TIMEOUT_MS` | `3000` | Deadline per provider call |
| `PRIMARY_BASE_URL` / `SECONDARY_BASE_URL` | `http://127.0.0.1:9201/v1` / `:9202/v1` | OpenAI-compatible providers |
| `PRIMARY_API_KEY` / `SECONDARY_API_KEY` | none | Sent as `Authorization: Bearer ...` |
| `PRIMARY_MODEL` / `SECONDARY_MODEL` | none | If set, replaces the request's `model` for that provider |
| `ROUTER_HOST`, `ROUTER_PORT` | `127.0.0.1`, `8070` | Listen address |

## Rate Limiter (`ratelimit.py`)

### Sliding window, token-aware

Usage is a log of `(tenant, timestamp, tokens)` rows. A tenant's current usage
is the sum of its rows newer than `now − 60 s`. Unlike a fixed per-minute
counter, this has **no boundary burst**: a tenant can't spend 50k at 12:00:59
and another 50k at 12:01:01.

### Reserve → settle or release

The real token count is only known after the provider answers, so:

| Step | When | Effect |
|---|---|---|
| `reserve` | before calling any provider | Inserts a row for the worst case: estimated prompt tokens + `max_tokens` (default 1024). Raises `RateLimited` if it wouldn't fit |
| `settle` | provider answered | Replaces the estimate with the provider's `usage.total_tokens` |
| `release` | no provider served it, or the client disconnected | Deletes the row; a failed request costs nothing |

Reserving the worst case means concurrent in-flight requests can't
collectively overshoot the budget. Settling means the budget isn't wasted
afterwards: a request reserving 1,108 tokens that used 12 is charged 12.

The prompt estimate is ~4 characters per token plus a small per-message
overhead (`tokens.py`). It only needs to be roughly right, because it's replaced
by the real count.

### Atomicity and concurrency

Check-and-insert runs in a single **`BEGIN IMMEDIATE`** transaction, which takes
SQLite's write lock *before* reading the current sum. Concurrent requests, from
coroutines, threads or **separate worker processes** sharing the database file,
are serialized and can never collectively exceed the limit. The database uses
WAL mode, and each operation uses a short-lived connection. Blocking SQLite
calls run in a worker thread (`asyncio.to_thread`), so the event loop is never
blocked.

### Eviction

Every `reserve` deletes **all** expired rows, for every tenant, using an index
on `ts`. The table therefore holds about one window of data, and expired rows
never affect a sum because every query filters on `ts > now − window`.

### `Retry-After`

When a request is refused, the limiter walks the tenant's rows oldest-first and
returns the time until enough of them expire to free the shortfall. This is
sent as both the `Retry-After` header and `retry_after_seconds` in the body.

## Failover (`providers.py`, `router.py`)

| Primary outcome | Action |
|---|---|
| HTTP 200 with a chat completion | Use it |
| **HTTP 429** | **Fail over** |
| **No complete response within 3000 ms** | **Fail over** |
| HTTP 5xx, connection refused, non-JSON or non-completion body | Fail over |
| HTTP 400, 404 or 422 | Stop: the request itself is invalid, so another provider would refuse it too. Return 400 |

### Timeout race conditions

- The 3000 ms deadline covers the **whole** call (connect, send, wait, read
  body), enforced with `asyncio.timeout`. A provider can't stall the router by
  trickling a slow body.
- When the deadline fires, the in-flight HTTP request is **cancelled**, so a
  late primary response can't be used after the router has moved on. The
  secondary is only called after the primary attempt has fully ended.
- Providers are tried **sequentially, never in parallel**. Hedged requests
  would cut latency but could bill both providers for one completion.
- If the client disconnects mid-request, the reservation is still released:
  the release is `asyncio.shield`-ed against the cancellation.

## Error Payload (`errors.py`)

Every error has one shape:

```json
{"error": {"type": "upstream_unavailable",
           "message": "All model providers are currently unavailable. Please retry.",
           "request_id": "57c5ba64795b4178"}}
```

| Situation | HTTP | `type` |
|---|---|---|
| Missing or unknown API key | 401 | `authentication_error` |
| Invalid body, `stream: true`, bad `max_tokens` | 400 | `invalid_request` |
| Provider rejected the request (400/404/422) | 400 | `invalid_request` |
| Tenant over budget | 429 + `Retry-After` | `rate_limit_exceeded` (+ `retry_after_seconds`) |
| One request needs more than the whole budget | 413 | `request_too_large` |
| Primary and secondary both failed | 503 | `upstream_unavailable` |
| Any unexpected exception | 500 | `internal_error` |

**No upstream detail is ever returned**: no provider status lines, bodies,
exception messages or stack traces. They are logged with the same `request_id`,
so support can find them. The tests feed the mock providers bodies like
`NullPointerException at internal.py:13` and check they never reach the client.

Successful responses carry `x-request-id`, `x-router-provider`,
`x-ratelimit-limit-tokens` and `x-ratelimit-remaining-tokens`.

## Project Layout

```
src/llm_router/
├── __main__.py       Entry point (uvicorn)
├── config.py         Settings; tenant IDs are SHA-256 hashes of the API key
├── errors.py         GatewayError: the standardized error payload
├── tokens.py         Token estimation and reading real usage
├── ratelimit.py      SQLite sliding-window limiter: reserve / settle / release
├── providers.py      One provider call with a hard deadline; Failover vs Rejected
├── router.py         ModelRouter: limiter → primary → secondary
├── app.py            Starlette front end: auth, validation, headers
└── mock_provider.py  Mock provider with ok / slow / 429 / 500 / garbage modes
```

## Testing

```bash
pytest --cov        # 68 tests, 91% coverage (router.py and errors.py: 100%)
ruff check . && ruff format --check . && mypy
```

| File | Covers |
|---|---|
| `test_ratelimit.py` | Limit boundary; per-tenant isolation; sliding (not fixed) window; no boundary burst; exact `Retry-After`; settle and release; eviction; persistence across restarts; **20 threads with separate connections racing on one file: exactly 50 × 1,000 tokens admitted** |
| `test_tokens.py` | Estimation, multi-part content, budgets, real vs estimated usage |
| `test_router.py` | Failover on 429, 500, garbage, unreachable and timeout; a response just inside the deadline is kept; **a real 3000 ms deadline test**; no failover on 400; refund on total failure and on cancellation; actual-usage settlement |
| `test_app.py` | Auth, validation, rate-limit response shape, sanitized 503 and 500, headers, config, refusing to start without keys |

## Limitations

- **Non-streaming only.** `stream: true` is rejected; streaming would need the
  reservation settled at the end of the stream.
- **Approximate pre-call estimate.** Prompt tokens are estimated at ~4
  characters per token. A tokenizer such as `tiktoken` would be more exact, but
  the estimate is corrected after each call anyway.
- **`Retry-After` is conservative while requests are in flight.** It assumes
  in-flight reservations use their full estimate, so a client may be told to
  wait longer than strictly necessary.
- **Single SQLite file.** Fine for one host with several workers. Several hosts
  would need a shared store such as Redis or Postgres.
- **Two providers, fixed order.** No health tracking or circuit breaker, so a
  primary that's down costs each request up to 3000 ms before failover. A
  circuit breaker would be the next improvement.
