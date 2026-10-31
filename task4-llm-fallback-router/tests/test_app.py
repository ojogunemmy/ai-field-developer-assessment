"""The HTTP front end: auth, validation, headers and sanitized errors."""

import subprocess
import sys

import httpx
import pytest
from conftest import OTHER_KEY, TENANT_KEY, HostRouter, chat, make_settings

from llm_router.app import create_app
from llm_router.config import ConfigError, Settings, tenant_id
from llm_router.ratelimit import SlidingWindowLimiter

LEAKS = ["NullPointerException", "internal.py", "Provider.java", "Bad Gateway", "trace"]


@pytest.fixture
def app_client(tmp_path, primary, secondary):
    def build(timeout_ms: int = 300, limit: int = 50_000):
        settings = make_settings(tmp_path, timeout_ms)
        upstream = httpx.AsyncClient(
            transport=HostRouter({"primary.test": primary.app, "secondary.test": secondary.app})
        )
        limiter = SlidingWindowLimiter(settings.db_path, limit=limit)
        app = create_app(settings, client=upstream, limiter=limiter)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test"
        )

    return build


def auth(key: str = TENANT_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_success_headers(app_client):
    async with app_client() as client:
        response = await client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert response.status_code == 200
    assert response.headers["x-router-provider"] == "primary"
    assert response.headers["x-ratelimit-limit-tokens"] == "50000"
    remaining = int(response.headers["x-ratelimit-remaining-tokens"])
    assert 49_900 < remaining < 50_000
    assert response.json()["choices"][0]["message"]["content"] == "Answer from primary."


async def test_failover_is_transparent_to_the_client(app_client, primary):
    primary.behavior = "429"
    async with app_client() as client:
        response = await client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert response.status_code == 200
    assert response.headers["x-router-provider"] == "secondary"


@pytest.mark.parametrize(
    "headers", [{}, auth("wrong-key"), {"Authorization": f"Basic {TENANT_KEY}"}]
)
async def test_unauthenticated(app_client, headers, primary):
    async with app_client() as client:
        response = await client.post("/v1/chat/completions", json=chat(), headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
    assert primary.hits == 0


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"messages": []}',
        b'{"messages": "hi"}',
        b'{"messages": [{"role":"user","content":"x"}], "stream": true}',
        b'{"messages": [{"role":"user","content":"x"}], "max_tokens": -1}',
        b'{"messages": [{"role":"user","content":"x"}], "max_tokens": "10"}',
    ],
)
async def test_invalid_requests(app_client, body, primary):
    async with app_client() as client:
        response = await client.post("/v1/chat/completions", content=body, headers=auth())
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"
    assert primary.hits == 0


async def test_settled_usage_frees_budget_for_next_request(app_client):
    async with app_client(limit=2_000) as client:
        first = await client.post(
            "/v1/chat/completions", json=chat(max_tokens=1_500), headers=auth()
        )
        second = await client.post(
            "/v1/chat/completions", json=chat(max_tokens=1_500), headers=auth()
        )
        third = await client.post(
            "/v1/chat/completions", json=chat(max_tokens=1_500), headers=auth(OTHER_KEY)
        )
    assert first.status_code == 200
    # The first request was settled to its real (small) usage, so the second fits too.
    assert second.status_code == 200
    assert third.status_code == 200  # other tenant, own budget


async def test_rate_limit_rejection_shape(app_client):
    async with app_client(limit=1_100) as client:
        await client.post("/v1/chat/completions", json=chat(max_tokens=1_000), headers=auth())
        response = await client.post(
            "/v1/chat/completions", json=chat(max_tokens=1_090), headers=auth()
        )
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["type"] == "rate_limit_exceeded"
    assert int(response.headers["retry-after"]) == error["retry_after_seconds"] >= 1
    assert response.headers["x-request-id"] == error["request_id"]


async def test_total_failure_is_sanitized(app_client, primary, secondary):
    primary.behavior, secondary.behavior = "500", "garbage"
    async with app_client() as client:
        response = await client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "type": "upstream_unavailable",
            "message": "All model providers are currently unavailable. Please retry.",
            "request_id": response.headers["x-request-id"],
        }
    }
    for leak in LEAKS:
        assert leak not in response.text


async def test_unexpected_error_is_sanitized(app_client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret database path /var/lib/router.db")

    async with app_client() as client:
        transport = client._transport
        monkeypatch.setattr(transport.app.state.router, "complete", boom)
        response = await client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "internal_error"
    assert "secret" not in response.text


async def test_health(app_client):
    async with app_client() as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}


def test_tenant_id_hides_key():
    assert TENANT_KEY not in tenant_id(TENANT_KEY)
    assert tenant_id(TENANT_KEY) != tenant_id(OTHER_KEY)


def test_settings_require_tenant_keys(tmp_path):
    with pytest.raises(ConfigError):
        Settings(
            primary=make_settings(tmp_path).primary,
            secondary=make_settings(tmp_path).secondary,
            tenant_keys=frozenset(),
        )


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ROUTER_TENANT_KEYS", "k1, k2,")
    monkeypatch.setenv("PRIMARY_BASE_URL", "http://p/v1")
    monkeypatch.setenv("SECONDARY_MODEL", "backup")
    settings = Settings.from_env()
    assert settings.tenant_keys == frozenset({"k1", "k2"})
    assert settings.primary.base_url == "http://p/v1"
    assert settings.primary.timeout_ms == 3000
    assert settings.secondary.model == "backup"
    assert settings.tenant_for("k1") == tenant_id("k1")
    assert settings.tenant_for("nope") is None


def test_router_refuses_to_start_without_keys(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-m", "llm_router"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "ROUTER_TENANT_KEYS is not set" in result.stderr
