from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/research-runs", handlers.create_research_run_endpoint, methods=["POST"])
router.add_api_route("/api/v1/research-runs", handlers.list_research_runs_endpoint, methods=["GET"])
router.add_api_route("/api/v1/research-runs/{research_run_id}", handlers.get_research_run_endpoint, methods=["GET"])
router.add_api_route("/api/v1/research-runs/{research_run_id}/events", handlers.research_run_events, methods=["GET"])
router.add_api_route("/api/v1/research-runs/{research_run_id}:pause", handlers.pause_research_run, methods=["POST"])
router.add_api_route("/api/v1/research-runs/{research_run_id}:resume", handlers.resume_research_run, methods=["POST"])
router.add_api_route("/api/v1/research-runs/{research_run_id}:cancel", handlers.cancel_research_run, methods=["POST"])
