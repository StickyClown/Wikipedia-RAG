from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/admin/users", handlers.admin_list_users, methods=["GET"])
router.add_api_route("/api/v1/admin/users", handlers.admin_create_user, methods=["POST"])
router.add_api_route("/api/v1/admin/users/{user_id}", handlers.admin_patch_user, methods=["PATCH"])
router.add_api_route("/api/v1/admin/tenants", handlers.admin_list_tenants, methods=["GET"])
router.add_api_route("/api/v1/admin/tenants", handlers.admin_create_tenant, methods=["POST"])
router.add_api_route("/api/v1/admin/tenants/{tenant_id}", handlers.admin_patch_tenant, methods=["PATCH"])
