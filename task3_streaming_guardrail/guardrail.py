"""
Task 3: Streaming PII Guardrail
High-performance rolling buffer state machine for real-time stream redaction.
Solves the chunk-boundary partial match problem while preserving Time To First Token (TTFT)
and maintaining O(1) memory footprint.
"""

import re
import json
from typing import AsyncGenerator, Optional, List
from task3_streaming_guardrail.patterns import redact_pii_text, REDACTED_TAG

# Maximum lookback tail retained across chunk boundaries to prevent split PII leakage
DEFAULT_MAX_LOOKBACK = 40
SAFE_BOUNDARY_CHARS = {" ", "\n", "\t", ",", ";", "!", "?", "(", ")", "[", "]", "{", "}", "\"", "'"}


class StreamingPIIGuardrail:
    """
    Rolling buffer stream processor.
    Buffers only a minimal tail (up to max_lookback chars) to detect PII that spans chunk boundaries,
    flushing safe prefixes immediately to minimize Time To First Token (TTFT).
    """

    def __init__(self, max_lookback: int = DEFAULT_MAX_LOOKBACK):
        self.max_lookback = max_lookback
        self.buffer = ""
        self.chunks_processed = 0
        self.total_bytes_emitted = 0

    def process_chunk(self, chunk: str) -> str:
        """
        Ingest a raw text chunk, perform real-time redaction, and return the safe flushed prefix.
        Returns empty string if chunk is held in lookback buffer.
        """
        if not chunk:
            return ""

        self.chunks_processed += 1
        self.buffer += chunk

        # Redact any completed PII in the current buffer
        self.buffer = redact_pii_text(self.buffer)

        # If buffer exceeds safe lookback threshold, flush the safe prefix
        if len(self.buffer) > self.max_lookback:
            # Look for a clean delimiter boundary to avoid splitting clean words
            target_cut = len(self.buffer) - self.max_lookback
            cut_index = -1
            for i in range(target_cut, max(0, target_cut - 20), -1):
                if self.buffer[i] in SAFE_BOUNDARY_CHARS:
                    cut_index = i + 1
                    break

            if cut_index == -1:
                cut_index = target_cut

            to_emit = self.buffer[:cut_index]
            self.buffer = self.buffer[cut_index:]
            self.total_bytes_emitted += len(to_emit)
            return to_emit

        return ""

    def finalize(self) -> str:
        """
        Flush any remaining buffer at end of stream after one final redaction pass.
        Must be called when upstream stream signals completion.
        """
        if not self.buffer:
            return ""

        remaining = redact_pii_text(self.buffer)
        self.buffer = ""
        self.total_bytes_emitted += len(remaining)
        return remaining

    async def transform_text_stream(self, stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
        """Async generator wrapper for raw text streams."""
        async for chunk in stream:
            emitted = self.process_chunk(chunk)
            if emitted:
                yield emitted

        final = self.finalize()
        if final:
            yield final

    async def transform_sse_stream(self, stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
        """
        Async generator wrapper for OpenAI-compatible Server-Sent Events (SSE) streams.
        Parses incoming JSON deltas, applies streaming guardrail, and re-emits SSE frames.
        """
        async for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue

            if line == "data: [DONE]":
                # Flush remaining buffer before forwarding [DONE]
                final_tail = self.finalize()
                if final_tail:
                    out_frame = {
                        "choices": [{"delta": {"content": final_tail}, "index": 0, "finish_reason": None}]
                    }
                    yield f"data: {json.dumps(out_frame)}\n\n"
                yield "data: [DONE]\n\n"
                continue

            if line.startswith("data: "):
                raw_json = line[len("data: "):]
                try:
                    event = json.loads(raw_json)
                    delta = event.get("choices", [{}])[0].get("delta", {})
                    content_chunk = delta.get("content", "")
                except Exception:
                    # Non-JSON or unsupported format: pass raw through process_chunk
                    content_chunk = raw_json

                if content_chunk:
                    emitted = self.process_chunk(content_chunk)
                    if emitted:
                        out_frame = {
                            "choices": [{"delta": {"content": emitted}, "index": 0, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(out_frame)}\n\n"
            else:
                # Other SSE lines (e.g. event:, id:, comments)
                yield f"{line}\n\n"
