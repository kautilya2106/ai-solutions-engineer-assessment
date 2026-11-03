"""
Automated Tests for Task 3: LLM Gateway Streaming Guardrail (PII Redaction)
Verifies:
1. Pattern detection for Emails, SSNs, and Credit Cards.
2. Streaming boundary handling: PII split across multiple small chunks.
3. TTFT (Time To First Token) preservation: stream emits incrementally without accumulating full response.
4. Memory efficiency (bounded buffer size).
5. End-to-end proxy streaming with SSE and raw chunked streams.
"""

import pytest
import asyncio
import json
import httpx

from task3_streaming_guardrail.patterns import (
    redact_pii_text,
    luhn_checksum_valid,
    REDACTED_TAG
)
from task3_streaming_guardrail.guardrail import StreamingPIIGuardrail
from task3_streaming_guardrail.proxy import LLMStreamingProxyGateway
from task3_streaming_guardrail.mock_llm_upstream import MockLLMUpstream


# ============================================================================
# 1. Pattern Matching & Redaction Unit Tests
# ============================================================================

def test_pattern_email_redaction():
    text = "Please reach out to support@acme-corp.com or john.doe+billing@sub.example.org for help."
    result = redact_pii_text(text)
    assert "support@acme-corp.com" not in result
    assert "john.doe+billing@sub.example.org" not in result
    assert result.count(REDACTED_TAG) == 2


def test_pattern_ssn_redaction():
    # Formatted SSN
    text1 = "Customer SSN is 123-45-6789 on the document."
    assert redact_pii_text(text1) == f"Customer SSN is {REDACTED_TAG} on the document."

    # Raw 9-digit SSN
    text2 = "Tax ID: 219458291."
    assert redact_pii_text(text2) == f"Tax ID: {REDACTED_TAG}."


def test_pattern_credit_card_redaction():
    # Valid Visa card (passes Luhn algorithm: 4532 0150 1234 5671)
    valid_visa = "4532 0150 1234 5671"
    assert luhn_checksum_valid(valid_visa)
    text = f"Charged credit card: {valid_visa}."
    assert redact_pii_text(text) == f"Charged credit card: {REDACTED_TAG}."

    # Hyphenated card
    dashed = "4532-0150-1234-5671"
    assert redact_pii_text(f"Card {dashed}") == f"Card {REDACTED_TAG}"


# ============================================================================
# 2. Streaming Buffer & Boundary Problem Unit Tests
# ============================================================================

def test_streaming_email_split_across_chunks():
    """Verify that an email sliced arbitrarily across chunks is properly redacted."""
    chunks = [
        "Welcome to the portal. ",
        "Your account contact is admin.sec",
        "urity@cloud",
        "defense.org. Please ",
        "check your inbox."
    ]

    guardrail = StreamingPIIGuardrail(max_lookback=35)
    emitted = []

    for chunk in chunks:
        out = guardrail.process_chunk(chunk)
        if out:
            emitted.append(out)

    final = guardrail.finalize()
    if final:
        emitted.append(final)

    full_output = "".join(emitted)
    assert "admin.security@clouddefense.org" not in full_output
    assert REDACTED_TAG in full_output
    assert "Welcome to the portal." in full_output
    assert "Please check your inbox." in full_output


def test_streaming_ssn_split_across_chunks():
    """Verify SSN split across 3 chunks is detected and redacted."""
    chunks = [
        "Identity verification: SSN is ",
        "123-",
        "45-",
        "6789. ",
        "Status: Approved."
    ]

    guardrail = StreamingPIIGuardrail(max_lookback=30)
    emitted = []
    for chunk in chunks:
        out = guardrail.process_chunk(chunk)
        if out:
            emitted.append(out)
    final = guardrail.finalize()
    if final:
        emitted.append(final)

    full_output = "".join(emitted)
    assert "123-45-6789" not in full_output
    assert REDACTED_TAG in full_output
    assert "Status: Approved." in full_output


def test_streaming_credit_card_split_across_chunks():
    """Verify credit card split across 4 small token chunks is detected and redacted."""
    chunks = [
        "Billing card: ",
        "4532 ",
        "0150 ",
        "1234 ",
        "5671",
        " has been billed."
    ]

    guardrail = StreamingPIIGuardrail(max_lookback=35)
    emitted = []
    for chunk in chunks:
        out = guardrail.process_chunk(chunk)
        if out:
            emitted.append(out)
    final = guardrail.finalize()
    if final:
        emitted.append(final)

    full_output = "".join(emitted)
    assert "4532 0150 1234 5671" not in full_output
    assert REDACTED_TAG in full_output
    assert "has been billed." in full_output


def test_streaming_ttft_and_incremental_emission():
    """
    CRITICAL REQUIREMENT:
    Verify stream emits chunks incrementally without waiting for full completion,
    minimizing Time To First Token (TTFT).
    """
    guardrail = StreamingPIIGuardrail(max_lookback=25)

    # Long text in 5 pieces
    piece1 = "This is the initial greeting sentence that is definitely longer than twenty-five chars. "
    piece2 = "Here is the second sentence describing system features in detail. "
    piece3 = "Contact developer at john@internal.corp for questions. "
    piece4 = "The final closing sentence ends here."

    # Process piece 1: Must emit immediately (since len > 25)
    out1 = guardrail.process_chunk(piece1)
    assert len(out1) > 0, "Guardrail must emit prefix of piece 1 immediately to ensure low TTFT!"
    assert "This is the initial greeting" in out1

    # Process piece 2: Must emit incrementally
    out2 = guardrail.process_chunk(piece2)
    assert len(out2) > 0, "Guardrail must continue emitting incrementally on subsequent chunks!"

    # Process piece 3 (contains email): Email must be redacted
    out3 = guardrail.process_chunk(piece3)

    # Process piece 4 & finalize
    out4 = guardrail.process_chunk(piece4)
    out5 = guardrail.finalize()

    total_emitted = out1 + out2 + out3 + out4 + out5
    assert "john@internal.corp" not in total_emitted
    assert REDACTED_TAG in total_emitted


def test_memory_efficiency_bounded_buffer():
    """Verify buffer size stays bounded O(1) regardless of total stream size."""
    guardrail = StreamingPIIGuardrail(max_lookback=40)
    max_observed_buffer = 0

    # Stream 1,000 chunks of safe text
    for i in range(1000):
        chunk = f"chunk_{i:04d} word data "
        guardrail.process_chunk(chunk)
        if len(guardrail.buffer) > max_observed_buffer:
            max_observed_buffer = len(guardrail.buffer)

    guardrail.finalize()
    # Max buffer size should never exceed max_lookback + chunk_size
    assert max_observed_buffer < 100, f"Buffer grew unexpectedly large: {max_observed_buffer}"


# ============================================================================
# 3. End-to-End Gateway Proxy Integration Tests
# ============================================================================

@pytest.fixture
def streaming_setup():
    upstream = MockLLMUpstream(chunk_delay=0.001)
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream.app),
        base_url="http://mock-llm"
    )
    proxy = LLMStreamingProxyGateway(upstream_client=upstream_client)
    proxy_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app),
        base_url="http://streaming-proxy"
    )

    return {
        "proxy_client": proxy_client,
        "proxy": proxy,
        "upstream": upstream,
        "upstream_client": upstream_client
    }


@pytest.mark.asyncio
async def test_end_to_end_raw_streaming_proxy(streaming_setup):
    """Test raw chunked text streaming through the proxy gateway with split PII."""
    client = streaming_setup["proxy_client"]

    split_chunks = [
        "Welcome! Your manager email is ",
        "steve.",
        "jobs@apple",
        ".com. ",
        "National ID is 123-",
        "45-6789.",
        " All confirmed."
    ]

    req_payload = {"stream_chunks": split_chunks}

    received_parts = []
    async with client.stream("POST", "/v1/stream/raw", json=req_payload) as response:
        assert response.status_code == 200
        async for chunk in response.aiter_text():
            if chunk:
                received_parts.append(chunk)

    full_body = "".join(received_parts)
    assert "steve.jobs@apple.com" not in full_body
    assert "123-45-6789" not in full_body
    assert full_body.count(REDACTED_TAG) >= 2
    assert "Welcome! Your manager email is" in full_body
    assert "All confirmed." in full_body


@pytest.mark.asyncio
async def test_end_to_end_sse_chat_completions_proxy(streaming_setup):
    """Test OpenAI-compatible SSE chat completions stream through proxy gateway."""
    client = streaming_setup["proxy_client"]

    split_chunks = [
        "Dear Customer, ",
        "we noticed payment issue with account ",
        "finance@fintech",
        ".global. ",
        "Please re-enter your card ",
        "4532 0150 ",
        "1234 5671",
        " to proceed."
    ]

    req_payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Check my account"}],
        "stream": True,
        "stream_chunks": split_chunks
    }

    sse_lines = []
    collected_deltas = []

    async with client.stream("POST", "/v1/chat/completions", json=req_payload) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")

        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            sse_lines.append(line)
            if line.startswith("data: ") and line != "data: [DONE]":
                event = json.loads(line[len("data: "):])
                delta_content = event["choices"][0]["delta"].get("content", "")
                if delta_content:
                    collected_deltas.append(delta_content)

    # Verify standard SSE terminator
    assert "data: [DONE]" in sse_lines
    assert len(collected_deltas) > 1, "Expected incremental deltas streamed across wire"

    full_text = "".join(collected_deltas)
    assert "finance@fintech.global" not in full_text
    assert "4532 0150 1234 5671" not in full_text
    assert REDACTED_TAG in full_text
    assert "Dear Customer" in full_text
    assert "to proceed." in full_text
