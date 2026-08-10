from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/research-plans", handlers.create_research_plan_endpoint, methods=["POST"])
router.add_api_route("/api/v1/research-plans", handlers.list_research_plans_endpoint, methods=["GET"])
router.add_api_route("/api/v1/research-plans/{research_plan_id}", handlers.get_research_plan_endpoint, methods=["GET"])
router.add_api_route(
    "/api/v1/research-plans/{research_plan_id}",
    handlers.patch_research_plan_endpoint,
    methods=["PATCH"],
)
router.add_api_route(
    "/api/v1/research-plans/{research_plan_id}:approve",
    handlers.approve_research_plan_endpoint,
    methods=["POST"],
)
