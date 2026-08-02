from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/knowledge-bases/{kb_id}/documents", handlers.upload_document_multipart, methods=["POST"])
router.add_api_route("/api/v1/uploads/sessions", handlers.create_upload_session_endpoint, methods=["POST"])
router.add_api_route("/api/v1/uploads/batches", handlers.create_upload_batch_endpoint, methods=["POST"])
router.add_api_route("/api/v1/uploads/batches/{batch_id}", handlers.get_upload_batch_endpoint, methods=["GET"])
router.add_api_route(
    "/api/v1/uploads/sessions/{upload_session_id}:complete",
    handlers.complete_upload_session_endpoint,
    methods=["POST"],
)
