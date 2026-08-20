from __future__ import annotations

from collections.abc import Callable
from typing import Any

from wikipediarag.api import handlers
from wikipediarag.api.route_contracts import AuthorizationBoundary as B
from wikipediarag.api.route_contracts import ContractRouter, contract
from wikipediarag.api.route_contracts import CrossTenantBehavior as X

router = ContractRouter()


def _source(
    path: str,
    endpoint: Callable[..., Any],
    methods: list[str],
    operation: str,
    scenario: str,
    *,
    boundary: B = B.resource,
) -> None:
    router.add_contract_route(
        path,
        endpoint,
        methods=methods,
        authorization=contract(
            boundary, "resource_read" if operation == "read" else "resource_write", operation, scenario, X.deny
        ),
    )


_source(
    "/api/v1/knowledge-bases/{kb_id}/sources",
    handlers.list_sources,
    ["GET"],
    "read",
    "source_collection",
    boundary=B.knowledge_base,
)
_source(
    "/api/v1/knowledge-bases/{kb_id}/sources",
    handlers.create_source,
    ["POST"],
    "create",
    "source_collection",
    boundary=B.knowledge_base,
)
_source("/api/v1/knowledge-bases/{kb_id}/sources/{source_id}", handlers.get_source, ["GET"], "read", "source")
_source("/api/v1/knowledge-bases/{kb_id}/sources/{source_id}", handlers.patch_source, ["PATCH"], "update", "source")
_source(
    "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}:healthcheck",
    handlers.healthcheck_source,
    ["POST"],
    "execute",
    "source",
)
_source("/api/v1/knowledge-bases/{kb_id}/sources/{source_id}:sync", handlers.sync_source, ["POST"], "execute", "source")
_source("/api/v1/source-sync-runs/{run_id}", handlers.get_source_sync_run, ["GET"], "read", "source_sync_run")
