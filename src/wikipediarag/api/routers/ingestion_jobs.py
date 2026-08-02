from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/wikipedia/imports", handlers.create_wikipedia_import, methods=["POST"])
router.add_api_route("/api/v1/wikipedia/zim-imports", handlers.create_zim_import, methods=["POST"])
router.add_api_route("/api/v1/ingestion-jobs/{job_id}", handlers.get_ingestion_job, methods=["GET"])
router.add_api_route("/api/v1/ingestion-jobs/{job_id}/events", handlers.ingestion_job_events, methods=["GET"])
router.add_api_route("/api/v1/ingestion-jobs/{job_id}:cancel", handlers.cancel_ingestion_job, methods=["POST"])
router.add_api_route("/api/v1/ingestion-jobs/{job_id}:resume", handlers.resume_ingestion_job, methods=["POST"])
