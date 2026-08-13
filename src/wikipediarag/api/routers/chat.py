from __future__ import annotations

from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X
from wikipediarag.api.route_contracts import ExposureSurface as E
from wikipediarag.chat_service import stream_chat_response

router = ContractRouter()

router.add_contract_route(
    "/api/v1/chat",
    stream_chat_response,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_viewer", "read", "chat", X.deny, exposure=(E.retrieval,)),
)
