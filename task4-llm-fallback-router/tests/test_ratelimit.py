import asyncio
import sqlite3
import threading
from contextlib import closing

import pytest

from llm_router.ratelimit import (
    RateLimited,
    RequestTooLarge,
    SlidingWindowLimiter,
    retry_after_header,
)


def test_allows_up_to_the_limit(limiter):
    for _ in range(5):
        limiter.reserve_sync("t1", 10_000)
    assert limiter.usage_sync("t1") == 50_000
    with pytest.raises(RateLimited):
        limiter.reserve_sync("t1", 1)


def test_tenants_are_independent(limiter):
    limiter.reserve_sync("t1", 50_000)
    limiter.reserve_sync("t2", 50_000)
    with pytest.raises(RateLimited):
        limiter.reserve_sync("t1", 1)


def test_window_slides_entry_by_entry(limiter, clock):
    limiter.reserve_sync("t1", 30_000)  # t=0
    clock.advance(30)
    limiter.reserve_sync("t1", 20_000)  # t=30, now full
    with pytest.raises(RateLimited):
        limiter.reserve_sync("t1", 1)
    clock.advance(30.001)  # first entry leaves the window; second has 30s to go
    assert limiter.usage_sync("t1") == 20_000
    limiter.reserve_sync("t1", 30_000)
    with pytest.raises(RateLimited):
        limiter.reserve_sync("t1", 1)


def test_no_boundary_burst(limiter, clock):
    """A fixed per-minute bucket would allow 100k across a minute boundary."""
    clock.now = 1_000_059.0  # one second before a "minute boundary"
    limiter.reserve_sync("t1", 50_000)
    clock.advance(2)  # now past the boundary
    with pytest.raises(RateLimited):
        limiter.reserve_sync("t1", 50_000)


def test_retry_after_points_to_when_enough_expires(limiter, clock):
    limiter.reserve_sync("t1", 20_000)  # t=0
    clock.advance(10)
    limiter.reserve_sync("t1", 20_000)  # t=10
    clock.advance(10)
    limiter.reserve_sync("t1", 10_000)  # t=20, total 50k
    clock.advance(5)  # t=25
    with pytest.raises(RateLimited) as exc_info:
        limiter.reserve_sync("t1", 25_000)  # needs 25k freed: entries at t=0 and t=10
    assert exc_info.value.retry_after == pytest.approx(45)  # t=10 entry expires at t=70
    clock.advance(45.001)
    limiter.reserve_sync("t1", 25_000)


def test_rejected_request_records_nothing(limiter):
    limiter.reserve_sync("t1", 50_000)
    with pytest.raises(RateLimited):
        limiter.reserve_sync("t1", 100)
    assert limiter.usage_sync("t1") == 50_000


def test_request_larger_than_limit(limiter):
    with pytest.raises(RequestTooLarge):
        limiter.reserve_sync("t1", 50_001)
    assert limiter.row_count_sync() == 0


def test_settle_replaces_estimate_with_actual(limiter):
    reservation = limiter.reserve_sync("t1", 40_000)
    limiter.settle_sync(reservation, 1_200)
    assert limiter.usage_sync("t1") == 1_200
    limiter.reserve_sync("t1", 48_000)


def test_release_refunds(limiter):
    reservation = limiter.reserve_sync("t1", 50_000)
    limiter.release_sync(reservation)
    assert limiter.usage_sync("t1") == 0


def test_expired_entries_are_evicted(limiter, clock):
    for tenant in ("t1", "t2", "t3"):
        limiter.reserve_sync(tenant, 100)
    assert limiter.row_count_sync() == 3
    clock.advance(61)
    limiter.reserve_sync("t9", 1)  # any reservation evicts expired rows for all tenants
    assert limiter.row_count_sync() == 1


def test_state_is_on_disk_and_shared(tmp_path, clock):
    path = tmp_path / "shared.db"
    SlidingWindowLimiter(path, clock=clock).reserve_sync("t1", 45_000)
    restarted = SlidingWindowLimiter(path, clock=clock)  # e.g. after a restart
    assert restarted.usage_sync("t1") == 45_000
    with pytest.raises(RateLimited):
        restarted.reserve_sync("t1", 10_000)
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT tenant, tokens FROM token_usage").fetchall()
    assert rows == [("t1", 45_000)]


def test_concurrent_reservations_never_overshoot(tmp_path, clock):
    """Separate limiter instances (separate connections, like separate worker
    processes) racing on one database file must not exceed the limit."""
    path = tmp_path / "race.db"
    SlidingWindowLimiter(path, clock=clock)
    successes, failures = [], []
    barrier = threading.Barrier(20)

    def worker() -> None:
        limiter = SlidingWindowLimiter(path, clock=clock)
        barrier.wait()
        for _ in range(10):
            try:
                limiter.reserve_sync("shared", 1_000)
                successes.append(1)
            except RateLimited:
                failures.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 50  # exactly 50 x 1,000 = 50,000
    assert len(failures) == 150
    assert SlidingWindowLimiter(path, clock=clock).usage_sync("shared") == 50_000


async def test_async_api_under_concurrency(limiter):
    async def attempt() -> bool:
        try:
            await limiter.reserve("t1", 2_000)
            return True
        except RateLimited:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(40)))
    assert results.count(True) == 25
    assert await limiter.usage("t1") == 50_000


@pytest.mark.parametrize(("seconds", "header"), [(0, 1), (0.2, 1), (1.0, 1), (1.01, 2), (44.5, 45)])
def test_retry_after_header(seconds, header):
    assert retry_after_header(seconds) == header
