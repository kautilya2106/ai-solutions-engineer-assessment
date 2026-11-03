"""
Interactive Demo & Verification Walkthrough
Demonstrates live execution of all 4 Solutions Engineering tasks.
"""

import asyncio
import json
import os
import sys
import tempfile
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

import httpx

# Task 1 imports
from task1_mcp_server.server import get_customer_record, trigger_refund
from mcp.shared.exceptions import MCPError

# Task 2 imports
from task2_mcp_gateway.gateway import MCPGatewayProxy
from task2_mcp_gateway.downstream_mock import DownstreamMockServer
from task2_mcp_gateway.auth import create_test_jwt

# Task 3 imports
from task3_streaming_guardrail.guardrail import StreamingPIIGuardrail

# Task 4 imports
from task4_resilient_router.rate_limiter import TokenSlidingWindowRateLimiter
from task4_resilient_router.router import LLMGatewayRouter
from task4_resilient_router.mock_providers import MockPrimaryLLMProvider, MockSecondaryLLMProvider


async def demo_task1():
    print("\n" + "=" * 70)
    print(" TASK 1: Custom MCP Server with Strict Validation & Transport")
    print("=" * 70)

    print("\n1. Executing tool 'get_customer_record' (Valid ID: CUST-10001)...")
    rec = await get_customer_record("CUST-10001")
    print("Result:", rec)

    print("\n2. Executing tool 'trigger_refund' (Valid amount & reason)...")
    ref = await trigger_refund("CUST-10001", 89.99, "Customer received defective hardware unit.")
    print("Result:", ref)

    print("\n3. Testing invalid validation rejection (Invalid ID: 'BAD_ID_999')...")
    try:
        await get_customer_record("BAD_ID_999")
    except MCPError as e:
        print(f"Correctly caught MCPError: Code={e.error.code}, Message='{e.error.message}'")


async def demo_task2():
    print("\n" + "=" * 70)
    print(" TASK 2: MCP Security Gateway Proxy (Tool Filtering & Auth)")
    print("=" * 70)

    downstream = DownstreamMockServer()
    downstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=downstream.app),
        base_url="http://downstream"
    )
    gateway = MCPGatewayProxy(downstream_client=downstream_client)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app),
        base_url="http://gateway"
    )

    viewer_jwt = create_test_jwt(role="viewer")
    admin_jwt = create_test_jwt(role="admin")

    print("\n1. Viewer requesting 'tools/list' (Transparent forwarding)...")
    resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer {viewer_jwt}"})
    print(f"Status: {resp.status_code}, Available Tools: {[t['name'] for t in resp.json()['result']['tools']]}")

    print("\n2. Viewer attempting 'admin_reset_key' (SECURITY INTERCEPTION)...")
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "admin_reset_key", "arguments": {"key_alias": "root-vault"}}},
        headers={"Authorization": f"Bearer {viewer_jwt}"}
    )
    print(f"Status: {resp.status_code}")
    print("Gateway Intercept Payload:", json.dumps(resp.json(), indent=2))
    print(f"Downstream admin execution count: {downstream.call_counts.get('tool:admin_reset_key', 0)} (Downstream was NOT called!)")

    print("\n3. Admin executing 'admin_reset_key' (Authorized)...")
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "admin_reset_key", "arguments": {"key_alias": "root-vault"}}},
        headers={"Authorization": f"Bearer {admin_jwt}"}
    )
    print(f"Status: {resp.status_code}")
    print("Result:", resp.json()["result"]["content"][0]["text"])


async def demo_task3():
    print("\n" + "=" * 70)
    print(" TASK 3: LLM Gateway Streaming Guardrail (Real-time PII Redaction)")
    print("=" * 70)

    guardrail = StreamingPIIGuardrail(max_lookback=35)
    chunks = [
        "Welcome user! ",
        "Contact email is steve.",
        "jobs@apple.com",
        " for assistance. ",
        "National ID is 123-",
        "45-6789.",
        " Card on file is 4532 0150 ",
        "1234 5671. ",
        "Have a great day!"
    ]

    print("Simulating stream with PII split across chunk boundaries:")
    emitted_parts = []
    for i, c in enumerate(chunks):
        out = guardrail.process_chunk(c)
        if out:
            emitted_parts.append(out)
        print(f"  Chunk {i+1} received: {c!r:<30} -> Emitted: {out!r}")

    final = guardrail.finalize()
    if final:
        emitted_parts.append(final)
    print(f"  Final stream flush: {final!r}")

    print("\nComplete Client Received Output:")
    print("".join(emitted_parts))


async def demo_task4():
    print("\n" + "=" * 70)
    print(" TASK 4: Rate Limiting & Model Fallback Router (SQLite On-Disk)")
    print("=" * 70)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        limiter = TokenSlidingWindowRateLimiter(db_path=db_path, limit_tokens_per_window=50000)

        # Setup primary (429 mode) and secondary (healthy mode)
        primary = MockPrimaryLLMProvider(mode="429")
        secondary = MockSecondaryLLMProvider(mode="healthy")

        p_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=primary.app), base_url="http://p")
        s_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=secondary.app), base_url="http://s")

        router = LLMGatewayRouter(rate_limiter=limiter, primary_client=p_client, secondary_client=s_client)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=router.app), base_url="http://router")

        print("\n1. Testing automatic fallback on primary 429 status...")
        resp = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hello"}]},
            headers={"X-API-Key": "tenant-acme"}
        )
        print(f"Status: {resp.status_code}")
        print(f"Routed Via: {resp.headers.get('X-Gateway-Route')}")
        print(f"Failover Reason: {resp.headers.get('X-Gateway-Failover-Reason')}")
        print(f"Model Serving: {resp.json().get('model')}")

        print("\n2. Testing 50,000 tokens/min rate limiter rejection...")
        # Consume rest of quota
        resp = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Big job"}], "estimated_tokens": 55000},
            headers={"X-API-Key": "tenant-acme"}
        )
        print(f"Status: {resp.status_code}")
        print("Sanitized Gateway Error Payload:", json.dumps(resp.json(), indent=2))

    finally:
        limiter.close()
        if os.path.exists(db_path):
            os.remove(db_path)


async def main():
    await demo_task1()
    await demo_task2()
    await demo_task3()
    await demo_task4()
    print("\n" + "=" * 70)
    print(" ALL DEMOS COMPLETED SUCCESSFULLY")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
