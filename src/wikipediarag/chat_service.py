from __future__ import annotations

from fastapi import Request
from fastapi.responses import StreamingResponse

from wikipediarag.api.handlers import stream_chat_response as _stream_chat_response
from wikipediarag.schemas import ChatRequest


async def stream_chat_response(payload: ChatRequest, request: Request) -> StreamingResponse:
    """Run chat retrieval, answer generation, diagnostics persistence, and SSE streaming."""
    return await _stream_chat_response(payload, request)
