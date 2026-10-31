import httpx
import pytest

from llm_router.config import ProviderConfig, Settings
from llm_router.mock_provider import MockProvider
from llm_router.ratelimit import SlidingWindowLimiter

TENANT_KEY = "tenant-a-key"
OTHER_KEY = "tenant-b-key"


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class HostRouter(httpx.AsyncBaseTransport):
    """Sends each request to the ASGI app registered for its host."""

    def __init__(self, apps: dict[str, object]) -> None:
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self.transports.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no route to {request.url.host}", request=request)
        return await transport.handle_async_request(request)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(tmp_path, clock) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(tmp_path / "usage.db", limit=50_000, window_seconds=60, clock=clock)


@pytest.fixture
def primary() -> MockProvider:
    return MockProvider("primary")


@pytest.fixture
def secondary() -> MockProvider:
    return MockProvider("secondary")


def provider_config(name: str, timeout_ms: int = 3000) -> ProviderConfig:
    return ProviderConfig(name=name, base_url=f"http://{name}.test/v1", timeout_ms=timeout_ms)


def make_settings(tmp_path, timeout_ms: int = 3000) -> Settings:
    return Settings(
        primary=provider_config("primary", timeout_ms),
        secondary=provider_config("secondary", timeout_ms),
        tenant_keys=frozenset({TENANT_KEY, OTHER_KEY}),
        db_path=str(tmp_path / "router.db"),
    )


def chat(content: str = "Hello there", **extra: object) -> dict[str, object]:
    return {"model": "gpt-test", "messages": [{"role": "user", "content": content}], **extra}
