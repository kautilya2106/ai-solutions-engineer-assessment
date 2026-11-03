"""
Task 4: Resilient Model Fallback Router with Token Rate Limiting
Handles token sliding-window rate limiting backed by on-disk SQLite,
enforces 3000ms timeout on primary model endpoint, and automatically fails over
to secondary backup model on primary 429 or timeout.
Returns standardized sanitized error payloads on failure without leaking internal details.
"""

import asyncio
import time
import uuid
import logging
from typing import Optional, Dict, Any
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from task4_resilient_router.rate_limiter import TokenSlidingWindowRateLimiter
from task4_resilient_router.errors import (
    build_gateway_error_response,
    RateLimitExceeded,
    UpstreamFallbackFailed
)

logger = logging.getLogger("llm.gateway.router")

PRIMARY_TIMEOUT_SECONDS = 3.0  # Strict 3000ms timeout requirement
SECONDARY_TIMEOUT_SECONDS = 5.0
DEFAULT_ESTIMATED_TOKENS = 500


class LLMGatewayRouter:
    """
    Resilient Model-Routing module for an LLM Gateway.
    - Tenant token sliding window rate limiting.
    - Automatic failover from primary to secondary on 429 or >3000ms timeout.
    - Standardized, sanitized gateway error envelopes.
    """

    def __init__(
        self,
        rate_limiter: TokenSlidingWindowRateLimiter,
        primary_client: httpx.AsyncClient,
        secondary_client: httpx.AsyncClient,
        primary_timeout: float = PRIMARY_TIMEOUT_SECONDS,
        secondary_timeout: float = SECONDARY_TIMEOUT_SECONDS
    ):
        self.rate_limiter = rate_limiter
        self.primary_client = primary_client
        self.secondary_client = secondary_client
        self.primary_timeout = primary_timeout
        self.secondary_timeout = secondary_timeout

        self.stats = {
            "primary_success": 0,
            "failover_triggered_429": 0,
            "failover_triggered_timeout": 0,
            "secondary_success": 0,
            "both_failed": 0,
            "rate_limited": 0
        }

        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self.handle_chat_completion, methods=["POST"]),
                Route("/health", self.handle_health, methods=["GET"])
            ]
        )

    def _extract_tenant_id(self, request: Request, payload: Dict[str, Any]) -> str:
        """Extract tenant identity from API key header, authorization header, or payload."""
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return api_key.strip()

        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            return auth.split()[1]

        if "tenant_id" in payload:
            return str(payload["tenant_id"])

        return "default-tenant"

    def _estimate_tokens(self, payload: Dict[str, Any]) -> int:
        """Estimate tokens for incoming request based on messages or specified field."""
        if "estimated_tokens" in payload and isinstance(payload["estimated_tokens"], int):
            return payload["estimated_tokens"]

        # Approximate token count: ~4 chars per token + max_tokens
        prompt_chars = 0
        for msg in payload.get("messages", []):
            prompt_chars += len(str(msg.get("content", "")))

        max_tokens = payload.get("max_tokens", 256)
        estimated = (prompt_chars // 4) + max_tokens
        return max(estimated, 50)

    async def handle_health(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "UP", "stats": self.stats})

    async def handle_chat_completion(self, request: Request) -> JSONResponse:
        req_id = f"req-{uuid.uuid4().hex[:12]}"
        try:
            payload = await request.json()
        except Exception:
            return build_gateway_error_response(
                code="invalid_request",
                message="Malformed JSON body in request",
                status_code=400,
                request_id=req_id
            )

        tenant_id = self._extract_tenant_id(request, payload)
        requested_tokens = self._estimate_tokens(payload)

        # -------------------------------------------------------------
        # Step 1: Token-Aware Sliding Window Rate Limiting Check
        # -------------------------------------------------------------
        allowed, current_usage, limit = await self.rate_limiter.check_and_consume_async(
            tenant_id=tenant_id,
            requested_tokens=requested_tokens
        )

        if not allowed:
            self.stats["rate_limited"] += 1
            return build_gateway_error_response(
                code="gateway_rate_limited",
                message=f"Rate limit exceeded: Tenant '{tenant_id}' has consumed {current_usage} of {limit} tokens in active 60s window (requested {requested_tokens} tokens).",
                status_code=429,
                request_id=req_id,
                tenant_id=tenant_id,
                extra={
                    "current_usage_tokens": current_usage,
                    "limit_tokens": limit,
                    "requested_tokens": requested_tokens,
                    "retry_after_seconds": 15
                }
            )

        # -------------------------------------------------------------
        # Step 2: Attempt Primary Model Endpoint with 3000ms Timeout
        # -------------------------------------------------------------
        primary_response: Optional[httpx.Response] = None
        failover_reason: Optional[str] = None

        try:
            logger.info("Routing request %s to PRIMARY model endpoint (timeout=%.1fs)", req_id, self.primary_timeout)
            primary_coro = self.primary_client.post(
                "/v1/chat/completions",
                json=payload,
                timeout=self.primary_timeout
            )
            primary_response = await asyncio.wait_for(primary_coro, timeout=self.primary_timeout)

            # Check for 429 status from primary
            if primary_response.status_code == 429:
                failover_reason = "primary_status_429_too_many_requests"
                self.stats["failover_triggered_429"] += 1
                logger.warning("Primary provider returned 429. Triggering fallback to secondary provider for %s", req_id)
            elif primary_response.status_code == 200:
                self.stats["primary_success"] += 1
                body = primary_response.json()
                return JSONResponse(body, status_code=200, headers={"X-Gateway-Route": "primary"})
            else:
                failover_reason = f"primary_status_{primary_response.status_code}"

        except (httpx.TimeoutException, asyncio.TimeoutError):
            failover_reason = f"primary_timeout_exceeded_{int(self.primary_timeout * 1000)}ms"
            self.stats["failover_triggered_timeout"] += 1
            logger.warning(
                "Primary model timed out after %.1fs. Triggering fallback to secondary provider for %s",
                self.primary_timeout, req_id
            )
        except httpx.ConnectError:
            failover_reason = "primary_connection_error"
            logger.warning("Primary model connection failed. Triggering fallback for %s", req_id)
        except Exception as e:
            failover_reason = f"primary_exception_{type(e).__name__}"
            logger.warning("Primary model encounter unexpected error: %s. Triggering fallback for %s", e, req_id)

        # -------------------------------------------------------------
        # Step 3: Automatic Failover to Secondary Backup Model Provider
        # -------------------------------------------------------------
        logger.info("Executing failover to SECONDARY backup model provider for %s (reason: %s)", req_id, failover_reason)

        try:
            secondary_response = await self.secondary_client.post(
                "/v1/chat/completions",
                json=payload,
                timeout=self.secondary_timeout
            )

            if secondary_response.status_code == 200:
                self.stats["secondary_success"] += 1
                body = secondary_response.json()
                headers = {
                    "X-Gateway-Route": "fallback-secondary",
                    "X-Gateway-Failover-Reason": failover_reason
                }
                return JSONResponse(body, status_code=200, headers=headers)
            else:
                self.stats["both_failed"] += 1
                logger.error("Secondary backup provider returned error status %d for %s", secondary_response.status_code, req_id)
                return build_gateway_error_response(
                    code="upstream_unavailable",
                    message="Both primary model provider and backup model provider failed to service request.",
                    status_code=502,
                    request_id=req_id,
                    tenant_id=tenant_id
                )

        except (httpx.TimeoutException, asyncio.TimeoutError):
            self.stats["both_failed"] += 1
            logger.error("Secondary backup provider timed out after %.1fs for %s", self.secondary_timeout, req_id)
            return build_gateway_error_response(
                code="gateway_timeout",
                message="Upstream model providers timed out servicing completion request.",
                status_code=504,
                request_id=req_id,
                tenant_id=tenant_id
            )
        except Exception as sec_ex:
            self.stats["both_failed"] += 1
            logger.error("Secondary backup provider connection error: %s", sec_ex)
            return build_gateway_error_response(
                code="upstream_unavailable",
                message="Upstream model providers unavailable.",
                status_code=502,
                request_id=req_id,
                tenant_id=tenant_id
            )
