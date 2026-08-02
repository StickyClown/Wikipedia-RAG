from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.chat_service import stream_chat_response

router = APIRouter()

router.add_api_route("/api/v1/chat", stream_chat_response, methods=["POST"])
