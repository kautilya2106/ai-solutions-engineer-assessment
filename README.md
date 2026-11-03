# AI Field Developer & Solutions Engineer Technical Assessment

This repository contains production-grade implementations for all 4 core assessment tasks across **Model Context Protocol (MCP) Servers**, **MCP Gateways**, **LLM Gateways**, **Streaming Security Guardrails**, and **Resilient Model Routing**.

---

## Architecture Overview

```
                                      AI Agent Client
                                            │
                     ┌──────────────────────┴──────────────────────┐
                     ▼                                             ▼
        [Task 2: MCP Security Gateway]              [Task 4: LLM Resilient Router]
                     │ (Bearer Auth & Tool ACLs)                   │ (Token Sliding Window Limiter)
                     ▼                                             ▼
        [Task 1: MCP Customer Server]               [Task 3: Streaming Guardrail Proxy]
          (Strict Pydantic / STDIO)                 (Zero-leakage Boundary Redaction)
                                                                   │
                                                     ┌─────────────┴─────────────┐
                                                     ▼                           ▼
                                            Primary Model LLM          Secondary Backup LLM
                                            (3000ms Timeout / 429)     (Automatic Failover)
```

---

## Summary of Completed Tasks

| Task | Core Focus | Key Technologies | Status |
| :--- | :--- | :--- | :--- |
| **Task 1** | Custom MCP Server with Strict Validation & Transport | Official `mcp` SDK (2.x), Pydantic, STDIO isolation | ✅ **Passed (15/15 tests)** |
| **Task 2** | MCP Security Gateway Proxy (Tool Filtering & Auth) | Starlette, JSON-RPC 2.0, Bearer / JWT Auth, ACL Interceptor | ✅ **Passed (7/7 tests)** |
| **Task 3** | LLM Gateway Streaming Guardrail (PII Redaction) | Rolling Buffer Window, SSE / OpenAI Streaming, Luhn Check | ✅ **Passed (10/10 tests)** |
| **Task 4** | Rate-Limiting & Model Fallback Router | On-disk SQLite (WAL Mode), Sliding Window, Failover Engine | ✅ **Passed (8/8 tests)** |

---

## Task 1: Custom MCP Server with Strict Validation & Transport

### Objective
Build a runnable Model Context Protocol (MCP) server over `stdio` transport exposing two tools with strict input validation and guaranteed isolation between JSON-RPC stdout frames and diagnostic stderr logs.

### Tools Implemented
1. `get_customer_record`:
   - Validates `customer_id` strictly adheres to `CUST-XXXXX` regex (`^CUST-[A-Za-z0-9]{5}$`).
   - Rejects malformed IDs with standard MCP JSON-RPC error code `-32602` (`INVALID_PARAMS`).
2. `trigger_refund`:
   - Validates `customer_id` format (`CUST-XXXXX`).
   - Validates `amount > 0.0` (positive float).
   - Validates `reason` minimum length of 10 non-whitespace characters.
   - Rejects violations with standard MCP JSON-RPC error code `-32602`.

### Evaluation Criteria Verification
- **STDIO Isolation**: All diagnostic, startup, and execution logs write exclusively to `sys.stderr`. `sys.stdout` is purely reserved for newline-delimited JSON-RPC wire frames. Tested and verified in subprocess integration test.
- **Protocol Compliance**: Implements MCP 2.x protocol initialization handshake (`initialize`), dynamic tool discovery (`tools/list`), and execution (`tools/call`).
- **Validation**: Pydantic schema validation models (`GetCustomerRecordInput` and `TriggerRefundInput`) reject all boundary violations.

---

## Task 2: MCP Security Gateway Proxy (Tool Filtering & Auth)

### Objective
Build a lightweight HTTP/JSON-RPC reverse proxy that intercepts MCP client requests, authenticates callers via Bearer tokens, extracts role permissions (`admin` vs `viewer`), and enforces fine-grained tool execution boundaries.

### Implementation Details
- **Bearer Token Extraction**:
  - Parses `Authorization: Bearer <token>` supporting both JWT signed tokens and secure API keys.
  - Extracts caller role context (`admin` or `viewer`).
- **Payload Inspection & Forwarding**:
  - `tools/list`: Transparently forwarded downstream.
  - `tools/call`:
    - Checks tool name prefix. If name begins with `admin_` (e.g., `admin_reset_key`, `admin_purge_cache`):
      - If role is not `admin`: **Interception occurs**. Returns JSON-RPC error code `-32001` (`Unauthorized Tool Call`) with HTTP 403. **Downstream mock server is never contacted.**
      - If role is `admin`: Forwarded downstream and returns downstream execution result.
    - Non-admin tools (`get_customer_record`, `query_status`): Forwarded transparently for both `viewer` and `admin`.

---

## Task 3: LLM Gateway Streaming Guardrail (PII Redaction)

### Objective
Implement an LLM Gateway streaming proxy endpoint that detects and redacts sensitive PII (Emails, SSNs, Credit Cards) in real-time while preserving minimal Time To First Token (TTFT) and maintaining an $O(1)$ memory footprint.

### The Chunk-Boundary Problem & State Machine Solution
If sensitive data spans two chunks (e.g., chunk 1: `john.doe`, chunk 2: `@example.com`), naive per-chunk regex matching leaks the entity. Accumulating the whole document destroys streaming TTFT.

**Rolling Lookback Buffer Architecture**:
1. When a chunk arrives, append to `buffer`.
2. Redact completed PII entities with `[REDACTED]`.
3. Keep a lookback safety window (max 40 characters) at the tail to guard against incomplete prefixes.
4. Locate the nearest word boundary/delimiter before the lookback threshold and immediately flush the safe prefix to the client.
5. On stream termination (`[DONE]`), perform final redaction pass and flush remaining tail.

**Performance Characteristics**:
- **Zero Leakage**: Redacts emails, formatted/raw SSNs, and credit cards (verified by Luhn algorithm) even when split across 2–4 chunk boundaries.
- **Minimal TTFT**: First token emitted immediately on initial chunk threshold.
- **$O(1)$ Memory**: Buffer never accumulates more than `lookback_window + chunk_size` bytes.

---

## Task 4: Rate Limiting & Model Fallback Router (SQLite On-Disk)

### Objective
Implement a resilient LLM model router with a token-aware sliding window rate limiter backed by an on-disk SQLite database, and automatic failover from primary to secondary model providers upon 429 status or 3000ms timeout.

### Implementation Details
- **On-Disk SQLite Rate Limiter**:
  - Configured with Write-Ahead Logging (`PRAGMA journal_mode = WAL`), `busy_timeout = 5000ms`, and composite indexing `(tenant_id, timestamp)`.
  - Enforces sliding window token quota (50,000 tokens/minute per tenant API key).
  - Automatically evicts expired records (`timestamp < now - 60s`).
  - Thread-safe and persistent across process restarts.
- **Failover Trigger Conditions**:
  - Primary provider returns **HTTP 429 Too Many Requests**.
  - Primary provider **times out after 3000ms (3.0s)**.
  - Automatically fails over to the secondary backup provider, attaching diagnostic headers `X-Gateway-Route: fallback-secondary`.
- **Sanitized Error Envelopes**:
  - When both providers fail or timeout, returns a standardized gateway error payload:
    ```json
    {
      "error": {
        "code": "upstream_unavailable",
        "message": "Both primary model provider and backup model provider failed to service request.",
        "request_id": "req-...",
        "tenant_id": "tenant-...",
        "timestamp": "2026-11-03T18:45:00Z"
      }
    }
    ```
  - Guaranteed zero leakage of internal stack traces, SQLite database paths, or private hostnames.

---

## Running the Verification Suite & Interactive Demo

### Prerequisites
- Python 3.10+
- Virtual environment with dependencies installed:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Running All Automated Tests
```bash
python3 run_all_tests.py
```
Or directly with pytest:
```bash
.venv/bin/pytest -v
```

### Running the Live Interactive Demo
```bash
.venv/bin/python3 demo.py
```
