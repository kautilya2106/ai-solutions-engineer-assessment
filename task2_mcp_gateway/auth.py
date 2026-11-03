"""
Task 2: Authentication and Role Extraction Module
Supports both structured Bearer tokens and signed JWT tokens with claims.
"""

from typing import Dict, Any, Optional
import jwt

JWT_SECRET = "mcp-gateway-secure-demo-secret-key-32chars"
JWT_ALGORITHM = "HS256"

# Static token mapping for rapid testing / API key auth
STATIC_ROLE_TOKENS = {
    "admin-token-xyz": {"sub": "admin_user_01", "role": "admin", "name": "System Administrator"},
    "admin-key-999": {"sub": "ops_lead", "role": "admin", "name": "DevOps Engineer"},
    "viewer-token-abc": {"sub": "viewer_user_02", "role": "viewer", "name": "Support Analyst"},
    "viewer-key-111": {"sub": "guest_auditor", "role": "viewer", "name": "Compliance Auditor"},
}


class AuthError(Exception):
    """Raised when authentication fails."""
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def create_test_jwt(role: str, sub: str = "test-user") -> str:
    """Generate a valid signed JWT for testing."""
    payload = {
        "sub": sub,
        "role": role,
        "iss": "mcp-auth-gateway"
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def extract_role_from_header(auth_header: Optional[str]) -> Dict[str, Any]:
    """
    Extracts and validates credentials from Authorization header: 'Bearer <token>'.
    Returns user payload dictionary containing at least 'role'.
    Raises AuthError on missing or invalid tokens.
    """
    if not auth_header:
        raise AuthError("Missing 'Authorization' header in request.", status_code=401)

    parts = auth_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Invalid Authorization format. Expected 'Bearer <token>'.", status_code=401)

    raw_token = parts[1]

    # 1. Check known static tokens first
    if raw_token in STATIC_ROLE_TOKENS:
        return STATIC_ROLE_TOKENS[raw_token]

    # 2. Attempt decoding as signed JWT
    try:
        payload = jwt.decode(raw_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if "role" not in payload:
            raise AuthError("JWT token is missing required 'role' claim.", status_code=403)
        return payload
    except jwt.InvalidTokenError as err:
        # If not a valid JWT and not in static map, reject
        raise AuthError(f"Invalid Bearer token: {err}", status_code=401)
