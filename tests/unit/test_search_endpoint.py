from __future__ import annotations

from datetime import date
from types import TracebackType
from typing import Any, cast

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole
from wikipediarag.auth_service import AuthenticationError
from wikipediarag.repository import search_public_chunks
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import SearchFilters, SearchRequest, SearchResponse, SearchResult


class _MappingResult:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingResult:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _FakeConnection:
    async def execute(self, *_args: object, **_kwargs: object) -> _MappingResult:
        return _MappingResult(
            [{"id": "33333333-3333-4333-8333-333333333333", "tenant_id": "11111111-1111-4111-8111-111111111111"}]
        )


class _FakeConnectionContext:
    async def __aenter__(self) -> object:
        return _FakeConnection()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/search",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


def _actor() -> ActorContext:
    return ActorContext(
        user_id="22222222-2222-4222-8222-222222222222",
        platform_role=PlatformRole.user,
        session_id="44444444-4444-4444-8444-444444444444",
        authentication_method=AuthenticationMethod.local,
        request_id="33333333-3333-4333-8333-333333333333",
        trace_id="trace",
    )


async def test_search_endpoint_requires_authenticated_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        raise AuthenticationError("UNAUTHENTICATED", "authentication required")

    monkeypatch.setattr(api_app, "_require_actor", require_actor)

    with pytest.raises(AuthenticationError, match="authentication required"):
        await api_app.search(
            SearchRequest(query="query", knowledge_base_ids=["33333333-3333-4333-8333-333333333333"]),
            _request(),
        )


async def test_search_endpoint_uses_viewer_scope_and_public_result_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    search_calls: list[dict[str, Any]] = []

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    class WorkspaceGrants:
        def __init__(self, _conn: object) -> None:
            pass

        async def list_visible_knowledge_bases(self, **_kwargs: Any) -> list[tuple[Any, str, bool, bool]]:
            return [
                (
                    cast(Any, type("Resource", (), {"resource_id": "33333333-3333-4333-8333-333333333333"})()),
                    "full",
                    False,
                    False,
                )
            ]

    async def run_search(_conn: object, search_payload: SearchRequest, **kwargs: Any) -> SearchResponse:
        search_calls.append(
            {
                **kwargs,
                "filters": search_payload.filters.model_dump(mode="json", exclude_none=True),
            }
        )

        return SearchResponse(
            results=[
                SearchResult(
                    chunk_id="chunk:1",
                    document_id="doc:1",
                    document_version_id="docv:1",
                    knowledge_base_id="33333333-3333-4333-8333-333333333333",
                    title="Report",
                    snippet="A long report about verification marker and public search.",
                    section_path=["Intro"],
                    source_url="http://localhost/doc",
                    source_type="upload",
                    document_type="application/pdf",
                    language="ru",
                    document_date=date(2026, 7, 29),
                    locator={"page": 1},
                    score=7.0,
                    ranks={"search": 1},
                )
            ],
            limit=search_payload.limit,
            offset=search_payload.offset,
            has_more=False,
        )

    async def resolve_profile(_conn: object, **_kwargs: Any) -> Any:
        return get_retrieval_profile("test_mock")

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "WorkspaceGrantRepository", WorkspaceGrants)
    monkeypatch.setattr(api_app, "run_public_search", run_search)
    monkeypatch.setattr(api_app, "resolve_retrieval_profile", resolve_profile)

    payload = SearchRequest(
        query="verification marker",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        filters=SearchFilters(language="ru", document_type="pdf"),
    )
    response = await api_app.search(payload, _request())

    assert search_calls[0]["actor_user_id"] == "22222222-2222-4222-8222-222222222222"
    assert search_calls[0]["knowledge_base_ids"] == ["33333333-3333-4333-8333-333333333333"]
    assert search_calls[0]["filters"] == {"document_type": "pdf", "language": "ru"}
    result = response.results[0]
    assert result.chunk_id == "chunk:1"
    assert result.document_id == "doc:1"
    assert result.document_version_id == "docv:1"
    assert result.snippet == "A long report about verification marker and public search."
    assert result.locator == {"page": 1}
    assert result.ranks == {"search": 1}


async def test_search_endpoint_returns_safe_kb_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    class WorkspaceGrants:
        def __init__(self, _conn: object) -> None:
            pass

        async def list_visible_knowledge_bases(self, **_kwargs: Any) -> list[tuple[Any, str, bool, bool]]:
            return []

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "WorkspaceGrantRepository", WorkspaceGrants)

    with pytest.raises(HTTPException) as exc_info:
        await api_app.search(
            SearchRequest(query="query", knowledge_base_ids=["33333333-3333-4333-8333-333333333333"]),
            _request(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "knowledge base not found"


async def test_workspace_retrieval_scope_requires_full_kb_access(monkeypatch: pytest.MonkeyPatch) -> None:
    class WorkspaceGrants:
        def __init__(self, _conn: object) -> None:
            pass

        async def list_visible_knowledge_bases(self, **_kwargs: Any) -> list[tuple[Any, str, bool, bool]]:
            return [(cast(Any, type("Resource", (), {"resource_id": "kb"})()), "partial", False, False)]

    monkeypatch.setattr(api_app, "WorkspaceGrantRepository", WorkspaceGrants)

    with pytest.raises(HTTPException) as exc_info:
        await api_app._require_full_workspace_kb_scope(cast(Any, _FakeConnection()), actor=_actor(), kb_ids=["kb"])

    assert exc_info.value.status_code == 404


def test_search_sql_is_tenant_scoped_and_excludes_deleted_documents() -> None:
    sql = str(search_public_chunks.__code__.co_consts)

    assert "c.tenant_id = :tenant_id" in sql
    assert "c.knowledge_base_id = ANY(CAST(:knowledge_base_ids AS uuid[]))" in sql
    assert "c.publication_status = 'published'" in sql
    assert "d.lifecycle_state = 'active'" in sql
    assert "document_type" in sql
    assert "document_date" in sql
