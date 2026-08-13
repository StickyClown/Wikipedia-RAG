from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary, ContractRouter, CrossTenantBehavior, contract

router = ContractRouter()

router.add_contract_route(
    "/health",
    handlers.health,
    methods=["GET"],
    authorization=contract(
        AuthorizationBoundary.public, "anonymous", "read", "operational_health", CrossTenantBehavior.not_applicable
    ),
)
router.add_contract_route(
    "/ready",
    handlers.ready,
    methods=["GET"],
    authorization=contract(
        AuthorizationBoundary.public, "anonymous", "read", "operational_ready", CrossTenantBehavior.not_applicable
    ),
)
