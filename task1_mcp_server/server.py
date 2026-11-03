"""
Task 1: Custom MCP Server with Strict Validation & Transport Handling
Built with official MCP Python SDK (mcp 2.x).
STDIO transport with stdout strictly isolated for JSON-RPC frames and all logging routed to stderr.
"""

import sys
import os
import json
import logging
from typing import Any, Dict
from pydantic import ValidationError

from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from task1_mcp_server.models import (
    GetCustomerRecordInput,
    TriggerRefundInput,
)

# Strict STDIO Isolation: Configure root logger to output exclusively to sys.stderr
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("mcp.server.customer_ops")

# JSON-RPC Standard Error Code for Invalid Parameters (-32602)
JSONRPC_INVALID_PARAMS = -32602

# In-memory mock database of customer records
MOCK_CUSTOMERS: Dict[str, Dict[str, Any]] = {
    "CUST-10001": {
        "customer_id": "CUST-10001",
        "name": "Sarah Connor",
        "email": "s.connor@cyberdyne.io",
        "tier": "ENTERPRISE",
        "status": "ACTIVE",
        "balance": 15420.50,
        "created_at": "2023-01-15T09:30:00Z"
    },
    "CUST-A99B2": {
        "customer_id": "CUST-A99B2",
        "name": "Miles Dyson",
        "email": "m.dyson@cyberdyne.io",
        "tier": "PREMIUM",
        "status": "ACTIVE",
        "balance": 3499.00,
        "created_at": "2023-05-20T14:15:00Z"
    },
    "CUST-12345": {
        "customer_id": "CUST-12345",
        "name": "John Doe",
        "email": "john.doe@example.com",
        "tier": "STANDARD",
        "status": "ACTIVE",
        "balance": 250.00,
        "created_at": "2024-02-10T11:00:00Z"
    }
}

server = MCPServer("customer-operations-server")


@server.tool(
    name="get_customer_record",
    description="Retrieve a detailed customer profile by customer_id (must match format CUST-XXXXX)."
)
async def get_customer_record(customer_id: str) -> str:
    """
    Exposes get_customer_record tool with strict Pydantic validation.
    Rejects malformed IDs with standard MCP JSON-RPC error code -32602.
    """
    logger.info("Executing tool 'get_customer_record' with customer_id: %s", customer_id)
    try:
        validated = GetCustomerRecordInput(customer_id=customer_id)
    except ValidationError as err:
        err_msg = f"Invalid parameters for 'get_customer_record': {err.errors()[0]['msg']}"
        logger.warning("Validation failed: %s", err_msg)
        raise MCPError(code=JSONRPC_INVALID_PARAMS, message=err_msg)

    cid = validated.customer_id
    if cid in MOCK_CUSTOMERS:
        record = MOCK_CUSTOMERS[cid]
    else:
        # Generate deterministic synthetic record for valid IDs not in seed DB
        record = {
            "customer_id": cid,
            "name": f"Customer {cid}",
            "email": f"{cid.lower()}@customer-directory.internal",
            "tier": "STANDARD",
            "status": "ACTIVE",
            "balance": 0.00,
            "created_at": "2024-01-01T00:00:00Z"
        }

    logger.info("Successfully fetched record for %s", cid)
    return json.dumps(record, indent=2)


@server.tool(
    name="trigger_refund",
    description="Trigger a customer monetary refund. Inputs: customer_id (CUST-XXXXX), amount (>0), reason (len >= 10)."
)
async def trigger_refund(customer_id: str, amount: float, reason: str) -> str:
    """
    Exposes trigger_refund tool with strict Pydantic validation.
    Rejects malformed arguments with standard MCP JSON-RPC error code -32602.
    """
    logger.info(
        "Executing tool 'trigger_refund' for customer_id: %s, amount: %s, reason: %s",
        customer_id, amount, reason
    )
    try:
        validated = TriggerRefundInput(customer_id=customer_id, amount=amount, reason=reason)
    except ValidationError as err:
        err_details = [f"{e['loc'][0]}: {e['msg']}" for e in err.errors()]
        err_msg = f"Invalid parameters for 'trigger_refund': {'; '.join(err_details)}"
        logger.warning("Validation failed: %s", err_msg)
        raise MCPError(code=JSONRPC_INVALID_PARAMS, message=err_msg)

    import uuid
    refund_id = f"REF-{uuid.uuid4().hex[:8].upper()}"
    result = {
        "refund_id": refund_id,
        "customer_id": validated.customer_id,
        "amount_usd": round(validated.amount, 2),
        "reason": validated.reason,
        "status": "PROCESSED",
        "processed_at": "2026-11-03T18:30:00Z"
    }

    logger.info("Successfully processed refund %s for customer %s ($%.2f)", refund_id, validated.customer_id, validated.amount)
    return json.dumps(result, indent=2)


def main():
    """Main entrypoint running MCP stdio transport."""
    logger.info("Customer Operations MCP Server starting on stdio transport...")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
