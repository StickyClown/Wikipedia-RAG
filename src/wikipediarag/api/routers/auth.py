from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/auth/local/login", handlers.local_login, methods=["POST"])
router.add_api_route("/api/v1/auth/oidc/start", handlers.oidc_start, methods=["POST"])
router.add_api_route("/api/v1/auth/oidc/callback", handlers.oidc_callback, methods=["GET"])
router.add_api_route("/api/v1/auth/local/password", handlers.change_password, methods=["POST"])
router.add_api_route("/api/v1/auth/logout", handlers.logout, methods=["POST"])
router.add_api_route("/api/v1/auth/session", handlers.get_session, methods=["GET"])
router.add_api_route("/api/v1/auth/session/tenant", handlers.select_session_tenant, methods=["POST"])
