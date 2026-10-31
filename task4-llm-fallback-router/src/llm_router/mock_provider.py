"""Mock OpenAI-compatible provider with a selectable failure mode.

Behaviours: ``ok`` (answer immediately), ``slow`` (answer after ``delay``
seconds), ``429``, ``500``, ``garbage`` (HTTP 200 with a non-JSON body).
``hits`` counts the requests received, so tests can tell which provider served
a request.

Run with ``llm-router-mock-provider``; set ``MOCK_BEHAVIOR``, ``MOCK_PORT`` and
``MOCK_NAME``.
"""

import asyncio
import os
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route


class MockProvider:
    def __init__(self, name: str, behavior: str = "ok", delay: float = 5.0) -> None:
        self.name = name
        self.behavior = behavior
        self.delay = delay
        self.hits = 0
        self.app = Starlette(routes=[Route("/v1/chat/completions", self.handle, methods=["POST"])])

    async def handle(self, request: Request) -> Response:
        self.hits += 1
        payload = await request.json()
        if self.behavior == "429":
            return JSONResponse(
                {"error": {"message": "rate limit", "trace": "at Provider.java:42"}}, 429
            )
        if self.behavior == "500":
            return JSONResponse({"error": "NullPointerException at internal.py:13"}, 500)
        if self.behavior == "garbage":
            return PlainTextResponse("<html>Bad Gateway</html>")
        if self.behavior == "slow":
            await asyncio.sleep(self.delay)
        reply = f"Answer from {self.name}."
        prompt_tokens = sum(len(str(m.get("content", ""))) for m in payload["messages"]) // 4 + 7
        completion_tokens = len(reply) // 4
        return JSONResponse(
            {
                "id": f"cmpl-{self.name}-{self.hits}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model", f"{self.name}-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )


def main() -> None:
    provider = MockProvider(
        os.environ.get("MOCK_NAME", "mock"),
        os.environ.get("MOCK_BEHAVIOR", "ok"),
        float(os.environ.get("MOCK_DELAY", "5")),
    )
    port = int(os.environ.get("MOCK_PORT", "9201"))
    uvicorn.run(provider.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
