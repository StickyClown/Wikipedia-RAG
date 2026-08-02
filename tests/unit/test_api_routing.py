from __future__ import annotations

from fastapi.routing import APIRoute

from wikipediarag.api_app import app


def test_api_app_includes_domain_router_paths() -> None:
    """Lock the public API paths after splitting endpoint registration by domain."""
    paths: set[tuple[str, str]] = set()
    for route in app.routes:
        routes = getattr(getattr(route, "original_router", None), "routes", [route])
        for nested in routes:
            if isinstance(nested, APIRoute):
                for method in nested.methods or set():
                    paths.add((method, nested.path))

    assert ("GET", "/health") in paths
    assert ("GET", "/ready") in paths
    assert ("POST", "/api/v1/chat") in paths
    assert ("POST", "/api/v1/search") in paths
    assert ("POST", "/api/v1/search:debug") in paths
    assert ("POST", "/api/v1/uploads/batches") in paths
    assert ("GET", "/api/v1/knowledge-bases/{kb_id}/access-groups") in paths
    assert ("PATCH", "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/access") in paths
    assert ("GET", "/api/v1/documents/{document_id}/context") in paths
    assert ("PATCH", "/api/v1/documents/{document_id}/access") in paths
    assert ("POST", "/api/v1/query-runs/{query_run_id}/feedback") in paths
    assert ("POST", "/api/v1/research-runs") in paths
    assert ("GET", "/api/v1/research-runs") in paths
    assert ("GET", "/api/v1/research-runs/{research_run_id}") in paths
    assert ("GET", "/api/v1/research-runs/{research_run_id}/events") in paths
    assert ("POST", "/api/v1/research-runs/{research_run_id}:pause") in paths
    assert ("POST", "/api/v1/research-runs/{research_run_id}:resume") in paths
    assert ("POST", "/api/v1/research-runs/{research_run_id}:cancel") in paths
