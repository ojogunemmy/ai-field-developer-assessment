# LLM Guardrail Gateway

An OpenAI-compatible `/v1/chat/completions` proxy that streams model output to
the client while redacting **email addresses, US Social Security numbers and
credit card numbers** in real time. The full response is never buffered.

Assessment Task 3: *Implement an LLM Gateway Streaming Guardrail (PII Redaction)*.

```
Client ──POST stream:true──▶ Gateway ──▶ LLM provider
Client ◀── SSE, PII → [REDACTED] ── Gateway ◀── SSE chunks
```

Example from a real run. The provider split the SSN across four chunks:

```
upstream chunks:  ", SS"  "N 123"  "-45-6"  "7"  "89, "  "card" ...
client receives:  Reach me at [REDACTED], SSN [REDACTED], card [REDACTED].
```

## Quick Start

```bash
cd ai-field-developer-assessment/task3-llm-guardrail-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

llm-mock-provider &        # mock LLM on :9100 that echoes your message in random small chunks
llm-guardrail-gateway &    # gateway on :8090 -> :9100

curl -sN localhost:8090/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "mock", "stream": true,
  "messages": [{"role": "user", "content": "Mail jane.doe@example.com, SSN 123-45-6789"}]
}'
```

To use a real provider, point the gateway at any OpenAI-compatible API:

```bash
UPSTREAM_BASE_URL=https://api.openai.com/v1 UPSTREAM_API_KEY=sk-... llm-guardrail-gateway
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:9100/v1` | OpenAI-compatible provider base URL |
| `UPSTREAM_API_KEY` | none | Sent upstream as `Authorization: Bearer ...` |
| `UPSTREAM_CONNECT_TIMEOUT` | `5` | Seconds to connect |
| `UPSTREAM_READ_TIMEOUT` | `60` | Maximum seconds **between chunks**, not for the whole response |
| `GATEWAY_HOST`, `GATEWAY_PORT` | `127.0.0.1`, `8090` | Listen address |

## How the Streaming Redaction Works

### The problem

Tokens arrive in small pieces, and PII is often split across them:
`"john.doe@exa"` then `"mple.com"`. A filter that only looks at one chunk at a
time misses it. A filter that waits for the whole response defeats streaming.

### The approach: hold back only what could still become PII

`StreamRedactor` (in `redactor.py`) keeps a small buffer. For every delta:

1. Append the delta to the buffer.
2. Find the **earliest position from which the end of the buffer could still be
   part of PII**: a trailing run of email characters (`[A-Za-z0-9._%+-]`,
   optionally followed by `@` and a domain), or a trailing run of digits with
   spaces or hyphens.
3. Everything **before** that position is final. Redact it and send it now.
4. Keep the suffix and wait for more text.
5. At the end of the stream, `flush()` redacts and sends the remainder.

```
buffer: "Contact me at jane.doe@exa"
                       └─ could still become an email → held
sent:   "Contact me at "

next delta "mple.com today" → buffer "jane.doe@example.com today"
                                                         └─ "today" held
sent:   "[REDACTED] "
```

Ordinary prose is delayed by at most **one partial word**, because a space or
punctuation mark ends the held suffix.

### Patterns

| Type | Pattern | Notes |
|---|---|---|
| Email | `local@domain.tld` | The TLD must be 2+ letters, so `user@localhost` is not matched |
| SSN | `ddd-dd-dddd` or `ddd dd dddd` | Not preceded or followed by another digit, so `555-123-4567` (a phone number) is not matched |
| Card | 13–19 digits, optionally grouped with spaces or hyphens | Redacted only if it passes the **Luhn check**, so order numbers and timestamps are left alone |

### Latency and memory

| Concern | How it's handled |
|---|---|
| **Time to first token** | Each upstream event is forwarded as soon as it is parsed. Only the trailing partial word or number is held. The real-socket test checks that first content arrives in under 20% of the total stream time |
| **Memory** | The redactor's buffer is capped at `MAX_HOLDBACK` (256) characters, whatever the response length. Tested on a stream of over 1 MB |
| **SSE parsing** | `SSEParser` keeps only the incomplete line between reads, and decodes UTF-8 incrementally so multi-byte characters split across reads are reassembled |
| **Regex cost** | Each delta scans only the small buffer, never the whole response |
| **Proxy buffering** | Responses send `Cache-Control: no-cache` and `X-Accel-Buffering: no`, so reverse proxies such as nginx don't hold the stream |

### Event handling

- Each upstream chunk is re-emitted with its `delta.content` replaced by the
  text the redactor released. All other fields (`id`, `model`, `role`,
  `finish_reason`) are kept.
- **Each choice** (`n > 1`) has its own redactor, so text from one choice can't
  complete PII in another.
- When a choice sends `finish_reason`, its held text is flushed into that event.
- If the upstream sends `[DONE]` or closes without a `finish_reason`, held text
  is flushed in a final event before `[DONE]`.
- Unparseable upstream events are dropped and logged.

## Error Handling

All errors use one sanitized shape. Upstream bodies, stack traces and exception
messages are never passed to the client:

```json
{"error": {"type": "upstream_error", "message": "The model provider returned an error.", "request_id": "3f9a1c2b7d4e"}}
```

| Situation | HTTP | `type` |
|---|---|---|
| Body is not JSON, or has no `messages` list | 400 | `invalid_request` |
| Body over 1 MB | 413 | `invalid_request` |
| Provider returns 429 | 429 | `rate_limited` |
| Provider returns 400, 404 or 422 | 400 | `invalid_request` |
| Provider returns any other error | 502 | `upstream_error` |
| Provider unreachable | 502 | `upstream_unavailable` |
| Provider connect timeout | 504 | `upstream_timeout` |
| Stream breaks after it started | in-band `data: {"error": ...}` event | `upstream_error` |

If the stream breaks midway, any held text is **discarded rather than flushed**:
it may be the start of PII that will never be completed.

## Project Layout

```
src/llm_guardrail/
├── __main__.py       Entry point (uvicorn)
├── config.py         Settings from environment variables
├── redactor.py       Patterns, Luhn check, StreamRedactor
├── sse.py            Incremental SSE parser
├── gateway.py        Starlette app: StreamRelay (per-choice redaction) and error mapping
└── mock_provider.py  Mock OpenAI-compatible provider that echoes the user message in random chunks
```

## Testing

```bash
pytest --cov        # 406 tests, 90% coverage (redactor.py: 100%)
ruff check . && ruff format --check . && mypy
```

| File | Covers |
|---|---|
| `test_redactor.py` | Patterns and false positives; **every possible two-way split** of each sample, character-by-character streams and 200 random chunkings, each compared with one-shot redaction; no released text ever contains a secret or its leading fragment; bounded buffer on a stream over 1 MB |
| `test_sse.py` | Every byte split, including inside a multi-byte UTF-8 character; comments; multi-line data; unterminated final events |
| `test_gateway.py` | Streaming and non-streaming redaction; metadata preserved; per-choice redactors; flush without `finish_reason`; mid-stream failure; sanitized errors; **real-socket time-to-first-token** |

## Limitations

- **Pattern-based.** Only emails, SSNs and Luhn-valid card numbers are
  detected. Names, addresses and phone numbers are not. A production guardrail
  would add an NER model or a service such as Presidio.
- **Text content only.** `delta.content` is redacted. Tool-call arguments and
  `logprobs` are passed through unchanged.
- **SSN formats.** Only the separated forms (`123-45-6789`, `123 45 6789`). A
  bare 9-digit number is too ambiguous to redact without context.
- **No authentication** on the gateway itself; that belongs to the MCP Security
  Gateway (Task 2) or an API gateway in front.
- **Requests are not inspected.** Only responses are redacted, as the task asks.
