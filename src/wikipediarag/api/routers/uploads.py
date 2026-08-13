from __future__ import annotations

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X

router = ContractRouter()

router.add_contract_route(
    "/api/v1/knowledge-bases/{kb_id}/documents",
    handlers.upload_document_multipart,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_editor", "create", "multipart_upload", X.deny),
)
router.add_contract_route(
    "/api/v1/uploads/sessions",
    handlers.create_upload_session_endpoint,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_editor", "create", "upload_session", X.deny),
)
router.add_contract_route(
    "/api/v1/uploads/batches",
    handlers.create_upload_batch_endpoint,
    methods=["POST"],
    authorization=contract(B.knowledge_base, "kb_editor", "create", "upload_batch", X.deny),
)
router.add_contract_route(
    "/api/v1/uploads/batches/{batch_id}",
    handlers.get_upload_batch_endpoint,
    methods=["GET"],
    authorization=contract(B.resource, "kb_editor", "read", "upload_batch", X.deny),
)
router.add_contract_route(
    "/api/v1/uploads/sessions/{upload_session_id}:complete",
    handlers.complete_upload_session_endpoint,
    methods=["POST"],
    authorization=contract(B.resource, "kb_editor", "update", "upload_session", X.deny),
)
