from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers
from wikipediarag.debug_search_service import run_debug_search

router = APIRouter()

router.add_api_route("/api/v1/search", handlers.search, methods=["POST"])
router.add_api_route("/api/v1/search:debug", run_debug_search, methods=["POST"])
