from __future__ import annotations

from collections.abc import Callable
from typing import Any

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X

router = ContractRouter()


def _tenant(
    path: str,
    endpoint: Callable[..., Any],
    methods: list[str],
    operation: str,
    scenario: str,
    *,
    boundary: B = B.active_tenant,
    cross: X = X.deny,
) -> None:
    router.add_contract_route(
        path,
        endpoint,
        methods=methods,
        authorization=contract(
            boundary, "tenant_member" if boundary == B.active_tenant else "kb_manager", operation, scenario, cross
        ),
    )


def _workspace_admin(
    path: str, endpoint: Callable[..., Any], methods: list[str], operation: str, scenario: str
) -> None:
    router.add_contract_route(
        path,
        endpoint,
        methods=methods,
        authorization=contract(B.platform, "platform_admin", operation, scenario, X.actor_scoped),
    )


_workspace_admin("/api/v1/groups", handlers.list_groups, ["GET"], "read", "group_collection")
_workspace_admin("/api/v1/groups", handlers.create_group, ["POST"], "create", "group_collection")
_workspace_admin("/api/v1/groups/{group_id}", handlers.patch_group, ["PATCH"], "update", "group")
_workspace_admin("/api/v1/groups/{group_id}", handlers.delete_group, ["DELETE"], "delete", "group")
router.add_contract_route(
    "/api/v1/knowledge-bases",
    handlers.get_knowledge_bases,
    methods=["GET"],
    authorization=contract(B.session, "authenticated", "read", "knowledge_base_collection", X.actor_scoped),
)
_tenant(
    "/api/v1/retrieval-profiles",
    handlers.retrieval_profiles,
    ["GET"],
    "read",
    "retrieval_profile_collection",
    boundary=B.knowledge_base,
)
router.add_contract_route(
    "/api/v1/knowledge-bases",
    handlers.create_knowledge_base,
    methods=["POST"],
    authorization=contract(B.session, "authenticated", "create", "knowledge_base_collection", X.actor_scoped),
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}/access-groups",
    handlers.list_access_groups,
    ["GET"],
    "read",
    "knowledge_base",
    boundary=B.knowledge_base,
)
router.add_contract_route(
    "/api/v1/knowledge-bases/{kb_id}",
    handlers.get_knowledge_base_endpoint,
    methods=["GET"],
    authorization=contract(B.resource, "resource_read", "read", "knowledge_base", X.deny),
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}",
    handlers.patch_knowledge_base,
    ["PATCH"],
    "update",
    "knowledge_base",
    boundary=B.knowledge_base,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}",
    handlers.delete_knowledge_base,
    ["DELETE"],
    "delete",
    "knowledge_base",
    boundary=B.knowledge_base,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}/access-grants",
    handlers.list_knowledge_base_access_grants,
    ["GET"],
    "read",
    "knowledge_base_access_grants",
    boundary=B.resource,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}/access-grants",
    handlers.replace_knowledge_base_access_grants,
    ["PUT"],
    "replace",
    "knowledge_base_access_grants",
    boundary=B.resource,
)
