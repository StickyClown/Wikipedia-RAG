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


_tenant("/api/v1/groups", handlers.list_groups, ["GET"], "read", "group_collection", cross=X.actor_scoped)
_tenant("/api/v1/groups", handlers.create_group, ["POST"], "create", "group_collection", cross=X.actor_scoped)
_tenant("/api/v1/groups/{group_id}", handlers.patch_group, ["PATCH"], "update", "group")
_tenant("/api/v1/groups/{group_id}", handlers.delete_group, ["DELETE"], "delete", "group")
_tenant(
    "/api/v1/knowledge-bases",
    handlers.get_knowledge_bases,
    ["GET"],
    "read",
    "knowledge_base_collection",
    cross=X.actor_scoped,
)
_tenant(
    "/api/v1/retrieval-profiles",
    handlers.retrieval_profiles,
    ["GET"],
    "read",
    "retrieval_profile_collection",
    boundary=B.knowledge_base,
)
_tenant(
    "/api/v1/knowledge-bases",
    handlers.create_knowledge_base,
    ["POST"],
    "create",
    "knowledge_base_collection",
    cross=X.actor_scoped,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}/access-groups",
    handlers.list_access_groups,
    ["GET"],
    "read",
    "knowledge_base",
    boundary=B.knowledge_base,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}",
    handlers.get_knowledge_base_endpoint,
    ["GET"],
    "read",
    "knowledge_base",
    boundary=B.knowledge_base,
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
    "/api/v1/knowledge-bases/{kb_id}/grants",
    handlers.list_kb_grants,
    ["GET"],
    "read",
    "knowledge_base_grant_collection",
    boundary=B.knowledge_base,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}/grants",
    handlers.create_kb_grant,
    ["POST"],
    "create",
    "knowledge_base_grant_collection",
    boundary=B.knowledge_base,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}/grants/{grant_id}",
    handlers.patch_kb_grant,
    ["PATCH"],
    "update",
    "knowledge_base_grant",
    boundary=B.resource,
)
_tenant(
    "/api/v1/knowledge-bases/{kb_id}/grants/{grant_id}",
    handlers.delete_kb_grant,
    ["DELETE"],
    "delete",
    "knowledge_base_grant",
    boundary=B.resource,
)
