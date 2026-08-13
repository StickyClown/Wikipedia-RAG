from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X

router = ContractRouter()

router.add_contract_route(
    "/api/v1/research-plans",
    handlers.create_research_plan_endpoint,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_viewer", "create", "research_plan_collection", X.deny),
)
router.add_contract_route(
    "/api/v1/research-plans",
    handlers.list_research_plans_endpoint,
    methods=["GET"],
    authorization=contract(B.active_tenant, "tenant_member", "read", "research_plan_collection", X.actor_scoped),
)
router.add_contract_route(
    "/api/v1/research-plans/{research_plan_id}",
    handlers.get_research_plan_endpoint,
    methods=["GET"],
    authorization=contract(B.resource, "kb_viewer", "read", "research_plan", X.deny),
)
router.add_contract_route(
    "/api/v1/research-plans/{research_plan_id}",
    handlers.patch_research_plan_endpoint,
    methods=["PATCH"],
    authorization=contract(B.resource, "kb_viewer", "update", "research_plan", X.deny),
)
router.add_contract_route(
    "/api/v1/research-plans/{research_plan_id}:approve",
    handlers.approve_research_plan_endpoint,
    methods=["POST"],
    authorization=contract(B.resource, "kb_viewer", "update", "research_plan", X.deny),
)
