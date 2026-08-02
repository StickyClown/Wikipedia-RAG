from __future__ import annotations

from typing import Any

from fastapi import Request

from wikipediarag.api.handlers import run_debug_search as _run_debug_search
from wikipediarag.schemas import DebugSearchRequest


async def run_debug_search(payload: DebugSearchRequest, request: Request) -> dict[str, Any]:
    """Run editor-only retrieval diagnostics and persist the query-run event trail."""
    return await _run_debug_search(payload, request)
