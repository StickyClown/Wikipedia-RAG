from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X
from wikipediarag.api.route_contracts import ExposureSurface as E
from wikipediarag.debug_search_service import run_debug_search

router = ContractRouter()

router.add_contract_route(
    "/api/v1/search",
    handlers.search,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_viewer", "read", "search", X.deny, exposure=(E.retrieval,)),
)
router.add_contract_route(
    "/api/v1/search:debug",
    run_debug_search,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_editor", "read", "debug_search", X.deny, exposure=(E.retrieval,)),
)
