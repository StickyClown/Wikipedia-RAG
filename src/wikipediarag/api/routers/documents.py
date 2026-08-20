from __future__ import annotations

from collections.abc import Callable
from typing import Any

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X
from wikipediarag.api.route_contracts import ExposureSurface as E

router = ContractRouter()


def _document(
    path: str,
    endpoint: Callable[..., Any],
    methods: list[str],
    operation: str,
    scenario: str,
    *,
    exposure: tuple[E, ...] = (),
) -> None:
    router.add_contract_route(
        path,
        endpoint,
        methods=methods,
        authorization=contract(B.resource, "resource_read", operation, scenario, X.deny, exposure=exposure),
    )


_document("/api/v1/documents/{document_id}", handlers.get_document, ["GET"], "read", "document", exposure=(E.document,))
_document(
    "/api/v1/documents/{document_id}/versions",
    handlers.get_document_versions,
    ["GET"],
    "read",
    "document_versions",
    exposure=(E.document,),
)
_document(
    "/api/v1/documents/{document_id}/access-grants",
    handlers.list_document_access_grants,
    ["GET"],
    "read",
    "document_access_grants",
)
_document(
    "/api/v1/documents/{document_id}/access-grants",
    handlers.replace_document_access_grants,
    ["PUT"],
    "replace",
    "document_access_grants",
)
_document(
    "/api/v1/documents/{document_id}/structure",
    handlers.get_document_structure,
    ["GET"],
    "read",
    "document_structure",
    exposure=(E.document,),
)
_document(
    "/api/v1/documents/{document_id}/context",
    handlers.get_document_context,
    ["GET"],
    "read",
    "document_context",
    exposure=(E.document,),
)
_document(
    "/api/v1/documents/{document_id}/search",
    handlers.search_document,
    ["POST"],
    "read",
    "document_search",
    exposure=(E.document, E.retrieval),
)
_document("/api/v1/documents/{document_id}", handlers.delete_document, ["DELETE"], "delete", "document")
_document("/api/v1/documents/{document_id}:reprocess", handlers.reprocess_document, ["POST"], "update", "document")
