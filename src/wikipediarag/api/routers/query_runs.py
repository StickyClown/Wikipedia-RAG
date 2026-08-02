from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/query-runs/{query_run_id}/retrieval", handlers.query_run_retrieval, methods=["GET"])
router.add_api_route("/api/v1/query-runs/{query_run_id}/feedback", handlers.query_run_feedback, methods=["POST"])
router.add_api_route("/api/v1/query-runs/{query_run_id}/evaluation", handlers.query_run_evaluation, methods=["POST"])
