from __future__ import annotations

import httpx

from wikipediarag.operational_authorization import (
    exposure_route_contracts,
    public_route_contracts,
    revocation_probe,
    route_requires_cross_tenant_replay,
    safe_contract_report,
    tenant_denial_probe,
)


def _response(status_code: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("GET", "http://api.test/resource"), json=payload)


def test_registered_contracts_cover_every_public_route_and_report_no_payloads() -> None:
    routes = public_route_contracts()
    report = safe_contract_report(routes)

    assert len(routes) == len(report)
    assert any(route_requires_cross_tenant_replay(item) for item in routes)
    assert all("endpoint" in item and "scenario" in item for item in report)
    assert all("payload" not in item and "secret" not in item for item in report)


def test_exposure_contracts_include_document_retrieval_and_research_surfaces() -> None:
    surfaces = {surface.value for item in exposure_route_contracts() for surface in item.contract.exposure}

    assert surfaces == {"document", "retrieval", "research"}


def test_tenant_denial_requires_a_safe_not_found_or_forbidden_response() -> None:
    passed = tenant_denial_probe(_response(404, {"error": {"code": "NOT_FOUND"}}), forbidden_values=["tenant-a"])
    leaked = tenant_denial_probe(
        _response(404, {"error": {"details": {"object_key": "tenant-a"}}}), forbidden_values=["tenant-a"]
    )

    assert passed["passed"] is True
    assert leaked["passed"] is False
    assert leaked["leak_detected"] is True


def test_revocation_probe_accepts_a_safe_denial_but_rejects_marker_leakage() -> None:
    hidden = revocation_probe(_response(404, {"detail": "document not found"}), forbidden_values=["marker"])
    leaked = revocation_probe(_response(200, {"answer": "marker"}), forbidden_values=["marker"])

    assert hidden["passed"] is True
    assert leaked["passed"] is False
