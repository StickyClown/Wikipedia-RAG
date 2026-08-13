from wikipediarag.api.app import ROUTERS, create_app
from wikipediarag.api.route_contracts import (
    OPENAPI_AUTHORIZATION_EXTENSION,
    CrossTenantBehavior,
    ExposureSurface,
    route_contract_from_openapi,
)


def _public_routes() -> list[object]:
    return [route for router in ROUTERS for route in router.routes]


def test_every_public_http_route_has_a_typed_authorization_contract() -> None:
    routes = _public_routes()
    contracts = [route_contract_from_openapi(route) for route in routes]

    assert routes
    assert all(item is not None for item in contracts)
    assert all(item.scenario for item in contracts if item is not None)
    assert all(item.capability for item in contracts if item is not None)


def test_authorization_contracts_are_exported_in_openapi() -> None:
    schema = create_app().openapi()
    exported = [
        operation[OPENAPI_AUTHORIZATION_EXTENSION]
        for path, path_item in schema["paths"].items()
        if path.startswith("/api/") or path in {"/health", "/ready"}
        for operation in path_item.values()
        if isinstance(operation, dict) and OPENAPI_AUTHORIZATION_EXTENSION in operation
    ]

    assert len(exported) == len(_public_routes())
    assert {item["cross_tenant"] for item in exported} >= {
        CrossTenantBehavior.deny.value,
        CrossTenantBehavior.actor_scoped.value,
        CrossTenantBehavior.not_applicable.value,
    }


def test_document_retrieval_and_research_exposure_routes_are_explicit() -> None:
    exposure = {
        item.scenario: set(item.exposure)
        for item in (route_contract_from_openapi(route) for route in _public_routes())
        if item is not None and item.exposure
    }

    assert ExposureSurface.document in exposure["document"]
    assert ExposureSurface.retrieval in exposure["search"]
    assert ExposureSurface.research in exposure["research_run"]
