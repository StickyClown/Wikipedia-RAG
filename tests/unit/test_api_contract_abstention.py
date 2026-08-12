from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole, TenantRole
from wikipediarag.config import Settings
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import ChatRequest, Evidence, RetrievalResult


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
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        },
        receive,
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


@pytest.mark.asyncio
async def test_answer_stage_persists_contract_abstention_without_model_content(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []

    async def record_event(_conn: object, **kwargs: object) -> None:
        events.append(kwargs)

    monkeypatch.setattr(api_app, "insert_retrieval_event", record_event)
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Россия",
                section_path=[],
                content="секрет",
                source_url="u1",
            )
        ],
        events=[],
    )
    validation = {
        "citations": [],
        "status": "abstained",
        "model_gateway": {"provider": "Venice", "provider_request_id": "request"},
        "model_output_contract_abstained": True,
        "model_output_contract_reason": "answer_mode_contract",
    }

    await api_app._insert_answer_events(
        object(),
        tenant_id="tenant",
        query_run_id="run",
        trace_id="trace",
        retrieval=retrieval,
        answer="Я не могу сформировать проверяемый ответ по найденным источникам.",
        validation=validation,
        timings_ms={"generation_total": 1},
        answer_artifact={"root_cause": {}},
    )

    answer_event = next(event for event in events if event["stage"] == "answer_generation")
    payload = answer_event["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "abstained"
    assert payload["model_output_contract_abstained"] is True
    assert payload["model_output_contract_reason"] == "answer_mode_contract"
    assert "raw_model_content" not in payload


@pytest.mark.asyncio
async def test_chat_contract_abstention_completes_and_exposes_safe_sse_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted_usage: dict[str, Any] = {}

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def load_roles(_conn: object, **_kwargs: Any) -> dict[str, object]:
        return {"kb": None}

    async def document_scopes(_conn: object, **_kwargs: Any) -> dict[str, object]:
        return {"kb": object()}

    async def resolve_profile(_conn: object, **_kwargs: Any) -> Any:
        return get_retrieval_profile("test_mock", Settings())

    async def create_run(_conn: object, **_kwargs: Any) -> str:
        return "run"

    async def retrieve(_conn: object, _query: str, **_kwargs: Any) -> RetrievalResult:
        return RetrievalResult(
            query="q",
            trace_id="trace",
            evidence=[],
            events=[],
        )

    async def generate(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object]]:
        return (
            "Не удалось сформировать проверяемый ответ по найденным источникам.",
            {
                "valid": True,
                "status": "abstained",
                "citations": [],
                "claims": [],
                "interpretations": [],
                "answer_mode": "single",
                "insufficient_evidence": True,
                "usage": {"total_tokens": 7},
                "model_output_contract_abstained": True,
                "model_output_contract_reason": "undeclared_citation",
                "timings_ms": {},
            },
        )

    async def complete_run(_conn: object, **kwargs: Any) -> None:
        persisted_usage.update(dict(kwargs["usage"]))

    async def record_event(_conn: object, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "connect_autocommit", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_load_kb_scope_roles", load_roles)
    monkeypatch.setattr(api_app, "_document_access_scopes", document_scopes)
    monkeypatch.setattr(api_app, "resolve_retrieval_profile", resolve_profile)
    monkeypatch.setattr(api_app, "create_query_run", create_run)
    monkeypatch.setattr(api_app, "retrieve", retrieve)
    monkeypatch.setattr(api_app, "generate_answer", generate)
    monkeypatch.setattr(api_app, "complete_query_run", complete_run)
    monkeypatch.setattr(api_app, "insert_retrieval_event", record_event)

    response = await api_app.stream_chat_response(
        ChatRequest(message="q", knowledge_base_ids=["kb"]),
        _request(),
    )
    body = ""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, str) else bytes(chunk).decode()

    assert "event: run.completed" in body
    assert '"model_output_contract_abstained": true' in body
    assert '"model_output_contract_reason": "undeclared_citation"' in body
    assert "raw_model_content" not in body
    assert persisted_usage["status"] == "abstained"
    assert persisted_usage["model_output_contract_abstained"] is True
    assert persisted_usage["model_output_contract_reason"] == "undeclared_citation"
