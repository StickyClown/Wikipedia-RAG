from __future__ import annotations

from fastapi import APIRouter

from wikipediarag.api import handlers

router = APIRouter()

router.add_api_route("/api/v1/groups", handlers.list_groups, methods=["GET"])
router.add_api_route("/api/v1/groups", handlers.create_group, methods=["POST"])
router.add_api_route("/api/v1/groups/{group_id}", handlers.patch_group, methods=["PATCH"])
router.add_api_route("/api/v1/groups/{group_id}", handlers.delete_group, methods=["DELETE"])
router.add_api_route("/api/v1/knowledge-bases", handlers.get_knowledge_bases, methods=["GET"])
router.add_api_route("/api/v1/knowledge-bases", handlers.create_knowledge_base, methods=["POST"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/access-groups", handlers.list_access_groups, methods=["GET"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}", handlers.get_knowledge_base_endpoint, methods=["GET"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}", handlers.patch_knowledge_base, methods=["PATCH"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}", handlers.delete_knowledge_base, methods=["DELETE"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/grants", handlers.list_kb_grants, methods=["GET"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/grants", handlers.create_kb_grant, methods=["POST"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/grants/{grant_id}", handlers.patch_kb_grant, methods=["PATCH"])
router.add_api_route("/api/v1/knowledge-bases/{kb_id}/grants/{grant_id}", handlers.delete_kb_grant, methods=["DELETE"])
