from __future__ import annotations

from datetime import date
from types import TracebackType
from typing import Any, cast

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, KnowledgeBaseRole, PlatformRole, TenantRole
from wikipediarag.auth_service import AuthenticationError
from wikipediarag.document_access import DocumentAccessScope
from wikipediarag.repository import search_public_chunks
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import SearchFilters, SearchRequest, SearchResponse, SearchResult


class _FakeConnectionContext:
    async def __aenter__(self) -> object:
        return object()

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
        active_tenant_id="11111111-1111-4111-8111-111111111111",
        tenant_role=TenantRole.member,
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
    loaded_roles: list[str] = []
    search_calls: list[dict[str, Any]] = []

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def load_kb_role(_conn: object, **kwargs: Any) -> KnowledgeBaseRole:
        loaded_roles.append(str(kwargs["kb_id"]))
        return KnowledgeBaseRole.viewer

    async def load_access_scope(_conn: object, **kwargs: Any) -> DocumentAccessScope:
        return DocumentAccessScope(
            tenant_id=kwargs["tenant_id"],
            user_id="22222222-2222-4222-8222-222222222222",
            kb_role=kwargs["effective_kb_role"],
        )

    async def get_kb(_conn: object, _tenant_id: str, kb_id: str) -> dict[str, str]:
        return {"id": kb_id, "active_index": f"read-{kb_id}"}

    async def load_index(_conn: object, **kwargs: Any) -> dict[str, str]:
        return {"id": "index", "read_alias": kwargs["read_alias"]}

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
    monkeypatch.setattr(api_app, "_load_kb_role_optional", load_kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", load_access_scope)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "load_index_version_by_read_alias", load_index)
    monkeypatch.setattr(api_app, "run_public_search", run_search)
    monkeypatch.setattr(api_app, "resolve_retrieval_profile", resolve_profile)

    payload = SearchRequest(
        query="verification marker",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        filters=SearchFilters(language="ru", document_type="pdf"),
    )
    response = await api_app.search(payload, _request())

    assert loaded_roles == ["33333333-3333-4333-8333-333333333333"]
    assert search_calls[0]["document_access_scopes"] == {
        "33333333-3333-4333-8333-333333333333": DocumentAccessScope(
            tenant_id="11111111-1111-4111-8111-111111111111",
            user_id="22222222-2222-4222-8222-222222222222",
            kb_role=KnowledgeBaseRole.viewer,
        )
    }
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

    async def load_kb_role(*_args: object, **_kwargs: object) -> None:
        return None

    async def get_kb(_conn: object, _tenant_id: str, kb_id: str) -> dict[str, str]:
        return {"id": kb_id, "active_index": ""}

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", load_kb_role)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)

    with pytest.raises(HTTPException) as exc_info:
        await api_app.search(
            SearchRequest(query="query", knowledge_base_ids=["33333333-3333-4333-8333-333333333333"]),
            _request(),
        )

    assert exc_info.value.status_code == 409
    detail = cast(dict[str, Any], exc_info.value.detail)
    error = cast(dict[str, Any], detail["error"])
    assert error["code"] == "KB_NOT_READY"


def test_search_sql_is_tenant_scoped_and_excludes_deleted_documents() -> None:
    sql = str(search_public_chunks.__code__.co_consts)

    assert "c.tenant_id = :tenant_id" in sql
    assert "c.knowledge_base_id = ANY(CAST(:knowledge_base_ids AS uuid[]))" in sql
    assert "c.publication_status = 'published'" in sql
    assert "d.lifecycle_state = 'active'" in sql
    assert "document_type" in sql
    assert "document_date" in sql
