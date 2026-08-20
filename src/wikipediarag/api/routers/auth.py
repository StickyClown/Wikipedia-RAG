from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary, ContractRouter, CrossTenantBehavior, contract

router = ContractRouter()

router.add_contract_route(
    "/api/v1/auth/local/login",
    handlers.local_login,
    methods=["POST"],
    authorization=contract(
        AuthorizationBoundary.public, "anonymous", "create", "local_login", CrossTenantBehavior.not_applicable
    ),
)
router.add_contract_route(
    "/api/v1/auth/oidc/start",
    handlers.oidc_start,
    methods=["POST"],
    authorization=contract(
        AuthorizationBoundary.public, "anonymous", "create", "oidc_start", CrossTenantBehavior.not_applicable
    ),
)
router.add_contract_route(
    "/api/v1/auth/oidc/callback",
    handlers.oidc_callback,
    methods=["GET"],
    authorization=contract(
        AuthorizationBoundary.public, "external_identity", "read", "oidc_callback", CrossTenantBehavior.not_applicable
    ),
)
router.add_contract_route(
    "/api/v1/auth/local/password",
    handlers.change_password,
    methods=["POST"],
    authorization=contract(
        AuthorizationBoundary.session, "authenticated", "update", "password", CrossTenantBehavior.not_applicable
    ),
)
router.add_contract_route(
    "/api/v1/auth/logout",
    handlers.logout,
    methods=["POST"],
    authorization=contract(
        AuthorizationBoundary.session, "authenticated", "delete", "logout", CrossTenantBehavior.not_applicable
    ),
)
router.add_contract_route(
    "/api/v1/auth/session",
    handlers.get_session,
    methods=["GET"],
    authorization=contract(
        AuthorizationBoundary.session, "anonymous_or_session", "read", "session", CrossTenantBehavior.not_applicable
    ),
)
