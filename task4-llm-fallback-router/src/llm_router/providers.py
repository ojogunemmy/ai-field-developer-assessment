"""Calling one model provider, with a hard deadline.

``call_provider`` either returns the provider's completion or raises one of:

- ``Failover``: the provider is unusable *right now* (429, timeout, connection
  failure, 5xx, or an unreadable response). The router should try the next
  provider.
- ``Rejected``: the provider refused the request itself (400, 404, 422).
  Another provider would refuse it too, so the router should stop.

The deadline covers the **whole** call: connecting, sending, waiting and
reading the body. It is enforced with ``asyncio.timeout``, which cancels the
in-flight HTTP request when it fires, so a late primary response can never be
used after the router has moved on to the secondary.
"""

import asyncio
import logging
from typing import Any

import httpx

from llm_router.config import ProviderConfig

logger = logging.getLogger(__name__)

REJECTED_STATUSES = frozenset({400, 404, 422})


class Failover(Exception):
    """The provider failed in a way another provider might not."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Rejected(Exception):
    """The provider rejected the request as invalid."""


async def call_provider(
    client: httpx.AsyncClient, provider: ProviderConfig, payload: dict[str, Any]
) -> dict[str, Any]:
    body = {**payload, "model": provider.model} if provider.model else payload
    headers = {"authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
    url = provider.base_url.rstrip("/") + "/chat/completions"
    try:
        async with asyncio.timeout(provider.timeout_seconds):
            response = await client.post(url, json=body, headers=headers)
    except TimeoutError as exc:
        raise Failover(f"timeout after {provider.timeout_ms}ms") from exc
    except httpx.TimeoutException as exc:
        raise Failover("timeout") from exc
    except httpx.HTTPError as exc:
        raise Failover(f"connection error ({type(exc).__name__})") from exc

    if response.status_code == 429:
        raise Failover("HTTP 429 Too Many Requests")
    if response.status_code in REJECTED_STATUSES:
        raise Rejected(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise Failover(f"HTTP {response.status_code}")
    try:
        completion = response.json()
    except ValueError as exc:
        raise Failover("response is not JSON") from exc
    if not isinstance(completion, dict) or not isinstance(completion.get("choices"), list):
        raise Failover("response is not a chat completion")
    return completion
