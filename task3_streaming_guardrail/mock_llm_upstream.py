"""
Task 3: Mock LLM Upstream Streaming Provider
Simulates an OpenAI/Anthropic compatible LLM provider that streams completion tokens
using Server-Sent Events (SSE) or raw chunked text.
"""

import asyncio
import json
from typing import List, AsyncGenerator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route


class MockLLMUpstream:
    def __init__(self, chunk_delay: float = 0.005):
        self.chunk_delay = chunk_delay
        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self.handle_chat_completion, methods=["POST"]),
                Route("/v1/stream/raw", self.handle_raw_stream, methods=["POST"]),
            ]
        )

    async def generate_sse_stream(self, chunks: List[str]) -> AsyncGenerator[str, None]:
        for i, text in enumerate(chunks):
            if self.chunk_delay > 0:
                await asyncio.sleep(self.chunk_delay)
            frame = {
                "id": "chatcmpl-mock-123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "mock-gpt-4o",
                "choices": [{
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(frame)}\n\n"

        yield "data: [DONE]\n\n"

    async def generate_raw_stream(self, chunks: List[str]) -> AsyncGenerator[str, None]:
        for text in chunks:
            if self.chunk_delay > 0:
                await asyncio.sleep(self.chunk_delay)
            yield text

    async def handle_chat_completion(self, request: Request) -> StreamingResponse:
        body = await request.json()
        messages = body.get("messages", [])
        custom_chunks = body.get("stream_chunks")

        if custom_chunks:
            chunks = custom_chunks
        else:
            # Default sequence containing split PII for testing
            chunks = [
                "Hello! Here is ",
                "the customer info: ",
                "email: user",
                ".testing",
                "@example.com, ",
                "and SSN is ",
                "123-",
                "45-",
                "6789.",
                " Have a great day!"
            ]

        return StreamingResponse(
            self.generate_sse_stream(chunks),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    async def handle_raw_stream(self, request: Request) -> StreamingResponse:
        body = await request.json()
        chunks = body.get("stream_chunks", ["chunk1", "chunk2"])
        return StreamingResponse(
            self.generate_raw_stream(chunks),
            media_type="text/plain"
        )
