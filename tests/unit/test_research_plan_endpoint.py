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
from wikipediarag.retrieval_contract import KnowledgeBaseNotReady
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


async def test_create_research_plan_returns_safe_kb_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def require_kb_role(_conn: object, **_kwargs: Any) -> KnowledgeBaseRole:
        return KnowledgeBaseRole.viewer

    async def validate_contract(_conn: object, **_kwargs: Any) -> None:
        raise KnowledgeBaseNotReady(
            "active index source is incompatible with retrieval profile",
            details={"knowledge_base_id": "33333333-3333-4333-8333-333333333333"},
        )

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_kb_role)
    monkeypatch.setattr(api_app, "validate_active_retrieval_contract", validate_contract)
    monkeypatch.setattr(
        api_app,
        "get_retrieval_profile",
        lambda *_args, **_kwargs: SimpleNamespace(name="upload_sota_mvp"),
    )

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
