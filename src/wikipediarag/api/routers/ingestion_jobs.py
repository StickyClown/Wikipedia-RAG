from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X

router = ContractRouter()

router.add_contract_route(
    "/api/v1/wikipedia/imports",
    handlers.create_wikipedia_import,
    methods=["POST"],
    authorization=contract(B.active_tenant, "kb_editor", "create", "wikipedia_import", X.actor_scoped),
)
router.add_contract_route(
    "/api/v1/wikipedia/zim-imports",
    handlers.create_zim_import,
    methods=["POST"],
    authorization=contract(B.active_tenant, "kb_editor", "create", "zim_import", X.actor_scoped),
)
router.add_contract_route(
    "/api/v1/ingestion-jobs/{job_id}",
    handlers.get_ingestion_job,
    methods=["GET"],
    authorization=contract(B.resource, "kb_editor", "read", "ingestion_job", X.deny),
)
router.add_contract_route(
    "/api/v1/ingestion-jobs/{job_id}/events",
    handlers.ingestion_job_events,
    methods=["GET"],
    authorization=contract(B.resource, "kb_editor", "read", "ingestion_job_events", X.deny),
)
router.add_contract_route(
    "/api/v1/ingestion-jobs/{job_id}:cancel",
    handlers.cancel_ingestion_job,
    methods=["POST"],
    authorization=contract(B.resource, "kb_editor", "update", "ingestion_job", X.deny),
)
router.add_contract_route(
    "/api/v1/ingestion-jobs/{job_id}:resume",
    handlers.resume_ingestion_job,
    methods=["POST"],
    authorization=contract(B.resource, "kb_editor", "update", "ingestion_job", X.deny),
)
