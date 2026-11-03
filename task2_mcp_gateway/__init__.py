"""Task 2 MCP Security Gateway Proxy Package"""
from task2_mcp_gateway.gateway import MCPGatewayProxy, create_gateway_app
from task2_mcp_gateway.auth import extract_role_from_header, create_test_jwt, AuthError
from task2_mcp_gateway.downstream_mock import DownstreamMockServer

__all__ = [
    "MCPGatewayProxy",
    "create_gateway_app",
    "extract_role_from_header",
    "create_test_jwt",
    "AuthError",
    "DownstreamMockServer"
]
