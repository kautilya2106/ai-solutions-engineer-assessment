"""
Task 4: Mock Primary and Secondary LLM Providers
Simulates primary model (with configurable 200, 429, or >3000ms delay) and secondary backup provider.
"""

import asyncio
from typing import Dict, Any, Optional
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class MockPrimaryLLMProvider:
    def __init__(self, mode: str = "healthy", delay_seconds: float = 0.0):
        self.mode = mode
        self.delay_seconds = delay_seconds
        self.call_count = 0
        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self.handle_completion, methods=["POST"])
            ]
        )

    async def handle_completion(self, request: Request) -> JSONResponse:
        self.call_count += 1
        body = await request.json()

        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        if self.mode == "healthy":
            return JSONResponse({
                "id": "chatcmpl-primary-001",
                "model": "primary-model-gpt4",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Response from PRIMARY model"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}
            }, status_code=200)

        elif self.mode == "429":
            return JSONResponse({
                "error": {"message": "Primary upstream provider quota exceeded", "type": "insufficient_quota"}
            }, status_code=429)

        elif self.mode == "error":
            return JSONResponse({
                "error": {"message": "Primary upstream internal server fault"}
            }, status_code=500)

        return JSONResponse({"error": "Unknown mock state"}, status_code=500)


class MockSecondaryLLMProvider:
    def __init__(self, mode: str = "healthy", delay_seconds: float = 0.0):
        self.mode = mode
        self.delay_seconds = delay_seconds
        self.call_count = 0
        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self.handle_completion, methods=["POST"])
            ]
        )

    async def handle_completion(self, request: Request) -> JSONResponse:
        self.call_count += 1
        body = await request.json()

        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        if self.mode == "healthy":
            return JSONResponse({
                "id": "chatcmpl-backup-999",
                "model": "secondary-backup-claude",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Response from SECONDARY BACKUP model"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150},
                "_gateway_routing": {"served_by": "secondary_backup", "fallback": True}
            }, status_code=200)

        elif self.mode == "error":
            return JSONResponse({
                "error": {"message": "Secondary backup internal crash"}
            }, status_code=500)

        return JSONResponse({"error": "Unknown mock state"}, status_code=500)
