"""
Task 2: MCP Security Gateway Proxy (Tool Filtering & Auth)
Acts as a reverse proxy sitting between an AI client and a downstream MCP server.
Enforces Bearer authentication, extracts user roles, and intercepts unauthorized 'admin_*' tool calls.
Built with Starlette and httpx for high-performance async processing.
"""

from typing import Optional, Dict, Any, Union
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import logging
from task2_mcp_gateway.auth import extract_role_from_header, AuthError

logger = logging.getLogger("mcp.security_gateway")

# MCP Gateway JSON-RPC Error Codes
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_UNAUTHORIZED_TOOL = -32001


class MCPGatewayProxy:
    def __init__(
        self,
        downstream_url: Optional[str] = None,
        downstream_client: Optional[httpx.AsyncClient] = None
    ):
        self.downstream_url = downstream_url.rstrip("/") if downstream_url else "http://downstream-mcp"
        self._downstream_client = downstream_client
        self.interception_count = 0
        self.app = Starlette(
            routes=[
                Route("/mcp", self.handle_proxy_request, methods=["POST"]),
                Route("/health", self.handle_health, methods=["GET"]),
            ]
        )

    async def get_client(self) -> httpx.AsyncClient:
        if self._downstream_client is not None:
            return self._downstream_client
        self._downstream_client = httpx.AsyncClient(
            base_url=self.downstream_url,
            timeout=10.0
        )
        return self._downstream_client

    async def close(self):
        if self._downstream_client and not self._downstream_client.is_closed:
            await self._downstream_client.aclose()

    async def handle_health(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "UP",
            "downstream_url": self.downstream_url,
            "intercepted_calls": self.interception_count
        })

    async def handle_proxy_request(self, request: Request) -> JSONResponse:
        """
        Main gateway handler:
        1. Extract Bearer token and user role.
        2. Parse JSON-RPC wire format.
        3. Intercept unauthorized admin_* tool calls.
        4. Forward authorized requests to downstream server.
        """
        # Step 1: Authentication & Role Extraction
        auth_header = request.headers.get("Authorization")
        try:
            user_context = extract_role_from_header(auth_header)
        except AuthError as auth_err:
            logger.warning("Authentication failure: %s", auth_err.message)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": JSONRPC_UNAUTHORIZED_TOOL,
                        "message": f"Authentication failed: {auth_err.message}"
                    }
                },
                status_code=auth_err.status_code
            )

        user_role = user_context.get("role", "viewer")
        user_sub = user_context.get("sub", "anonymous")
        logger.debug("Request authenticated: sub=%s, role=%s", user_sub, user_role)

        # Step 2: Parse JSON-RPC Wire Format
        try:
            payload = await request.json()
        except Exception as json_err:
            logger.error("JSON parse failure: %s", json_err)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": JSONRPC_PARSE_ERROR,
                        "message": f"Parse error: Invalid JSON received ({json_err})"
                    }
                },
                status_code=400
            )

        if not isinstance(payload, dict):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": JSONRPC_INVALID_REQUEST,
                        "message": "Invalid Request: Payload must be a JSON object"
                    }
                },
                status_code=400
            )

        req_id = payload.get("id")
        method = payload.get("method")

        if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": JSONRPC_INVALID_REQUEST,
                        "message": "Invalid Request: 'jsonrpc' must be '2.0' and 'method' string required"
                    }
                },
                status_code=400
            )

        # Step 3: Tool Authorization & Filtering Logic
        if method == "tools/list":
            # Transparently forward tools/list to downstream
            logger.info("Forwarding tools/list request transparently for role=%s", user_role)
            return await self._forward_downstream(payload)

        elif method == "tools/call":
            params = payload.get("params") or {}
            tool_name = params.get("name") if isinstance(params, dict) else None

            if not tool_name:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": JSONRPC_INVALID_REQUEST,
                            "message": "Invalid params: 'name' is required for tools/call"
                        }
                    },
                    status_code=400
                )

            # Inspect tool prefix
            if tool_name.startswith("admin_"):
                if user_role != "admin":
                    self.interception_count += 1
                    logger.warning(
                        "SECURITY INTERCEPT: Denied unauthorized call to '%s' from user '%s' (role: '%s')",
                        tool_name, user_sub, user_role
                    )
                    # Directly return JSON-RPC Error -32001 without calling downstream server
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": JSONRPC_UNAUTHORIZED_TOOL,
                                "message": f"Unauthorized Tool Call: Tool '{tool_name}' requires 'admin' role privileges. Current role: '{user_role}'."
                            }
                        },
                        status_code=403
                    )
                else:
                    logger.info("Admin tool '%s' authorized for user '%s'", tool_name, user_sub)

            # Authorized tool call - forward to downstream
            return await self._forward_downstream(payload)

        else:
            # Forward all other standard MCP methods (e.g., initialize, ping, notifications)
            logger.info("Forwarding method '%s' to downstream server", method)
            return await self._forward_downstream(payload)

    async def _forward_downstream(self, payload: Dict[str, Any]) -> JSONResponse:
        """Forward JSON-RPC request to downstream MCP server and return response."""
        client = await self.get_client()
        target_path = "/mcp"

        try:
            resp = await client.post(target_path, json=payload)
            body = resp.json()
            return JSONResponse(body, status_code=resp.status_code)
        except httpx.ConnectError as conn_err:
            logger.error("Downstream connection failed: %s", conn_err)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {
                        "code": JSONRPC_INTERNAL_ERROR,
                        "message": "MCP Gateway: Failed to connect to downstream MCP server"
                    }
                },
                status_code=502
            )
        except Exception as ex:
            logger.error("Downstream forwarding error: %s", ex)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {
                        "code": JSONRPC_INTERNAL_ERROR,
                        "message": f"MCP Gateway internal forwarding error: {str(ex)}"
                    }
                },
                status_code=500
            )


def create_gateway_app(
    downstream_url: Optional[str] = None,
    downstream_client: Optional[httpx.AsyncClient] = None
) -> Starlette:
    gateway = MCPGatewayProxy(downstream_url=downstream_url, downstream_client=downstream_client)
    return gateway.app
