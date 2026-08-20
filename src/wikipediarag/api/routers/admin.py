from __future__ import annotations

from collections.abc import Callable
from typing import Any

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X

router = ContractRouter()


def _admin(path: str, endpoint: Callable[..., Any], methods: list[str], operation: str, scenario: str) -> None:
    router.add_contract_route(
        path,
        endpoint,
        methods=methods,
        authorization=contract(B.platform, "platform_admin", operation, scenario, X.not_applicable),
    )


_admin("/api/v1/admin/users", handlers.admin_list_users, ["GET"], "read", "admin_user_collection")
_admin("/api/v1/admin/users", handlers.admin_create_user, ["POST"], "create", "admin_user_collection")
_admin("/api/v1/admin/users/{user_id}", handlers.admin_patch_user, ["PATCH"], "update", "admin_user")
