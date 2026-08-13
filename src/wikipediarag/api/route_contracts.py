"""Executable authorization metadata for public HTTP routes.

The metadata is intentionally attached while a route is registered.  It is used
by unit inventory checks, OpenAPI audit output, and the operational HTTP
authorization harness.  FastAPI cannot infer resource ownership semantics from
a handler signature, so new public routes must make that boundary explicit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from fastapi import APIRouter

OPENAPI_AUTHORIZATION_EXTENSION = "x-wikipediarag-authorization"


class AuthorizationBoundary(StrEnum):
    public = "public"
    session = "session"
    platform = "platform"
    active_tenant = "active_tenant"
    knowledge_base = "knowledge_base"
    resource = "resource"


class CrossTenantBehavior(StrEnum):
    deny = "deny"
    actor_scoped = "actor_scoped"
    not_applicable = "not_applicable"


class ExposureSurface(StrEnum):
    document = "document"
    retrieval = "retrieval"
    research = "research"


@dataclass(frozen=True, slots=True)
class RouteAuthorizationContract:
    """Authorization contract that an external HTTP probe can execute."""

    boundary: AuthorizationBoundary
    capability: str
    operation: str
    scenario: str
    cross_tenant: CrossTenantBehavior
    exposure: tuple[ExposureSurface, ...] = ()

    def openapi_value(self) -> dict[str, object]:
        return {
            "boundary": self.boundary.value,
            "capability": self.capability,
            "operation": self.operation,
            "scenario": self.scenario,
            "cross_tenant": self.cross_tenant.value,
            "exposure": [item.value for item in self.exposure],
        }


def contract(
    boundary: AuthorizationBoundary,
    capability: str,
    operation: str,
    scenario: str,
    cross_tenant: CrossTenantBehavior,
    *,
    exposure: tuple[ExposureSurface, ...] = (),
) -> RouteAuthorizationContract:
    """Keep router registrations concise while preserving typed metadata."""
    return RouteAuthorizationContract(
        boundary=boundary,
        capability=capability,
        operation=operation,
        scenario=scenario,
        cross_tenant=cross_tenant,
        exposure=exposure,
    )


class ContractRouter(APIRouter):
    """An API router that refuses public routes without authorization metadata."""

    def add_contract_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        authorization: RouteAuthorizationContract,
        **kwargs: Any,
    ) -> None:
        if not isinstance(authorization, RouteAuthorizationContract):
            raise TypeError("public API routes require a RouteAuthorizationContract")
        openapi_extra = dict(kwargs.pop("openapi_extra", {}) or {})
        if OPENAPI_AUTHORIZATION_EXTENSION in openapi_extra:
            raise ValueError("authorization OpenAPI metadata is owned by ContractRouter")
        openapi_extra[OPENAPI_AUTHORIZATION_EXTENSION] = authorization.openapi_value()
        super().add_api_route(path, endpoint, openapi_extra=openapi_extra, **kwargs)


def route_contract_from_openapi(route: Any) -> RouteAuthorizationContract | None:
    """Rehydrate a route contract from FastAPI route metadata for consumers."""
    raw = dict(getattr(route, "openapi_extra", {}) or {}).get(OPENAPI_AUTHORIZATION_EXTENSION)
    if not isinstance(raw, dict):
        return None
    try:
        exposure = tuple(ExposureSurface(str(item)) for item in raw.get("exposure", []))
        return RouteAuthorizationContract(
            boundary=AuthorizationBoundary(str(raw["boundary"])),
            capability=str(raw["capability"]),
            operation=str(raw["operation"]),
            scenario=str(raw["scenario"]),
            cross_tenant=CrossTenantBehavior(str(raw["cross_tenant"])),
            exposure=exposure,
        )
    except (KeyError, TypeError, ValueError):
        return None


def attach_route_contracts(router: APIRouter, contracts: dict[str, RouteAuthorizationContract]) -> None:
    """Attach contracts to decorator-registered routes and fail on drift.

    ``model_control`` predates the regular routers and uses FastAPI decorators
    because its handlers live in the router module.  This adapter keeps it on
    the same executable contract without duplicating an inventory elsewhere.
    """
    remaining = dict(contracts)
    for base_route in router.routes:
        route = cast(Any, base_route)
        endpoint = getattr(route, "endpoint", None)
        name = getattr(endpoint, "__name__", "")
        item = remaining.pop(name, None)
        if item is None:
            raise RuntimeError(f"public route {name or route} has no authorization contract")
        openapi_extra = dict(getattr(route, "openapi_extra", {}) or {})
        openapi_extra[OPENAPI_AUTHORIZATION_EXTENSION] = item.openapi_value()
        route.openapi_extra = openapi_extra
    if remaining:
        raise RuntimeError(f"authorization contracts reference unregistered routes: {sorted(remaining)}")
