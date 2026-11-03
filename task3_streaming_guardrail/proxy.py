"""
Task 3: LLM Gateway Streaming Proxy Endpoint
Routes text generation requests to upstream LLM provider and streams the response
back to the client while intercepting and redacting PII in real-time.
"""

from typing import Optional, AsyncGenerator
import time
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse, JSONResponse
from starlette.routing import Route
import logging
from task3_streaming_guardrail.guardrail import StreamingPIIGuardrail

logger = logging.getLogger("llm.gateway.guardrail")


class LLMStreamingProxyGateway:
    def __init__(
        self,
        upstream_url: Optional[str] = None,
        upstream_client: Optional[httpx.AsyncClient] = None
    ):
        self.upstream_url = upstream_url.rstrip("/") if upstream_url else "http://mock-llm"
        self._upstream_client = upstream_client
        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self.handle_chat_completions, methods=["POST"]),
                Route("/v1/stream/raw", self.handle_raw_stream, methods=["POST"]),
                Route("/health", self.handle_health, methods=["GET"])
            ]
        )

    async def get_client(self) -> httpx.AsyncClient:
        if self._upstream_client is not None:
            return self._upstream_client
        self._upstream_client = httpx.AsyncClient(
            base_url=self.upstream_url,
            timeout=30.0
        )
        return self._upstream_client

    async def close(self):
        if self._upstream_client and not self._upstream_client.is_closed:
            await self._upstream_client.aclose()

    async def handle_health(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "UP", "service": "llm-streaming-guardrail-proxy"})

    async def handle_chat_completions(self, request: Request) -> StreamingResponse:
        """
        Proxy OpenAI-compatible SSE completion stream with PII redaction guardrail.
        """
        body = await request.json()
        client = await self.get_client()

        req = client.build_request(
            "POST",
            "/v1/chat/completions",
            json=body,
            headers={"Content-Type": "application/json"}
        )
        upstream_resp = await client.send(req, stream=True)

        if upstream_resp.status_code != 200:
            content = await upstream_resp.aread()
            return StreamingResponse(
                iter([content]),
                status_code=upstream_resp.status_code,
                media_type="application/json"
            )

        guardrail = StreamingPIIGuardrail(max_lookback=40)

        async def stream_generator() -> AsyncGenerator[bytes, None]:
            try:
                async def line_stream() -> AsyncGenerator[str, None]:
                    async for line in upstream_resp.aiter_lines():
                        yield line

                async for sse_chunk in guardrail.transform_sse_stream(line_stream()):
                    yield sse_chunk.encode("utf-8")
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    async def handle_raw_stream(self, request: Request) -> StreamingResponse:
        """
        Proxy raw chunked text stream with real-time PII redaction guardrail.
        """
        body = await request.json()
        client = await self.get_client()

        req = client.build_request(
            "POST",
            "/v1/stream/raw",
            json=body,
            headers={"Content-Type": "application/json"}
        )
        upstream_resp = await client.send(req, stream=True)

        if upstream_resp.status_code != 200:
            content = await upstream_resp.aread()
            return StreamingResponse(iter([content]), status_code=upstream_resp.status_code)

        guardrail = StreamingPIIGuardrail(max_lookback=40)

        async def raw_generator() -> AsyncGenerator[bytes, None]:
            try:
                async def text_chunks() -> AsyncGenerator[str, None]:
                    async for chunk in upstream_resp.aiter_text():
                        yield chunk

                async for out_chunk in guardrail.transform_text_stream(text_chunks()):
                    yield out_chunk.encode("utf-8")
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            raw_generator(),
            media_type="text/plain"
        )
