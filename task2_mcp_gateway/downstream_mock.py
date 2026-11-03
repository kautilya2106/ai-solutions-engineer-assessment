"""
Task 2: Downstream Mock MCP Server
Provides an HTTP JSON-RPC endpoint serving MCP protocol methods:
- tools/list: returns list of tools (both standard and admin_* tools)
- tools/call: executes tools
Tracks invocation metrics to verify whether the gateway proxy actually forwarded or intercepted calls.
"""

from typing import Dict, Any
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import json
import logging

logger = logging.getLogger("mcp.downstream_mock")

AVAILABLE_TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Standard tool: retrieve customer details.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"]
        }
    },
    {
        "name": "query_status",
        "description": "Standard tool: query cluster health status.",
        "inputSchema": {
            "type": "object",
            "properties": {"service": {"type": "string"}}
        }
    },
    {
        "name": "admin_reset_key",
        "description": "Admin tool: rotate and reset encryption keys.",
        "inputSchema": {
            "type": "object",
            "properties": {"key_alias": {"type": "string"}},
            "required": ["key_alias"]
        }
    },
    {
        "name": "admin_purge_cache",
        "description": "Admin tool: purge global cache layers.",
        "inputSchema": {
            "type": "object",
            "properties": {"tier": {"type": "string"}}
        }
    }
]


class DownstreamMockServer:
    def __init__(self):
        self.call_counts: Dict[str, int] = {}
        self.total_requests = 0
        self.app = Starlette(
            routes=[
                Route("/mcp", self.handle_jsonrpc, methods=["POST"]),
                Route("/metrics", self.handle_metrics, methods=["GET"]),
                Route("/reset_metrics", self.handle_reset_metrics, methods=["POST"]),
            ]
        )

    def record_call(self, method_or_tool: str):
        self.total_requests += 1
        self.call_counts[method_or_tool] = self.call_counts.get(method_or_tool, 0) + 1

    async def handle_jsonrpc(self, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"}
            }, status_code=400)

        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params", {})

        self.record_call(f"method:{method}")

        if method == "tools/list":
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": AVAILABLE_TOOLS}
            })

        elif method == "tools/call":
            tool_name = params.get("name", "unknown") if isinstance(params, dict) else "unknown"
            self.record_call(f"tool:{tool_name}")
            args = params.get("arguments", {}) if isinstance(params, dict) else {}

            if tool_name == "get_customer_record":
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps({"status": "FOUND", "customer_id": args.get("customer_id")})
                        }]
                    }
                })

            elif tool_name == "query_status":
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps({"status": "HEALTHY", "service": args.get("service", "all")})
                        }]
                    }
                })

            elif tool_name == "admin_reset_key":
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps({
                                "status": "KEY_ROTATED",
                                "key_alias": args.get("key_alias"),
                                "action": "Rotated secret encryption key successfully"
                            })
                        }]
                    }
                })

            elif tool_name == "admin_purge_cache":
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps({
                                "status": "PURGED",
                                "tier": args.get("tier", "all")
                            })
                        }]
                    }
                })

            else:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method/Tool '{tool_name}' not found"}
                }, status_code=200)

        elif method == "initialize":
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-downstream-mcp", "version": "1.0"}
                }
            })

        else:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method '{method}' not found"}
            }, status_code=200)

    async def handle_metrics(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "total_requests": self.total_requests,
            "call_counts": self.call_counts
        })

    async def handle_reset_metrics(self, request: Request) -> JSONResponse:
        self.call_counts.clear()
        self.total_requests = 0
        return JSONResponse({"status": "reset"})
