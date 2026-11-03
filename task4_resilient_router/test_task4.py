"""
Automated Integration Tests for Task 4: Rate Limiting & Model Fallback Router
Verifies:
1. Token-aware sliding window rate limiter backed by on-disk SQLite.
2. Accurate sliding window state eviction and token tracking across restarts.
3. Automatic failover on primary 429 Too Many Requests to secondary backup provider.
4. Automatic failover on primary 3000ms timeout to secondary backup provider.
5. Standardized sanitized error envelopes without stack trace leakage.
"""

import pytest
import os
import time
import httpx
import tempfile
import sqlite3

from task4_resilient_router.rate_limiter import TokenSlidingWindowRateLimiter
from task4_resilient_router.router import LLMGatewayRouter
from task4_resilient_router.mock_providers import MockPrimaryLLMProvider, MockSecondaryLLMProvider


@pytest.fixture
def temp_sqlite_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_ratelimit_")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)
    # also remove WAL/SHM files if present
    for ext in ("-wal", "-shm"):
        if os.path.exists(path + ext):
            os.remove(path + ext)


# ============================================================================
# 1. SQLite Token Sliding Window Rate Limiter Tests
# ============================================================================

def test_sqlite_token_rate_limiter_basic(temp_sqlite_db):
    """Verify rate limiter allows within limit and rejects when exceeding limit."""
    limiter = TokenSlidingWindowRateLimiter(
        db_path=temp_sqlite_db,
        limit_tokens_per_window=1000,
        window_seconds=60.0
    )

    t0 = time.time()
    # Request 400 tokens -> Allowed (usage: 400)
    allowed, usage, limit = limiter.check_and_consume("tenant-A", 400, now=t0)
    assert allowed is True
    assert usage == 400

    # Request 500 tokens -> Allowed (usage: 900)
    allowed, usage, limit = limiter.check_and_consume("tenant-A", 500, now=t0 + 1)
    assert allowed is True
    assert usage == 900

    # Request 200 tokens -> Exceeds 1000 limit (900 + 200 = 1100 > 1000) -> Rejected
    allowed, usage, limit = limiter.check_and_consume("tenant-A", 200, now=t0 + 2)
    assert allowed is False
    assert usage == 900

    # Tenant-B has its own independent limit
    allowed_b, usage_b, _ = limiter.check_and_consume("tenant-B", 800, now=t0 + 2)
    assert allowed_b is True
    assert usage_b == 800

    limiter.close()


def test_sqlite_sliding_window_eviction(temp_sqlite_db):
    """Verify expired records slide out of the window, freeing up token capacity."""
    limiter = TokenSlidingWindowRateLimiter(
        db_path=temp_sqlite_db,
        limit_tokens_per_window=1000,
        window_seconds=10.0  # 10s window for test
    )

    t0 = 1000.0
    # Consume 900 tokens at t0
    allowed, usage, _ = limiter.check_and_consume("tenant-exp", 900, now=t0)
    assert allowed is True

    # At t0 + 5s, cannot consume 300 tokens
    allowed, usage, _ = limiter.check_and_consume("tenant-exp", 300, now=t0 + 5.0)
    assert allowed is False

    # At t0 + 11s (> 10s window), previous 900 tokens have expired and are evicted
    allowed, usage, _ = limiter.check_and_consume("tenant-exp", 300, now=t0 + 11.0)
    assert allowed is True
    assert usage == 300

    limiter.close()


def test_sqlite_persistence_across_restarts(temp_sqlite_db):
    """Verify token usage records persist on disk across process/instance restarts."""
    t0 = time.time()
    # Instance 1 consumes tokens
    limiter1 = TokenSlidingWindowRateLimiter(db_path=temp_sqlite_db, limit_tokens_per_window=5000)
    limiter1.check_and_consume("persistent-tenant", 3000, now=t0)
    limiter1.close()

    # Instance 2 re-opens the same on-disk DB
    limiter2 = TokenSlidingWindowRateLimiter(db_path=temp_sqlite_db, limit_tokens_per_window=5000)
    usage = limiter2.get_current_usage("persistent-tenant", now=t0 + 1)
    assert usage == 3000

    # Attempting to consume 2500 should be rejected (3000 + 2500 = 5500 > 5000)
    allowed, current, _ = limiter2.check_and_consume("persistent-tenant", 2500, now=t0 + 2)
    assert allowed is False
    assert current == 3000
    limiter2.close()


# ============================================================================
# 2. Router Fallback & Resilience Tests
# ============================================================================

@pytest.fixture
def router_setup(temp_sqlite_db):
    limiter = TokenSlidingWindowRateLimiter(
        db_path=temp_sqlite_db,
        limit_tokens_per_window=50000,
        window_seconds=60.0
    )

    def create_router(
        primary_mode="healthy",
        primary_delay=0.0,
        secondary_mode="healthy",
        secondary_delay=0.0,
        primary_timeout=3.0
    ):
        primary = MockPrimaryLLMProvider(mode=primary_mode, delay_seconds=primary_delay)
        secondary = MockSecondaryLLMProvider(mode=secondary_mode, delay_seconds=secondary_delay)

        primary_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=primary.app),
            base_url="http://primary-llm"
        )
        secondary_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=secondary.app),
            base_url="http://secondary-llm"
        )

        router = LLMGatewayRouter(
            rate_limiter=limiter,
            primary_client=primary_client,
            secondary_client=secondary_client,
            primary_timeout=primary_timeout
        )

        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router.app),
            base_url="http://llm-gateway-router"
        )

        return {
            "router_client": router_client,
            "router": router,
            "primary": primary,
            "secondary": secondary,
            "limiter": limiter
        }

    return create_router


@pytest.mark.asyncio
async def test_primary_healthy_success(router_setup):
    """Verify primary model serves request directly when healthy."""
    env = router_setup(primary_mode="healthy")
    client = env["router_client"]

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
        headers={"X-API-Key": "tenant-prod-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "primary-model-gpt4"
    assert resp.headers.get("X-Gateway-Route") == "primary"
    assert env["primary"].call_count == 1
    assert env["secondary"].call_count == 0


@pytest.mark.asyncio
async def test_fallback_on_primary_429(router_setup):
    """
    CRITICAL REQUIREMENT:
    Verify that when primary model returns 429 Too Many Requests,
    router automatically fails over to the secondary backup model provider.
    """
    env = router_setup(primary_mode="429", secondary_mode="healthy")
    client = env["router_client"]

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Execute failover test"}]},
        headers={"X-API-Key": "tenant-prod-2"}
    )

    assert resp.status_code == 200
    body = resp.json()
    # Served by secondary backup provider
    assert body["model"] == "secondary-backup-claude"
    assert resp.headers.get("X-Gateway-Route") == "fallback-secondary"
    assert resp.headers.get("X-Gateway-Failover-Reason") == "primary_status_429_too_many_requests"

    # Both primary and secondary were contacted (1 attempt each)
    assert env["primary"].call_count == 1
    assert env["secondary"].call_count == 1
    assert env["router"].stats["failover_triggered_429"] == 1


@pytest.mark.asyncio
async def test_fallback_on_primary_timeout(router_setup):
    """
    CRITICAL REQUIREMENT:
    Verify that when primary model times out after 3000ms,
    router automatically fails over to the secondary backup provider.
    """
    # Primary has 3.5s delay, with router configured for 0.2s in test (or 3.0s timeout)
    env = router_setup(
        primary_mode="healthy",
        primary_delay=0.3,
        primary_timeout=0.1,  # Scaled down for fast test execution
        secondary_mode="healthy",
        secondary_delay=0.0
    )
    client = env["router_client"]

    t_start = time.time()
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Timeout failover test"}]},
        headers={"X-API-Key": "tenant-prod-3"}
    )
    elapsed = time.time() - t_start

    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "secondary-backup-claude"
    assert resp.headers.get("X-Gateway-Route") == "fallback-secondary"
    assert "timeout" in resp.headers.get("X-Gateway-Failover-Reason")
    assert env["router"].stats["failover_triggered_timeout"] == 1


@pytest.mark.asyncio
async def test_token_sliding_window_rate_limiting_enforcement(router_setup):
    """Verify that requests exceeding 50,000 tokens/min are rejected with sanitized 429."""
    env = router_setup(primary_mode="healthy")
    client = env["router_client"]

    # Consume 45,000 tokens -> Succeeds
    resp1 = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Task 1"}], "estimated_tokens": 45000},
        headers={"X-API-Key": "heavy-tenant"}
    )
    assert resp1.status_code == 200

    # Attempt to consume another 10,000 tokens (45,000 + 10,000 = 55,000 > 50,000 limit) -> 429 Rejected
    resp2 = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Task 2"}], "estimated_tokens": 10000},
        headers={"X-API-Key": "heavy-tenant"}
    )
    assert resp2.status_code == 429
    body2 = resp2.json()

    # Standardized error payload
    assert "error" in body2
    assert body2["error"]["code"] == "gateway_rate_limited"
    assert body2["error"]["tenant_id"] == "heavy-tenant"
    assert "retry_after_seconds" in body2["error"]
    assert resp2.headers.get("Retry-After") is not None


@pytest.mark.asyncio
async def test_standardized_error_sanitization_on_dual_failure(router_setup):
    """
    CRITICAL REQUIREMENT:
    Ensure error responses return a standardized gateway error payload without
    leaking raw upstream stack traces or internal implementation details.
    """
    # Both primary and secondary fail with 500 internal errors
    env = router_setup(primary_mode="429", secondary_mode="error")
    client = env["router_client"]

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Both providers crash"}]},
        headers={"X-API-Key": "tenant-test-sanitization"}
    )

    assert resp.status_code == 502
    body = resp.json()

    # Verify standardized error envelope structure
    assert "error" in body
    err = body["error"]
    assert err["code"] == "upstream_unavailable"
    assert "request_id" in err
    assert "timestamp" in err
    assert err["tenant_id"] == "tenant-test-sanitization"

    # Verify ZERO internal stack traces or database info leaked
    raw_response_str = str(body)
    assert "Traceback" not in raw_response_str
    assert "sqlite" not in raw_response_str.lower()
    assert "File \"" not in raw_response_str
    assert "Exception" not in raw_response_str
