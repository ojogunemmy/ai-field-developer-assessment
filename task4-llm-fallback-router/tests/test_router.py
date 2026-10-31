"""Failover, timeout races and reservation accounting in ModelRouter."""

import asyncio
import time

import httpx
import pytest
from conftest import HostRouter, chat, provider_config

from llm_router import errors
from llm_router.config import DEFAULT_TIMEOUT_MS
from llm_router.router import ModelRouter


def make_router(limiter, primary, secondary, timeout_ms: int = 300) -> ModelRouter:
    client = httpx.AsyncClient(
        transport=HostRouter({"primary.test": primary.app, "secondary.test": secondary.app})
    )
    return ModelRouter(
        limiter,
        [provider_config("primary", timeout_ms), provider_config("secondary", timeout_ms)],
        client,
    )


def test_default_timeout_is_3000ms():
    assert DEFAULT_TIMEOUT_MS == 3000
    assert provider_config("p").timeout_seconds == 3.0


async def test_primary_serves_when_healthy(limiter, primary, secondary):
    result = await make_router(limiter, primary, secondary).complete("t1", chat(), "r1")
    assert result.provider == "primary"
    assert (primary.hits, secondary.hits) == (1, 0)


@pytest.mark.parametrize("behavior", ["429", "500", "garbage"])
async def test_failover_on_primary_error(limiter, primary, secondary, behavior):
    primary.behavior = behavior
    result = await make_router(limiter, primary, secondary).complete("t1", chat(), "r1")
    assert result.provider == "secondary"
    assert (primary.hits, secondary.hits) == (1, 1)


async def test_failover_when_primary_unreachable(limiter, secondary):
    client = httpx.AsyncClient(transport=HostRouter({"secondary.test": secondary.app}))
    router = ModelRouter(
        limiter, [provider_config("primary"), provider_config("secondary")], client
    )
    assert (await router.complete("t1", chat(), "r1")).provider == "secondary"


async def test_failover_on_timeout(limiter, primary, secondary):
    primary.behavior, primary.delay = "slow", 5
    start = time.monotonic()
    result = await make_router(limiter, primary, secondary, timeout_ms=300).complete(
        "t1", chat(), "r1"
    )
    elapsed = time.monotonic() - start
    assert result.provider == "secondary"
    assert 0.3 <= elapsed < 1.5  # gave up on the primary at the deadline, not after 5s


async def test_primary_just_inside_deadline_is_used(limiter, primary, secondary):
    primary.behavior, primary.delay = "slow", 0.1
    result = await make_router(limiter, primary, secondary, timeout_ms=400).complete(
        "t1", chat(), "r1"
    )
    assert result.provider == "primary"
    assert secondary.hits == 0


async def test_real_3000ms_deadline(limiter, primary, secondary):
    primary.behavior, primary.delay = "slow", 10
    start = time.monotonic()
    result = await make_router(limiter, primary, secondary, timeout_ms=3000).complete(
        "t1", chat(), "r1"
    )
    elapsed = time.monotonic() - start
    assert result.provider == "secondary"
    assert 3.0 <= elapsed < 4.0


async def test_no_failover_on_rejected_request(limiter, primary, secondary):
    rejecting = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "bad"}))
    router = ModelRouter(
        limiter,
        [provider_config("primary"), provider_config("secondary")],
        httpx.AsyncClient(transport=rejecting),
    )
    with pytest.raises(errors.GatewayError) as exc_info:
        await router.complete("t1", chat(), "r1")
    assert exc_info.value.status == 400
    assert await limiter.usage("t1") == 0


async def test_both_fail_returns_503_and_refunds(limiter, primary, secondary):
    primary.behavior, secondary.behavior = "429", "500"
    with pytest.raises(errors.GatewayError) as exc_info:
        await make_router(limiter, primary, secondary).complete("t1", chat(), "r42")
    error = exc_info.value
    assert (error.status, error.type, error.request_id) == (503, "upstream_unavailable", "r42")
    assert await limiter.usage("t1") == 0  # nothing charged


async def test_success_charges_actual_usage_not_estimate(limiter, primary, secondary):
    result = await make_router(limiter, primary, secondary).complete(
        "t1", chat(max_tokens=4000), "r1"
    )
    assert result.tokens_used < 50
    assert await limiter.usage("t1") == result.tokens_used


async def test_rate_limit_is_checked_before_any_provider_call(limiter, primary, secondary):
    await limiter.reserve("t1", 49_990)
    with pytest.raises(errors.GatewayError) as exc_info:
        await make_router(limiter, primary, secondary).complete("t1", chat(), "r1")
    assert exc_info.value.status == 429
    assert exc_info.value.retry_after == 60
    assert primary.hits == secondary.hits == 0


async def test_oversized_request(limiter, primary, secondary):
    with pytest.raises(errors.GatewayError) as exc_info:
        await make_router(limiter, primary, secondary).complete("t1", chat(max_tokens=60_000), "r1")
    assert exc_info.value.status == 413


async def test_cancelled_request_releases_reservation(limiter, primary, secondary):
    primary.behavior, primary.delay = "slow", 5
    router = make_router(limiter, primary, secondary, timeout_ms=3000)
    task = asyncio.create_task(router.complete("t1", chat(), "r1"))
    await asyncio.sleep(0.2)
    assert await limiter.usage("t1") > 0  # reserved while in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert await limiter.usage("t1") == 0


def test_router_needs_a_provider(limiter):
    with pytest.raises(ValueError):
        ModelRouter(limiter, [], httpx.AsyncClient())


async def test_model_override_is_sent(limiter):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [], "usage": {"total_tokens": 5}})

    provider = provider_config("primary")
    provider = type(provider)(
        name="primary", base_url=provider.base_url, api_key="k", model="backup-4o"
    )
    router = ModelRouter(
        limiter, [provider], httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await router.complete("t1", chat(), "r1")
    assert b'"model":"backup-4o"' in seen[0].content.replace(b" ", b"")
    assert seen[0].headers["authorization"] == "Bearer k"


@pytest.mark.parametrize("body", [b"[]", b'{"no": "choices"}'])
async def test_non_completion_json_fails_over(limiter, secondary, body):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.test":
            return httpx.Response(200, content=body)
        return await httpx.ASGITransport(app=secondary.app).handle_async_request(request)

    router = ModelRouter(
        limiter,
        [provider_config("primary"), provider_config("secondary")],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (await router.complete("t1", chat(), "r1")).provider == "secondary"
    assert secondary.hits == 1
