"""Mock OpenAI-compatible LLM provider for demos and tests.

It replies with the last user message, echoed back, so a caller controls exactly
what text (and PII) comes out. Streaming replies are cut into small random
chunks with a short delay between them, which splits PII across chunk
boundaries the way real token streams do.

Run it with ``llm-mock-provider`` (listens on 127.0.0.1:9100).
"""

import asyncio
import json
import os
import random
import time
import uuid
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


def _reply_text(payload: dict[str, object]) -> str:
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                return str(message.get("content", ""))
    return "Hello!"


def create_mock_provider(
    delay: float = 0.02, max_chunk: int = 5, seed: int | None = None
) -> Starlette:
    rng = random.Random(seed)

    async def completions(request: Request) -> Response:
        payload = await request.json()
        text = _reply_text(payload)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        base = {
            "id": completion_id,
            "created": int(time.time()),
            "model": payload.get("model", "mock-model"),
        }
        if not payload.get("stream"):
            return JSONResponse(
                {
                    **base,
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        async def events() -> AsyncIterator[bytes]:
            def chunk(delta: dict[str, str], finish: str | None = None) -> bytes:
                body = {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return f"data: {json.dumps(body)}\n\n".encode()

            yield chunk({"role": "assistant", "content": ""})
            position = 0
            while position < len(text):
                size = rng.randint(1, max_chunk)
                yield chunk({"content": text[position : position + size]})
                position += size
                await asyncio.sleep(delay)
            yield chunk({}, finish="stop")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])


def main() -> None:
    port = int(os.environ.get("MOCK_PROVIDER_PORT", "9100"))
    uvicorn.run(create_mock_provider(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
