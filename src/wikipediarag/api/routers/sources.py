from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/knowledge-bases/{kb_id}/sources", handlers.list_sources, methods=["GET"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/sources", handlers.create_source, methods=["POST"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/sources/{source_id}", handlers.get_source, methods=["GET"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/sources/{source_id}", handlers.patch_source, methods=["PATCH"])
router.add_api_route(
    "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/access",
    handlers.patch_source_access,
    methods=["PATCH"],
)
router.add_api_route(
    "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}:healthcheck",
    handlers.healthcheck_source,
    methods=["POST"],
)
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/sources/{source_id}:sync", handlers.sync_source, methods=["POST"])
router.add_api_route("/api/v1/source-sync-runs/{run_id}", handlers.get_source_sync_run, methods=["GET"])
