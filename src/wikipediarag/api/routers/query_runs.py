from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X
from wikipediarag.api.route_contracts import ExposureSurface as E

router = ContractRouter()

router.add_contract_route(
    "/api/v1/query-runs/{query_run_id}/retrieval",
    handlers.query_run_retrieval,
    methods=["GET"],
    authorization=contract(B.resource, "kb_editor", "read", "query_run", X.deny, exposure=(E.retrieval,)),
)
router.add_contract_route(
    "/api/v1/query-runs/{query_run_id}/feedback",
    handlers.query_run_feedback,
    methods=["POST"],
    authorization=contract(B.resource, "kb_editor", "update", "query_run", X.deny),
)
router.add_contract_route(
    "/api/v1/query-runs/{query_run_id}/evaluation",
    handlers.query_run_evaluation,
    methods=["POST"],
    authorization=contract(B.resource, "kb_editor", "update", "query_run", X.deny),
)
