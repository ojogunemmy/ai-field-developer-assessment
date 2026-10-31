import json

import httpx
import pytest

from mcp_gateway.config import Settings
from mcp_gateway.proxy import create_app
from mcp_gateway.tokens import issue_token

SECRET = b"test-secret-that-is-at-least-32-bytes-long"
DOWNSTREAM_URL = "http://downstream.test/mcp"


def token(role: str, sub: str = "tester", secret: bytes = SECRET, ttl: int = 300) -> str:
    return issue_token(sub, role, secret, ttl)


def auth(role: str, **kwargs: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(role, **kwargs)}"}  # type: ignore[arg-type]


class FakeDownstream:
    """Records every request the gateway forwards and echoes a JSON-RPC result."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.content_type = "application/json"
        self.body: bytes | None = None
        self.error: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.body is not None:
            content = self.body
        elif request.content:
            message = json.loads(request.content)
            content = json.dumps(
                {"jsonrpc": "2.0", "id": message.get("id"), "result": {"echo": message}}
            ).encode()
        else:
            content = b""
        return httpx.Response(
            self.status_code,
            stream=httpx.ByteStream(content),
            headers={"content-type": self.content_type, "mcp-session-id": "session-123"},
        )

    @property
    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests if r.content]


@pytest.fixture
def downstream() -> FakeDownstream:
    return FakeDownstream()


@pytest.fixture
def settings() -> Settings:
    return Settings(jwt_secret=SECRET, downstream_url=DOWNSTREAM_URL, max_body_bytes=10_000)


@pytest.fixture
async def gateway(settings: Settings, downstream: FakeDownstream):
    downstream_client = httpx.AsyncClient(transport=httpx.MockTransport(downstream.handler))
    app = create_app(settings, client=downstream_client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        yield client
    await downstream_client.aclose()


def rpc(method: str, params: object = None, request_id: object = 1) -> dict[str, object]:
    message: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call_tool(name: object, arguments: object = None, request_id: object = 1) -> dict[str, object]:
    return rpc("tools/call", {"name": name, "arguments": arguments or {}}, request_id)
