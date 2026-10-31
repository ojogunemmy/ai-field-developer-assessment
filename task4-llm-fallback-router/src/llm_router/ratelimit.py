"""Token-aware sliding-window rate limiter, stored in SQLite on disk.

Each tenant may consume at most ``limit`` tokens in any rolling ``window``
(by default 50,000 tokens per 60 seconds). Usage is a log of timestamped
entries; the tenant's current usage is the sum of entries newer than
``now - window``. Unlike fixed per-minute buckets, this has no boundary burst:
a tenant can never spend 2x the limit across the turn of a minute.

**Atomicity.** Check-and-reserve runs inside one ``BEGIN IMMEDIATE``
transaction, which takes SQLite's write lock before reading. Two concurrent
requests, even from different worker processes sharing the database file, are
serialized, so together they can never exceed the limit.

**Lifecycle of a reservation.**

1. ``reserve(tenant, tokens)`` inserts an entry for the worst-case estimate, or
   raises ``RateLimited`` with a ``retry_after`` computed from when the oldest
   entries leave the window.
2. ``settle(entry_id, tokens)`` replaces the estimate with the real usage once
   the provider has answered.
3. ``release(entry_id)`` deletes the entry if no provider served the request,
   so a failed call costs the tenant nothing.

**Eviction.** Expired entries are deleted inside every ``reserve`` transaction,
using an index on ``ts``, so the table only ever holds about one window of data.

SQLite calls are blocking, so the async methods run them in a worker thread.
"""

import asyncio
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIMIT = 50_000
DEFAULT_WINDOW_SECONDS = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant  TEXT    NOT NULL,
    ts      REAL    NOT NULL,
    tokens  INTEGER NOT NULL CHECK (tokens >= 0)
);
CREATE INDEX IF NOT EXISTS idx_token_usage_tenant_ts ON token_usage (tenant, ts);
CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage (ts);
"""


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited; retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class RequestTooLarge(Exception):
    """The request alone needs more than the whole window's budget."""


@dataclass(frozen=True)
class Reservation:
    entry_id: int
    tenant: str
    tokens: int


class SlidingWindowLimiter:
    def __init__(
        self,
        db_path: str | Path,
        limit: int = DEFAULT_LIMIT,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = str(db_path)
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # One short-lived connection per operation: safe across threads and
        # processes. isolation_level=None lets us issue BEGIN IMMEDIATE ourselves.
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)

    # --- synchronous core (runs in a worker thread) ------------------------

    def reserve_sync(self, tenant: str, tokens: int) -> Reservation:
        if tokens > self.limit:
            raise RequestTooLarge
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            window_start = now - self.window
            conn.execute("DELETE FROM token_usage WHERE ts <= ?", (window_start,))
            (used,) = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts > ?",
                (tenant, window_start),
            ).fetchone()
            if used + tokens > self.limit:
                retry_after = self._retry_after(conn, tenant, used + tokens - self.limit, now)
                conn.execute("ROLLBACK")
                raise RateLimited(retry_after)
            cursor = conn.execute(
                "INSERT INTO token_usage (tenant, ts, tokens) VALUES (?, ?, ?)",
                (tenant, now, tokens),
            )
            conn.execute("COMMIT")
            assert cursor.lastrowid is not None
            return Reservation(cursor.lastrowid, tenant, tokens)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _retry_after(self, conn: sqlite3.Connection, tenant: str, excess: int, now: float) -> float:
        """Seconds until enough of the oldest entries expire to free ``excess`` tokens."""
        freed = 0
        rows = conn.execute(
            "SELECT ts, tokens FROM token_usage WHERE tenant = ? AND ts > ? ORDER BY ts",
            (tenant, now - self.window),
        )
        for ts, entry_tokens in rows:
            freed += entry_tokens
            if freed >= excess:
                return max(0.0, float(ts) + self.window - now)
        return self.window  # unreachable while tokens <= limit; safe upper bound

    def settle_sync(self, reservation: Reservation, actual_tokens: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE token_usage SET tokens = ? WHERE id = ?",
                (max(0, actual_tokens), reservation.entry_id),
            )

    def release_sync(self, reservation: Reservation) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM token_usage WHERE id = ?", (reservation.entry_id,))

    def usage_sync(self, tenant: str) -> int:
        with closing(self._connect()) as conn:
            (used,) = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts > ?",
                (tenant, self.clock() - self.window),
            ).fetchone()
        return int(used)

    def row_count_sync(self) -> int:
        with closing(self._connect()) as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()
        return int(count)

    # --- async API ---------------------------------------------------------

    async def reserve(self, tenant: str, tokens: int) -> Reservation:
        return await asyncio.to_thread(self.reserve_sync, tenant, tokens)

    async def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        await asyncio.to_thread(self.settle_sync, reservation, actual_tokens)

    async def release(self, reservation: Reservation) -> None:
        await asyncio.to_thread(self.release_sync, reservation)

    async def usage(self, tenant: str) -> int:
        return await asyncio.to_thread(self.usage_sync, tenant)


def retry_after_header(seconds: float) -> int:
    """Whole seconds for a Retry-After header, never less than 1."""
    return max(1, math.ceil(seconds))
