"""
Task 4: Token-Aware Sliding Window Rate Limiter
Backed by on-disk SQLite with automated state eviction and concurrency safety.
Default: 50,000 tokens/minute per tenant API key.
"""

import time
import asyncio
import threading
from typing import Tuple, Dict, Any, Optional
import logging
from task4_resilient_router.db import init_sqlite_db

logger = logging.getLogger("llm.gateway.ratelimiter")

DEFAULT_TOKEN_LIMIT_PER_MIN = 50000
DEFAULT_WINDOW_SECONDS = 60.0


class TokenSlidingWindowRateLimiter:
    """
    Token-aware sliding window rate limiter.
    Accurately sums token consumption within the active sliding time window (T - window_seconds to T).
    Persists usage into an on-disk SQLite database with WAL mode and evicts expired records.
    """

    def __init__(
        self,
        db_path: str,
        limit_tokens_per_window: int = DEFAULT_TOKEN_LIMIT_PER_MIN,
        window_seconds: float = DEFAULT_WINDOW_SECONDS
    ):
        self.db_path = db_path
        self.limit = limit_tokens_per_window
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._conn = init_sqlite_db(db_path)

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def check_and_consume(
        self,
        tenant_id: str,
        requested_tokens: int,
        now: Optional[float] = None
    ) -> Tuple[bool, int, int]:
        """
        Atomically:
        1. Evicts expired usage events for all tenants (timestamp < now - window_seconds).
        2. Calculates current active token usage for tenant_id.
        3. If usage + requested_tokens <= limit, records consumption and returns (True, new_usage, limit).
        4. If usage + requested_tokens > limit, rejects without recording and returns (False, current_usage, limit).
        """
        if now is None:
            now = time.time()

        window_start = now - self.window_seconds

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE;")

                # Step 1: Evict expired records older than the sliding window
                cursor.execute(
                    "DELETE FROM token_usage_events WHERE timestamp < ?;",
                    (window_start,)
                )

                # Step 2: Sum current tokens consumed within active sliding window
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(token_count), 0)
                    FROM token_usage_events
                    WHERE tenant_id = ? AND timestamp >= ?;
                    """,
                    (tenant_id, window_start)
                )
                current_usage = cursor.fetchone()[0]

                # Step 3: Evaluate limit
                if current_usage + requested_tokens > self.limit:
                    cursor.execute("COMMIT;")
                    logger.warning(
                        "Rate limit exceeded for tenant '%s': Current=%d, Requested=%d, Limit=%d (Window=%.1fs)",
                        tenant_id, current_usage, requested_tokens, self.limit, self.window_seconds
                    )
                    return False, current_usage, self.limit

                # Step 4: Record consumption
                cursor.execute(
                    """
                    INSERT INTO token_usage_events (tenant_id, timestamp, token_count)
                    VALUES (?, ?, ?);
                    """,
                    (tenant_id, now, requested_tokens)
                )
                cursor.execute("COMMIT;")

                new_usage = current_usage + requested_tokens
                logger.info(
                    "Consumed %d tokens for tenant '%s' (Total window usage: %d/%d)",
                    requested_tokens, tenant_id, new_usage, self.limit
                )
                return True, new_usage, self.limit

            except Exception as e:
                cursor.execute("ROLLBACK;")
                logger.error("Rate limiter database transaction error: %s", e)
                raise

    async def check_and_consume_async(
        self,
        tenant_id: str,
        requested_tokens: int,
        now: Optional[float] = None
    ) -> Tuple[bool, int, int]:
        """Async wrapper using asyncio.to_thread for non-blocking event-loop integration."""
        return await asyncio.to_thread(self.check_and_consume, tenant_id, requested_tokens, now)

    def get_current_usage(self, tenant_id: str, now: Optional[float] = None) -> int:
        """Returns total tokens consumed by tenant in active sliding window."""
        if now is None:
            now = time.time()
        window_start = now - self.window_seconds

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(token_count), 0)
                FROM token_usage_events
                WHERE tenant_id = ? AND timestamp >= ?;
                """,
                (tenant_id, window_start)
            )
            return cursor.fetchone()[0]

    def reset_tenant(self, tenant_id: str):
        """Clears all records for a specific tenant (useful in tests)."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("DELETE FROM token_usage_events WHERE tenant_id = ?;", (tenant_id,))
