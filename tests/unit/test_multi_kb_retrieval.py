from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, KnowledgeBaseRole, PlatformRole, TenantRole
from wikipediarag.config import Settings
from wikipediarag.document_access import DocumentAccessScope
from wikipediarag.retrieval import apply_knowledge_base_cap, build_stage_events, rrf_fuse
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import DebugSearchRequest, Evidence, RetrievalResult


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
            "path": "/api/v1/search:debug",
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
        platform_role=PlatformRole.platform_admin,
        active_tenant_id="11111111-1111-4111-8111-111111111111",
        tenant_role=TenantRole.tenant_admin,
        session_id="session",
        authentication_method=AuthenticationMethod.local,
        request_id="33333333-3333-4333-8333-333333333333",
        trace_id="trace",
    )


async def test_search_debug_accepts_multi_kb_scope_and_returns_kb_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_roles: list[tuple[str, KnowledgeBaseRole]] = []
    access_calls: list[tuple[str, KnowledgeBaseRole]] = []
    retrieval_events: list[dict[str, Any]] = []
    scopes = {
        "kb-a": DocumentAccessScope(user_id="22222222-2222-4222-8222-222222222222"),
        "kb-b": DocumentAccessScope(bypass=True, user_id="22222222-2222-4222-8222-222222222222"),
    }

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def require_role(_conn: object, **kwargs: Any) -> KnowledgeBaseRole:
        required_roles.append((str(kwargs["kb_id"]), kwargs["role"]))
        return KnowledgeBaseRole.manager if str(kwargs["kb_id"]) == "kb-b" else KnowledgeBaseRole.editor

    async def load_access_scope(_conn: object, **kwargs: Any) -> DocumentAccessScope:
        kb_id = str(kwargs["knowledge_base_id"])
        access_calls.append((kb_id, kwargs["effective_kb_role"]))
        return scopes[kb_id]

    async def retrieve_multi(_conn: object, _query: str, **kwargs: Any) -> RetrievalResult:
        assert kwargs["knowledge_base_ids"] == ["kb-a", "kb-b"]
        assert kwargs["query_run_id"] == "44444444-4444-4444-8444-444444444444"
        assert kwargs["search_filters"] == {"document_access_scopes": scopes}
        return RetrievalResult(
            query="query",
            trace_id="trace",
            evidence=[
                Evidence(
                    evidence_id="S1",
                    chunk_id="chunk-b",
                    knowledge_base_id="kb-b",
                    title="Title",
                    section_path=[],
                    content="content",
                    source_url="https://example.test/doc",
                )
            ],
            events=[],
        )

    async def create_query_run(_conn: object, **_kwargs: Any) -> str:
        return "44444444-4444-4444-8444-444444444444"

    async def insert_retrieval_event(_conn: object, **kwargs: Any) -> None:
        retrieval_events.append(kwargs)

    async def complete_query_run(_conn: object, **_kwargs: Any) -> None:
        return None

    async def resolve_profile(_conn: object, **_kwargs: Any) -> Any:
        return get_retrieval_profile("test_mock", Settings())

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_role)
    monkeypatch.setattr(api_app, "load_actor_document_access_scope", load_access_scope)
    monkeypatch.setattr(api_app, "create_query_run", create_query_run)
    monkeypatch.setattr(api_app, "insert_retrieval_event", insert_retrieval_event)
    monkeypatch.setattr(api_app, "complete_query_run", complete_query_run)
    monkeypatch.setattr(api_app, "retrieve_multi", retrieve_multi)
    monkeypatch.setattr(api_app, "resolve_retrieval_profile", resolve_profile)

    payload = DebugSearchRequest(message="query", knowledge_base_ids=["kb-a", "kb-b"])
    output = await api_app.search_debug(payload, _request())

    assert required_roles == [("kb-a", KnowledgeBaseRole.editor), ("kb-b", KnowledgeBaseRole.editor)]
    assert access_calls == [("kb-a", KnowledgeBaseRole.editor), ("kb-b", KnowledgeBaseRole.manager)]
    assert output["query_run_id"] == "44444444-4444-4444-8444-444444444444"
    assert output["evidence"][0]["knowledge_base_id"] == "kb-b"
    assert output["search_plan"]["knowledge_base_ids"] == ["kb-a", "kb-b"]
    assert retrieval_events[0]["stage"] == "path_selected"


async def test_query_run_retrieval_does_not_load_events_for_cross_tenant_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def load_query_run_for_actor(_conn: object, **_kwargs: Any) -> None:
        return None

    async def load_retrieval_events(*_args: object, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("cross-tenant event content must not be loaded")

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_load_query_run_for_actor", load_query_run_for_actor)
    monkeypatch.setattr(api_app, "load_retrieval_events", load_retrieval_events)

    with pytest.raises(HTTPException) as exc_info:
        await api_app.query_run_retrieval("foreign-run", _request())

    assert exc_info.value.status_code == 404


def test_apply_knowledge_base_cap_limits_dominant_kb() -> None:
    candidates = [
        {"chunk_id": "a1", "knowledge_base_id": "kb-a"},
        {"chunk_id": "a2", "knowledge_base_id": "kb-a"},
        {"chunk_id": "b1", "knowledge_base_id": "kb-b"},
    ]

    selected = apply_knowledge_base_cap(candidates, max_per_kb=1)

    assert [item["chunk_id"] for item in selected] == ["a1", "b1"]


def test_multi_kb_extended_search_skip_reason_is_explicit() -> None:
    status = api_app._extended_search_status(
        route_decision={"route": "direct_retrieval", "reason": "multi_kb_extended_search_not_enabled_v1"},
        knowledge_base_ids=["kb-a", "kb-b"],
        extended_policy="always",
    )

    assert status == {"decision": "skipped", "reason": "multi_kb_extended_search_not_enabled_v1"}


def test_rrf_fusion_keeps_same_chunk_id_separate_across_kbs() -> None:
    result_sets = {
        "bm25:kb-a": [
            {"chunk_id": "shared", "knowledge_base_id": "kb-a", "scores": {"bm25": 1.0}, "ranks": {"bm25": 1}}
        ],
        "bm25:kb-b": [
            {"chunk_id": "shared", "knowledge_base_id": "kb-b", "scores": {"bm25": 1.0}, "ranks": {"bm25": 1}}
        ],
    }

    fused = rrf_fuse(result_sets, top_k=10)

    assert {(item["knowledge_base_id"], item["chunk_id"]) for item in fused} == {
        ("kb-a", "shared"),
        ("kb-b", "shared"),
    }


def test_multi_kb_stage_events_share_primary_query_context() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    candidate_a = {
        "chunk_id": "a1",
        "document_id": "doc-a",
        "knowledge_base_id": "kb-a",
        "page_id": 1,
        "title": "A",
        "section_path": ["A"],
        "content": "alpha",
        "source_url": "http://localhost/a",
        "scores": {"bm25": 1.0},
        "ranks": {"bm25": 1},
        "metadata": {},
    }
    candidate_b = {
        "chunk_id": "b1",
        "document_id": "doc-b",
        "knowledge_base_id": "kb-b",
        "page_id": 1,
        "title": "B",
        "section_path": ["B"],
        "content": "beta",
        "source_url": "http://localhost/b",
        "scores": {"bm25": 0.9},
        "ranks": {"bm25": 1},
        "metadata": {},
    }

    events = build_stage_events(
        query="query",
        normalized_query="query",
        profile=profile,
        read_alias="multi",
        result_sets={"bm25:kb-a": [candidate_a], "bm25:kb-b": [candidate_b]},
        fused=[candidate_a, candidate_b],
        reranked=[candidate_a, candidate_b],
        selected=[candidate_a, candidate_b],
        policy_events=[],
        latency_ms=10,
    )

    for stage in ("bm25:kb-a", "bm25:kb-b"):
        event = next(item for item in events if item["stage"] == stage)
        assert event["query_context"]["subquery_id"] == "sq.primary.1"
        assert event["candidates"][0]["subquery_id"] == "sq.primary.1"
