from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/health", handlers.health, methods=["GET"])
router.add_api_route("/ready", handlers.ready, methods=["GET"])
