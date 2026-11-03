"""
Automated Integration Tests for Task 2: MCP Security Gateway Proxy
Verifies:
1. Bearer token extraction and role decoding (admin vs viewer).
2. Transparent forwarding of tools/list to downstream MCP server.
3. Transparent forwarding of standard tool calls (e.g., get_customer_record) for viewer & admin.
4. Interception of admin_* tool calls for non-admin roles:
   - Returns JSON-RPC error code -32001 (Unauthorized Tool Call).
   - Verifies downstream server is NOT called.
5. Successful forwarding of admin_* tool calls for admin role.
6. Error handling: invalid JSON, missing auth, invalid headers.
"""

import pytest
import httpx
from task2_mcp_gateway.gateway import MCPGatewayProxy
from task2_mcp_gateway.downstream_mock import DownstreamMockServer
from task2_mcp_gateway.auth import create_test_jwt


@pytest.fixture
def test_setup():
    """Sets up downstream mock server and gateway proxy connected via ASGITransport."""
    # 1. Instantiate downstream server
    downstream = DownstreamMockServer()

    # 2. Client that gateway uses to talk to downstream
    downstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=downstream.app),
        base_url="http://downstream-mcp"
    )

    # 3. Instantiate gateway proxy wired to downstream client
    gateway = MCPGatewayProxy(downstream_client=downstream_client)

    # 4. Client that tests use to talk to gateway
    gateway_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app),
        base_url="http://mcp-gateway"
    )

    return {
        "client": gateway_client,
        "gateway": gateway,
        "downstream": downstream,
        "downstream_client": downstream_client
    }


@pytest.mark.asyncio
async def test_tools_list_transparent_forwarding(test_setup):
    """Verify tools/list is transparently forwarded downstream for both admin and viewer."""
    client = test_setup["client"]
    downstream = test_setup["downstream"]

    # Test with viewer token
    viewer_jwt = create_test_jwt(role="viewer")
    payload = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/list",
        "params": {}
    }

    resp = await client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": f"Bearer {viewer_jwt}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 101
    assert "result" in body
    tools = body["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "get_customer_record" in tool_names
    assert "admin_reset_key" in tool_names

    # Verify downstream received the method:tools/list call
    assert downstream.call_counts.get("method:tools/list") == 1


@pytest.mark.asyncio
async def test_standard_tool_call_allowed_for_viewer(test_setup):
    """Verify viewer role can execute standard tools (get_customer_record)."""
    client = test_setup["client"]
    downstream = test_setup["downstream"]

    viewer_jwt = create_test_jwt(role="viewer")
    payload = {
        "jsonrpc": "2.0",
        "id": 102,
        "method": "tools/call",
        "params": {
            "name": "get_customer_record",
            "arguments": {"customer_id": "CUST-10001"}
        }
    }

    resp = await client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": f"Bearer {viewer_jwt}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 102
    assert "result" in body
    assert not body.get("isError")

    assert downstream.call_counts.get("tool:get_customer_record") == 1


@pytest.mark.asyncio
async def test_admin_tool_intercepted_for_viewer(test_setup):
    """
    CRITICAL SECURITY TEST:
    Verify that an admin_* tool call by a viewer is intercepted:
    - Gateway returns JSON-RPC error -32001 (Unauthorized Tool Call).
    - Downstream server is NEVER contacted for the admin tool.
    """
    client = test_setup["client"]
    downstream = test_setup["downstream"]
    gateway = test_setup["gateway"]

    # Reset downstream counts
    downstream.call_counts.clear()

    # Viewer token attempts admin_reset_key
    viewer_jwt = create_test_jwt(role="viewer")
    payload = {
        "jsonrpc": "2.0",
        "id": 103,
        "method": "tools/call",
        "params": {
            "name": "admin_reset_key",
            "arguments": {"key_alias": "kms/primary-vault"}
        }
    }

    resp = await client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": f"Bearer {viewer_jwt}"}
    )
    assert resp.status_code == 403
    body = resp.json()

    # Verify JSON-RPC standard error envelope
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 103
    assert "error" in body
    assert body["error"]["code"] == -32001
    assert "Unauthorized Tool Call" in body["error"]["message"]
    assert "admin_reset_key" in body["error"]["message"]

    # Verify downstream server was NEVER called for this tool!
    assert "tool:admin_reset_key" not in downstream.call_counts
    assert downstream.call_counts.get("tool:admin_reset_key", 0) == 0
    assert gateway.interception_count == 1


@pytest.mark.asyncio
async def test_admin_tool_allowed_for_admin(test_setup):
    """Verify that an admin_* tool call by an admin is authorized and executed downstream."""
    client = test_setup["client"]
    downstream = test_setup["downstream"]

    # Admin token attempts admin_reset_key
    admin_jwt = create_test_jwt(role="admin")
    payload = {
        "jsonrpc": "2.0",
        "id": 104,
        "method": "tools/call",
        "params": {
            "name": "admin_reset_key",
            "arguments": {"key_alias": "kms/primary-vault"}
        }
    }

    resp = await client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": f"Bearer {admin_jwt}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 104
    assert "result" in body
    content = body["result"]["content"][0]["text"]
    assert "KEY_ROTATED" in content

    # Verify downstream executed the admin tool
    assert downstream.call_counts.get("tool:admin_reset_key") == 1


@pytest.mark.asyncio
async def test_static_bearer_tokens(test_setup):
    """Verify static token format (e.g. Bearer admin-token-xyz, viewer-token-abc)."""
    client = test_setup["client"]

    # Viewer static token attempts admin_purge_cache -> rejected
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 105, "method": "tools/call", "params": {"name": "admin_purge_cache"}},
        headers={"Authorization": "Bearer viewer-token-abc"}
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == -32001

    # Admin static token attempts admin_purge_cache -> allowed
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 106, "method": "tools/call", "params": {"name": "admin_purge_cache"}},
        headers={"Authorization": "Bearer admin-token-xyz"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body


@pytest.mark.asyncio
async def test_missing_or_invalid_auth_header(test_setup):
    """Verify missing or malformed auth headers are rejected with 401."""
    client = test_setup["client"]
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    # No header
    resp = await client.post("/mcp", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert "Authentication failed" in body["error"]["message"]

    # Wrong scheme (Basic instead of Bearer)
    resp = await client.post("/mcp", json=payload, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401

    # Invalid token string
    resp = await client.post("/mcp", json=payload, headers={"Authorization": "Bearer bogus_token_data"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_malformed_json_rpc_envelope(test_setup):
    """Verify JSON-RPC syntax and schema errors are caught with proper codes."""
    client = test_setup["client"]
    token = create_test_jwt(role="admin")
    headers = {"Authorization": f"Bearer {token}"}

    # Missing method
    resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1}, headers=headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == -32600

    # Wrong jsonrpc version
    resp = await client.post("/mcp", json={"jsonrpc": "1.0", "method": "tools/list", "id": 1}, headers=headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == -32600
