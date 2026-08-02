from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, KnowledgeBaseRole, PlatformRole, TenantRole
from wikipediarag.document_access import DocumentAccessScope
from wikipediarag.repository import (
    fetch_document_context_chunks,
    list_document_sections,
    replace_document_sections_from_chunks,
    search_document_chunks,
)
from wikipediarag.schemas import DocumentAccessPatch, DocumentSearchRequest


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


def _request(path: str = "/api/v1/documents/doc:1/structure") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
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


def _document() -> dict[str, Any]:
    return {
        "id": "doc:1",
        "knowledge_base_id": "55555555-5555-4555-8555-555555555555",
        "title": "Report",
        "source_type": "upload_document",
        "metadata": {"source_url": "http://localhost/doc"},
        "current_version_id": "docv:1",
        "public_metadata": {"filename": "report.md"},
    }


def _restricted_document() -> dict[str, Any]:
    document = _document()
    document["metadata"] = {
        "source_url": "http://localhost/doc",
        "document_access": {"policy": "restricted", "user_ids": ["other-user"], "group_ids": []},
    }
    return document


async def _viewer_kb_role(*_args: object, **_kwargs: object) -> KnowledgeBaseRole:
    return KnowledgeBaseRole.viewer


async def _viewer_access_scope(*_args: object, **_kwargs: object) -> DocumentAccessScope:
    return DocumentAccessScope(
        tenant_id="11111111-1111-4111-8111-111111111111",
        user_id="22222222-2222-4222-8222-222222222222",
        kb_role=KnowledgeBaseRole.viewer,
    )


async def test_document_structure_uses_viewer_scope_and_safe_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    required_roles: list[tuple[str, KnowledgeBaseRole]] = []

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(_conn: object, tenant_id: str, document_id: str) -> dict[str, Any] | None:
        assert tenant_id == "11111111-1111-4111-8111-111111111111"
        assert document_id == "doc:1"
        return _document()

    async def kb_role(_conn: object, **kwargs: Any) -> KnowledgeBaseRole:
        required_roles.append((str(kwargs["kb_id"]), KnowledgeBaseRole.viewer))
        return KnowledgeBaseRole.viewer

    async def sections(_conn: object, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["knowledge_base_id"] == "55555555-5555-4555-8555-555555555555"
        return [
            {
                "section_id": "section:1",
                "parent_section_id": None,
                "title": "Report",
                "level": 1,
                "path": ["Report"],
                "ordinal": 1,
                "locator": {"page": 1},
                "first_chunk_id": "chunk:1",
                "last_chunk_id": "chunk:1",
                "metadata": {"source": "chunks"},
            }
        ]

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", _viewer_access_scope)
    monkeypatch.setattr(api_app, "list_document_sections", sections)

    response = await api_app.get_document_structure("doc:1", _request())

    assert required_roles == [("55555555-5555-4555-8555-555555555555", KnowledgeBaseRole.viewer)]
    assert response.document_id == "doc:1"
    assert response.sections[0].section_id == "section:1"
    assert "object_key" not in response.public_metadata


async def test_document_structure_hides_restricted_document_from_unauthorized_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(*_args: object) -> dict[str, Any]:
        return _restricted_document()

    async def access_scope(*_args: object, **_kwargs: object) -> DocumentAccessScope:
        return DocumentAccessScope(
            tenant_id="11111111-1111-4111-8111-111111111111",
            user_id="22222222-2222-4222-8222-222222222222",
        )

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", _viewer_kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", access_scope)

    with pytest.raises(HTTPException) as exc_info:
        await api_app.get_document_structure("doc:1", _request())

    assert exc_info.value.status_code == 404


async def test_document_structure_allows_restricted_document_for_admin_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(*_args: object) -> dict[str, Any]:
        return _restricted_document()

    async def access_scope(*_args: object, **_kwargs: object) -> DocumentAccessScope:
        return DocumentAccessScope(
            bypass=True,
            tenant_id="11111111-1111-4111-8111-111111111111",
            user_id="22222222-2222-4222-8222-222222222222",
            kb_role=KnowledgeBaseRole.manager,
        )

    async def sections(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", _viewer_kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", access_scope)
    monkeypatch.setattr(api_app, "list_document_sections", sections)

    response = await api_app.get_document_structure("doc:1", _request())

    assert response.document_id == "doc:1"


async def test_document_structure_allows_tenant_public_document_without_kb_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    document["metadata"] = {
        "source_url": "http://localhost/doc",
        "document_access": {"policy": "tenant", "user_ids": [], "group_ids": []},
    }

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(*_args: object) -> dict[str, Any]:
        return document

    async def no_kb_role(*_args: object, **_kwargs: object) -> None:
        return None

    async def access_scope(*_args: object, **_kwargs: object) -> DocumentAccessScope:
        return DocumentAccessScope(
            tenant_id="11111111-1111-4111-8111-111111111111",
            user_id="22222222-2222-4222-8222-222222222222",
        )

    async def sections(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", no_kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", access_scope)
    monkeypatch.setattr(api_app, "list_document_sections", sections)

    response = await api_app.get_document_structure("doc:1", _request())

    assert response.document_id == "doc:1"
    assert response.document_access["policy"] == "tenant"


async def test_patch_document_access_requires_manager_and_updates_search_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(*_args: object) -> dict[str, Any]:
        return _document()

    async def require_role(_conn: object, **kwargs: Any) -> KnowledgeBaseRole:
        calls.append(("role", kwargs))
        return KnowledgeBaseRole.manager

    async def update_db(*_args: object, **kwargs: Any) -> None:
        calls.append(("db", kwargs))

    async def get_kb(*_args: object) -> dict[str, str]:
        return {"active_index": "read-kb"}

    async def audit(*_args: object, **kwargs: Any) -> None:
        calls.append(("audit", kwargs))

    def update_os(**kwargs: Any) -> int:
        calls.append(("opensearch", kwargs))
        return 3

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "_require_kb_role", require_role)
    monkeypatch.setattr(api_app, "update_document_access_metadata", update_db)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "_audit", audit)
    monkeypatch.setattr(api_app, "update_document_access", update_os)

    response = await api_app.patch_document_access(
        "doc:1",
        DocumentAccessPatch(policy="restricted", user_ids=["user:1"], group_ids=["group:1"]),
        _request(),
    )

    assert response.document_access == {"policy": "restricted", "user_ids": ["user:1"], "group_ids": ["group:1"]}
    assert calls[0][0] == "role"
    assert calls[1][1]["origin"] == "manual"
    assert calls[-1][0] == "opensearch"
    assert calls[-1][1]["read_alias"] == "read-kb"


async def test_document_context_resolves_section_id_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    context_calls: list[dict[str, Any]] = []

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(*_args: object) -> dict[str, Any]:
        return _document()

    async def sections(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        return [{"section_id": "section:1", "path": ["Report", "Details"]}]

    async def context_chunks(_conn: object, **kwargs: Any) -> list[dict[str, Any]]:
        context_calls.append(kwargs)
        return [
            {
                "chunk_id": "chunk:2",
                "document_id": "doc:1",
                "document_version_id": "docv:1",
                "knowledge_base_id": "55555555-5555-4555-8555-555555555555",
                "title": "Report",
                "section_path": ["Report", "Details"],
                "content": "Deep marker.",
                "source_url": "http://localhost/doc",
                "locator": {"page": 1},
                "prev_chunk_id": "chunk:1",
                "next_chunk_id": None,
                "chunk_ordinal": 2,
            }
        ]

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", _viewer_kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", _viewer_access_scope)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "list_document_sections", sections)
    monkeypatch.setattr(api_app, "fetch_document_context_chunks", context_chunks)

    response = await api_app.get_document_context(
        "doc:1",
        _request(),
        chunk_id=None,
        section_id="section:1",
        before=2,
        after=2,
        limit=80,
        offset=0,
    )

    assert context_calls[0]["section_path"] == ["Report", "Details"]
    assert response.chunks[0].chunk_id == "chunk:2"


async def test_document_search_returns_safe_chunk_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(*_args: object) -> dict[str, Any]:
        return _document()

    async def search_chunks(_conn: object, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": "chunk:1",
                "document_id": "doc:1",
                "document_version_id": "docv:1",
                "knowledge_base_id": "55555555-5555-4555-8555-555555555555",
                "title": "Report",
                "section_path": ["Report"],
                "content": "A report with verification marker.",
                "source_url": "http://localhost/doc",
                "locator": {"page": 1},
                "prev_chunk_id": None,
                "next_chunk_id": None,
                "score": 1.0,
                "ranks": {"document_search": 1},
            }
        ]

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", _viewer_kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", _viewer_access_scope)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "search_document_chunks", search_chunks)

    response = await api_app.search_document("doc:1", DocumentSearchRequest(query="verification"), _request())

    assert response.results[0].chunk_id == "chunk:1"
    assert response.results[0].snippet == "A report with verification marker."


async def test_document_context_returns_404_for_unknown_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def get_document(*_args: object) -> dict[str, Any]:
        return _document()

    async def context_chunks(*_args: object, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_load_kb_role_optional", _viewer_kb_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", _viewer_access_scope)
    monkeypatch.setattr(api_app, "get_document_public", get_document)
    monkeypatch.setattr(api_app, "fetch_document_context_chunks", context_chunks)

    with pytest.raises(HTTPException) as exc_info:
        await api_app.get_document_context(
            "doc:1",
            _request(),
            chunk_id="missing",
            section_id=None,
            before=2,
            after=2,
            limit=80,
            offset=0,
        )

    assert exc_info.value.status_code == 404


def test_document_viewer_sql_is_tenant_scoped_and_published_only() -> None:
    context_sql = str(fetch_document_context_chunks.__code__.co_consts)
    search_sql = str(search_document_chunks.__code__.co_consts)

    for sql in (context_sql, search_sql):
        assert "c.tenant_id = :tenant_id" in sql
        assert "c.knowledge_base_id = :kb_id" in sql
        assert "c.document_id = :document_id" in sql
        assert "c.publication_status = 'published'" in sql
        assert "d.lifecycle_state = 'active'" in sql


def test_document_sections_replace_casts_nullable_document_version_id() -> None:
    sql = str(
        (
            replace_document_sections_from_chunks.__code__.co_consts,
            list_document_sections.__code__.co_consts,
            fetch_document_context_chunks.__code__.co_consts,
            search_document_chunks.__code__.co_consts,
        )
    )

    assert "CAST(:document_version_id AS text) IS NULL" in sql
    assert "document_version_id = CAST(:document_version_id AS text)" in sql
