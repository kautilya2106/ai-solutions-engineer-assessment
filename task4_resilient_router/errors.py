"""
Task 4: Standardized Gateway Error Payloads
Sanitizes all exceptions to prevent raw upstream stack traces, internal endpoints,
or database schema leaks from escaping to the client.
"""

from typing import Dict, Any, Optional
from datetime import datetime, timezone
from starlette.responses import JSONResponse
import uuid


class GatewayException(Exception):
    """Base exception for LLM Gateway failures."""
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        extra: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


class RateLimitExceeded(GatewayException):
    def __init__(self, current_usage: int, limit: int, requested: int):
        super().__init__(
            code="gateway_rate_limited",
            message=f"Tenant rate limit exceeded. Consumed {current_usage} of {limit} tokens in active 60s window (attempted +{requested}).",
            status_code=429,
            extra={
                "current_usage_tokens": current_usage,
                "limit_tokens": limit,
                "requested_tokens": requested,
                "retry_after_seconds": 10
            }
        )


class UpstreamFallbackFailed(GatewayException):
    def __init__(self, primary_reason: str, secondary_reason: str):
        super().__init__(
            code="upstream_unavailable",
            message="Both primary model provider and secondary backup provider are currently unavailable.",
            status_code=502,
            extra={
                "primary_status": primary_reason,
                "secondary_status": secondary_reason
            }
        )


def build_gateway_error_response(
    code: str,
    message: str,
    status_code: int,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None
) -> JSONResponse:
    """
    Constructs a clean, standardized, sanitized error payload adhering strictly to
    safe API gateway production standards (zero internal stack traces).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    req_id = request_id or f"req-{uuid.uuid4().hex[:12]}"

    error_body: Dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": req_id,
        "tenant_id": tenant_id or "anonymous",
        "timestamp": now_iso
    }

    if extra:
        error_body.update(extra)

    headers = {}
    if status_code == 429:
        headers["Retry-After"] = str(extra.get("retry_after_seconds", 10) if extra else 10)

    return JSONResponse(
        {"error": error_body},
        status_code=status_code,
        headers=headers
    )
