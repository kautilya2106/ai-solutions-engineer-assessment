"""
Automated Integration Tests for Task 1: Custom MCP Server
Verifies:
1. STDIO Isolation (stdout is strictly valid JSON-RPC frames; stderr contains diagnostic logs).
2. Protocol Compliance (initialize, tools/list, tools/call flow).
3. Schema Validation & Standard JSON-RPC error code -32602 rejection for invalid inputs.
"""

import sys
import os
import json
import subprocess
import pytest

PYTHON_BIN = sys.executable
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MCPStdioClient:
    """Helper client to interact with an MCP server subprocess over stdio."""

    def __init__(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = WORKSPACE_DIR
        self.proc = subprocess.Popen(
            [PYTHON_BIN, "-m", "task1_mcp_server.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=WORKSPACE_DIR,
            env=env
        )
        self.req_id = 0

    def send_request(self, method: str, params: dict = None) -> dict:
        self.req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.req_id,
            "method": method,
            "params": params if params is not None else {}
        }
        wire_msg = json.dumps(payload) + "\n"
        self.proc.stdin.write(wire_msg)
        self.proc.stdin.flush()

        raw_line = self.proc.stdout.readline()
        assert raw_line, "MCP server closed stdout unexpectedly"

        # Verify stdout purity: must parse strictly as JSON
        try:
            response = json.loads(raw_line.strip())
        except json.JSONDecodeError as err:
            pytest.fail(f"Stdout pollution detected! Output is not valid JSON-RPC: {raw_line!r} (Error: {err})")

        assert response.get("jsonrpc") == "2.0", f"Invalid JSON-RPC header: {response}"
        assert response.get("id") == self.req_id, f"Mismatched request id: {response}"
        return response

    def close(self):
        if self.proc:
            self.proc.terminate()
            stdout_rem, stderr_out = self.proc.communicate()
            return stdout_rem, stderr_out


@pytest.fixture
def mcp_client():
    client = MCPStdioClient()
    # Perform initialize handshake
    init_res = client.send_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-suite", "version": "1.0"}
    })
    assert "result" in init_res
    assert init_res["result"]["serverInfo"]["name"] == "customer-operations-server"
    yield client
    client.close()


def test_stdio_isolation_and_logging(mcp_client):
    """Verify that logging output goes exclusively to stderr while stdout remains pure JSON-RPC."""
    # Execute a tool call which triggers logging
    res = mcp_client.send_request("tools/call", {
        "name": "get_customer_record",
        "arguments": {"customer_id": "CUST-10001"}
    })
    assert "result" in res

    # Terminate and inspect stderr
    stdout_rem, stderr_out = mcp_client.close()
    assert stdout_rem.strip() == "", "Stdout should not have any stray trailing text"
    assert "Customer Operations MCP Server starting on stdio transport..." in stderr_out
    assert "Executing tool 'get_customer_record'" in stderr_out
    assert "Successfully fetched record for CUST-10001" in stderr_out


def test_tools_list_schema_definitions(mcp_client):
    """Verify tools/list exposes get_customer_record and trigger_refund with schemas."""
    res = mcp_client.send_request("tools/list")
    assert "result" in res
    tools = {t["name"]: t for t in res["result"]["tools"]}

    assert "get_customer_record" in tools
    assert "trigger_refund" in tools

    # Inspect get_customer_record schema
    cust_tool = tools["get_customer_record"]
    assert "customer_id" in cust_tool["inputSchema"]["properties"]

    # Inspect trigger_refund schema
    refund_tool = tools["trigger_refund"]
    assert "customer_id" in refund_tool["inputSchema"]["properties"]
    assert "amount" in refund_tool["inputSchema"]["properties"]
    assert "reason" in refund_tool["inputSchema"]["properties"]


def test_get_customer_record_valid(mcp_client):
    """Verify valid customer record retrieval."""
    res = mcp_client.send_request("tools/call", {
        "name": "get_customer_record",
        "arguments": {"customer_id": "CUST-10001"}
    })
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert content["customer_id"] == "CUST-10001"
    assert content["name"] == "Sarah Connor"
    assert content["tier"] == "ENTERPRISE"


@pytest.mark.parametrize("invalid_id", [
    "12345",              # Missing CUST- prefix
    "CUST-1234",          # Too short (4 chars instead of 5)
    "CUST-123456",        # Too long (6 chars)
    "CUSTOMER-10001",     # Wrong prefix
    "CUST-!!@@#",         # Special characters
    "",                   # Empty string
])
def test_get_customer_record_invalid_rejects_with_32602(mcp_client, invalid_id):
    """Verify malformed customer IDs are rejected with standard JSON-RPC error code -32602."""
    res = mcp_client.send_request("tools/call", {
        "name": "get_customer_record",
        "arguments": {"customer_id": invalid_id}
    })
    assert "error" in res, f"Expected error response for '{invalid_id}', got: {res}"
    assert res["error"]["code"] == -32602, f"Expected error code -32602, got: {res['error']}"
    assert "Invalid parameters" in res["error"]["message"]


def test_trigger_refund_valid(mcp_client):
    """Verify valid refund trigger processing."""
    res = mcp_client.send_request("tools/call", {
        "name": "trigger_refund",
        "arguments": {
            "customer_id": "CUST-A99B2",
            "amount": 129.50,
            "reason": "Customer reported billing calculation discrepancy on invoice."
        }
    })
    assert "result" in res
    data = json.loads(res["result"]["content"][0]["text"])
    assert data["customer_id"] == "CUST-A99B2"
    assert data["amount_usd"] == 129.50
    assert data["status"] == "PROCESSED"
    assert data["refund_id"].startswith("REF-")


@pytest.mark.parametrize("bad_args,expected_substr", [
    (
        {"customer_id": "INVALID", "amount": 50.0, "reason": "Valid reason length here"},
        "customer_id"
    ),
    (
        {"customer_id": "CUST-10001", "amount": -10.0, "reason": "Valid reason length here"},
        "amount"
    ),
    (
        {"customer_id": "CUST-10001", "amount": 0.0, "reason": "Valid reason length here"},
        "amount"
    ),
    (
        {"customer_id": "CUST-10001", "amount": 50.0, "reason": "short"},
        "reason"
    ),
    (
        {"customer_id": "CUST-10001", "amount": 50.0, "reason": "          "},
        "reason"
    )
])
def test_trigger_refund_invalid_rejects_with_32602(mcp_client, bad_args, expected_substr):
    """Verify malformed refund inputs reject with JSON-RPC error -32602 and specify offending field."""
    res = mcp_client.send_request("tools/call", {
        "name": "trigger_refund",
        "arguments": bad_args
    })
    assert "error" in res, f"Expected error response for {bad_args}, got: {res}"
    assert res["error"]["code"] == -32602
    assert expected_substr in res["error"]["message"]
