from __future__ import annotations

from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import (
    ActorContext,
    AuthenticationMethod,
    KnowledgeBaseRole,
    PlatformRole,
    TenantRole,
)
from wikipediarag.retrieval_contract import KnowledgeBaseNotReady, RetrievalProfileIncompatible
from wikipediarag.schemas import ResearchPlanCreate


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


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: Any, _parameters: dict[str, Any]) -> _EmptyResult:
        self.statements.append(str(statement))
        return _EmptyResult()


class _EmptyMappings:
    def __iter__(self) -> object:
        return iter(())

    def first(self) -> None:
        return None


class _EmptyResult:
    def mappings(self) -> _EmptyMappings:
        return _EmptyMappings()

    def scalar(self) -> bool:
        return False


class _RecordingConnectionContext:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _RecordingConnection:
        return self.connection

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
            "path": "/api/v1/research-plans",
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


def test_research_scope_always_contains_primary_knowledge_base() -> None:
    assert api_app._research_plan_scope_ids(["secondary", "third"], "primary") == [
        "primary",
        "secondary",
        "third",
    ]


@pytest.mark.asyncio
async def test_delete_empty_knowledge_base_removes_its_grants_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RecordingConnection()

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def require_kb_role(_conn: object, **_kwargs: Any) -> KnowledgeBaseRole:
        return KnowledgeBaseRole.owner

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def get_kb(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"active_index": ""}

    def delete_chunks(*_args: Any, **_kwargs: Any) -> None:
        return None

    def delete_artifacts(*_args: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(api_app, "connect", lambda: _RecordingConnectionContext(connection))
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_kb_role)
    monkeypatch.setattr(api_app, "_audit", audit)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "delete_document_chunks", delete_chunks)
    monkeypatch.setattr(api_app, "delete_objects", delete_artifacts)

    response = await api_app.delete_knowledge_base("kb-id", _request())

    assert response == {"status": "deleted"}
    grant_delete = next(
        index
        for index, statement in enumerate(connection.statements)
        if "DELETE FROM knowledge_base_grants" in statement
    )
    projection_delete = next(
        index
        for index, statement in enumerate(connection.statements)
        if "DELETE FROM search_projection_events" in statement
    )
    document_delete = next(
        index for index, statement in enumerate(connection.statements) if "DELETE FROM documents" in statement
    )
    kb_delete = next(
        index for index, statement in enumerate(connection.statements) if "DELETE FROM knowledge_bases" in statement
    )
    assert projection_delete < document_delete
    assert grant_delete < kb_delete


async def test_create_research_plan_returns_safe_kb_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def require_kb_role(_conn: object, **_kwargs: Any) -> KnowledgeBaseRole:
        return KnowledgeBaseRole.viewer

    async def resolve_profile(_conn: object, **_kwargs: Any) -> SimpleNamespace:
        raise KnowledgeBaseNotReady(
            "active index source is incompatible with retrieval profile",
            details={"knowledge_base_id": "33333333-3333-4333-8333-333333333333"},
        )

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_kb_role)
    monkeypatch.setattr(api_app, "resolve_retrieval_profile", resolve_profile)

    with pytest.raises(HTTPException) as exc_info:
        await api_app.create_research_plan_endpoint(
            ResearchPlanCreate(
                topic="QA topic",
                knowledge_base_id="33333333-3333-4333-8333-333333333333",
            ),
            _request(),
        )

    assert exc_info.value.status_code == 409
    detail = cast(dict[str, Any], exc_info.value.detail)
    error = cast(dict[str, Any], detail["error"])
    assert error["code"] == "KB_NOT_READY"
    assert error["request_id"] == _actor().request_id


@pytest.mark.asyncio
async def test_retrieval_profile_catalog_returns_incompatible_scope_without_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def load_roles(_conn: object, **_kwargs: Any) -> dict[str, KnowledgeBaseRole]:
        return {"first": KnowledgeBaseRole.viewer, "second": KnowledgeBaseRole.viewer}

    async def get_kb(_conn: object, _tenant_id: str, kb_id: str) -> dict[str, str]:
        return {"id": kb_id, "active_index": "read_alias"}

    async def load_index(_conn: object, **_kwargs: Any) -> dict[str, str]:
        return {"id": "index-id", "read_alias": "read_alias", "embedding_alias": "embed_default"}

    async def resolve_profile(_conn: object, **_kwargs: Any) -> SimpleNamespace:
        raise RetrievalProfileIncompatible("no common profile")

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_load_kb_scope_roles", load_roles)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "load_index_version_by_read_alias", load_index)
    monkeypatch.setattr(api_app, "resolve_retrieval_profile", resolve_profile)

    catalog = await api_app.retrieval_profiles(_request(), ["first", "second"])

    assert catalog.resolved_default is None
    assert catalog.scope_error_code == "RETRIEVAL_PROFILE_INCOMPATIBLE"
    assert catalog.profiles
    assert all(not profile.compatible for profile in catalog.profiles)
