from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X
from wikipediarag.api.route_contracts import ExposureSurface as E

router = ContractRouter()

router.add_contract_route(
    "/api/v1/research-runs",
    handlers.create_research_run_endpoint,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_viewer", "create", "research_run_collection", X.deny),
)
router.add_contract_route(
    "/api/v1/research-runs",
    handlers.list_research_runs_endpoint,
    methods=["GET"],
    authorization=contract(B.active_tenant, "tenant_member", "read", "research_run_collection", X.actor_scoped),
)
router.add_contract_route(
    "/api/v1/research-runs/{research_run_id}",
    handlers.get_research_run_endpoint,
    methods=["GET"],
    authorization=contract(B.resource, "kb_viewer", "read", "research_run", X.deny, exposure=(E.research,)),
)
router.add_contract_route(
    "/api/v1/research-runs/{research_run_id}/events",
    handlers.research_run_events,
    methods=["GET"],
    authorization=contract(B.resource, "kb_viewer", "read", "research_run_events", X.deny, exposure=(E.research,)),
)
router.add_contract_route(
    "/api/v1/research-runs/{research_run_id}:pause",
    handlers.pause_research_run,
    methods=["POST"],
    authorization=contract(B.resource, "kb_viewer", "update", "research_run", X.deny),
)
router.add_contract_route(
    "/api/v1/research-runs/{research_run_id}:resume",
    handlers.resume_research_run,
    methods=["POST"],
    authorization=contract(B.resource, "kb_viewer", "update", "research_run", X.deny),
)
router.add_contract_route(
    "/api/v1/research-runs/{research_run_id}:cancel",
    handlers.cancel_research_run,
    methods=["POST"],
    authorization=contract(B.resource, "kb_viewer", "update", "research_run", X.deny),
)
