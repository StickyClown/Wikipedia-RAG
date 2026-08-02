from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/documents/{document_id}", handlers.get_document, methods=["GET"])
router.add_api_route("/api/v1/documents/{document_id}/versions", handlers.get_document_versions, methods=["GET"])
router.add_api_route("/api/v1/documents/{document_id}/access", handlers.patch_document_access, methods=["PATCH"])
router.add_api_route("/api/v1/documents/{document_id}/structure", handlers.get_document_structure, methods=["GET"])
router.add_api_route("/api/v1/documents/{document_id}/context", handlers.get_document_context, methods=["GET"])
router.add_api_route("/api/v1/documents/{document_id}/search", handlers.search_document, methods=["POST"])
router.add_api_route("/api/v1/documents/{document_id}", handlers.delete_document, methods=["DELETE"], status_code=202)
router.add_api_route("/api/v1/documents/{document_id}:reprocess", handlers.reprocess_document, methods=["POST"])
