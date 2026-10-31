"""The routing module: rate limit, then primary, then secondary.

``ModelRouter.complete`` is the whole decision flow for one request:

    reserve tokens ──▶ primary ──ok──▶ settle to real usage ──▶ response
         │               │
         │            Failover (429 / timeout / outage)
         │               ▼
         │           secondary ──ok──▶ settle ──▶ response
         │               │
         │            Failover ──▶ release reservation ──▶ 503 upstream_unavailable
         │
      RateLimited ──▶ 429 rate_limit_exceeded (Retry-After)

Providers are tried **one after another, never in parallel**: hedging would
double cost and could let both providers bill for the same request.

The reservation is always resolved exactly once. It is settled on success and
released on every other exit, including the caller disconnecting mid-request.
A failed request therefore never consumes the tenant's budget.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from llm_router import errors
from llm_router.config import ProviderConfig
from llm_router.providers import Failover, Rejected, call_provider
from llm_router.ratelimit import (
    RateLimited,
    RequestTooLarge,
    SlidingWindowLimiter,
    retry_after_header,
)
from llm_router.tokens import actual_usage, reservation_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutedCompletion:
    provider: str
    completion: dict[str, Any]
    tokens_used: int
    request_id: str


class ModelRouter:
    def __init__(
        self,
        limiter: SlidingWindowLimiter,
        providers: list[ProviderConfig],
        client: httpx.AsyncClient,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self.limiter = limiter
        self.providers = providers
        self.client = client

    async def complete(
        self, tenant: str, payload: dict[str, Any], request_id: str
    ) -> RoutedCompletion:
        try:
            reservation = await self.limiter.reserve(tenant, reservation_for(payload))
        except RequestTooLarge:
            raise errors.request_too_large(request_id) from None
        except RateLimited as exc:
            logger.info("[%s] tenant %s rate limited", request_id, tenant)
            raise errors.rate_limited(retry_after_header(exc.retry_after), request_id) from None

        settled = False
        try:
            for provider in self.providers:
                try:
                    completion = await call_provider(self.client, provider, payload)
                except Failover as exc:
                    logger.warning(
                        "[%s] provider %s failed (%s); trying next",
                        request_id,
                        provider.name,
                        exc.reason,
                    )
                    continue
                except Rejected as exc:
                    logger.info(
                        "[%s] provider %s rejected request: %s", request_id, provider.name, exc
                    )
                    raise errors.upstream_rejected(request_id) from None

                used = actual_usage(completion, payload)
                await self.limiter.settle(reservation, used)
                settled = True
                logger.info(
                    "[%s] served by %s, %d tokens (reserved %d)",
                    request_id,
                    provider.name,
                    used,
                    reservation.tokens,
                )
                return RoutedCompletion(provider.name, completion, used, request_id)

            logger.error("[%s] all providers failed", request_id)
            raise errors.all_providers_failed(request_id)
        finally:
            if not settled:
                # Shielded so the refund still happens if this task is being cancelled.
                await asyncio.shield(self.limiter.release(reservation))
