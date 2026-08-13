"""Shared, content-safe primitives for live authorization operational gates."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from wikipediarag.api.app import ROUTERS
from wikipediarag.api.route_contracts import (
    CrossTenantBehavior,
    ExposureSurface,
    RouteAuthorizationContract,
    route_contract_from_openapi,
)


@dataclass(frozen=True, slots=True)
class PublicRouteContract:
    method: str
    path: str
    endpoint_name: str
    contract: RouteAuthorizationContract


def public_route_contracts() -> list[PublicRouteContract]:
    """Return every registered public HTTP route and fail closed on missing metadata."""
    discovered: list[PublicRouteContract] = []
    for router in ROUTERS:
        for route in router.routes:
            contract = route_contract_from_openapi(route)
            endpoint = getattr(route, "endpoint", None)
            methods = sorted(getattr(route, "methods", ()) or ())
            path = str(getattr(route, "path", ""))
            if contract is None or endpoint is None or not methods:
                raise RuntimeError("PUBLIC_ROUTE_AUTHORIZATION_CONTRACT_MISSING")
            for method in methods:
                discovered.append(
                    PublicRouteContract(
                        method=method,
                        path=path,
                        endpoint_name=str(getattr(endpoint, "__name__", "")),
                        contract=contract,
                    )
                )
    return discovered


def exposure_route_contracts() -> list[PublicRouteContract]:
    """Routes that can return current document-, retrieval-, or research-derived data."""
    return [item for item in public_route_contracts() if item.contract.exposure]


def safe_contract_report(routes: Iterable[PublicRouteContract]) -> list[dict[str, Any]]:
    """Report route names and policy metadata without request payloads or secrets."""
    return [
        {
            "method": item.method,
            "path": item.path,
            "endpoint": item.endpoint_name,
            "boundary": item.contract.boundary.value,
            "capability": item.contract.capability,
            "operation": item.contract.operation,
            "scenario": item.contract.scenario,
            "cross_tenant": item.contract.cross_tenant.value,
            "exposure": [surface.value for surface in item.contract.exposure],
        }
        for item in routes
    ]


def response_contains_forbidden_values(payload: Any, forbidden_values: Iterable[str]) -> bool:
    """Check a response without returning document content to an artifact or log."""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return any(value and value in serialized for value in forbidden_values)


def safe_response_summary(response: httpx.Response) -> dict[str, Any]:
    """Extract only a stable status/error envelope from a potentially sensitive response."""
    body: dict[str, Any] = {}
    try:
        decoded = response.json()
        body = decoded if isinstance(decoded, dict) else {}
    except (json.JSONDecodeError, ValueError):
        pass
    error = body.get("error")
    if isinstance(error, dict):
        return {
            "status_code": response.status_code,
            "error_code": str(error.get("code") or "") or None,
        }
    detail = body.get("detail")
    if isinstance(detail, dict):
        nested = detail.get("error")
        if isinstance(nested, dict):
            return {"status_code": response.status_code, "error_code": str(nested.get("code") or "") or None}
    return {"status_code": response.status_code, "error_code": None}


def tenant_denial_probe(
    response: httpx.Response,
    *,
    forbidden_values: Iterable[str],
) -> dict[str, Any]:
    """Apply the reusable cross-tenant denial invariant."""
    leaked = response_contains_forbidden_values(_response_payload(response), forbidden_values)
    return {
        **safe_response_summary(response),
        "passed": response.status_code in {403, 404} and not leaked,
        "leak_detected": leaked,
    }


def revocation_probe(
    response: httpx.Response,
    *,
    forbidden_values: Iterable[str],
) -> dict[str, Any]:
    """Apply the reusable post-revocation no-content-leak invariant."""
    leaked = response_contains_forbidden_values(_response_payload(response), forbidden_values)
    return {
        **safe_response_summary(response),
        "passed": response.status_code < 500 and not leaked,
        "leak_detected": leaked,
    }


def route_requires_cross_tenant_replay(item: PublicRouteContract) -> bool:
    return item.contract.cross_tenant == CrossTenantBehavior.deny


def route_exposes_document_derived_data(item: PublicRouteContract) -> bool:
    return bool(
        {ExposureSurface.document, ExposureSurface.retrieval, ExposureSurface.research} & set(item.contract.exposure)
    )


def _response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text
