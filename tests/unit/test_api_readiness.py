from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx
import pytest

import wikipediarag.api_app as api_app
from wikipediarag.config import Settings
from wikipediarag.observability import ModelGatewayError
from wikipediarag.schemas import Evidence, RetrievalResult


class _FakeConnection:
    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


class _FakeGatewayClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def __aenter__(self) -> _FakeGatewayClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def get(self, _url: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "degraded",
                "checks": [
                    {
                        "component": "openrouter.startup_smoke",
                        "status": "failed",
                        "reason": "provider_http_403",
                    }
                ],
            },
        )


@pytest.mark.asyncio
async def test_api_ready_reports_model_gateway_failed_when_gateway_ready_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_app, "get_settings", lambda: Settings(model_gateway_url="http://gateway.test"))
    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnection())
    monkeypatch.setattr(httpx, "AsyncClient", _FakeGatewayClient)

    payload: dict[str, Any] = await api_app.ready()

    assert payload == {
        "status": "degraded",
        "components": {
            "postgres": "ok",
            "model_gateway": "failed",
        },
    }


def test_safe_failure_payload_preserves_stage_trace_and_chunk_ids() -> None:
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Статья",
                section_path=["Статья"],
                content="secret document text must not be copied",
                source_url="http://localhost/source",
                scores={"rerank": 1.0},
            )
        ],
        events=[
            {
                "stage": "rerank",
                "count": 1,
                "latency_ms": 12,
                "candidates": [
                    {
                        "chunk_id": "c1",
                        "title": "Статья",
                        "source_url": "http://localhost/source",
                        "scores": {"rerank": 1.0},
                    }
                ],
            }
        ],
        index_contract_id="sha256:index",
        run_contract_id="sha256:child",
    )

    payload = api_app._safe_failure_payload(
        TimeoutError("provider timed out with details"),
        stage="answer_generation",
        last_successful_stage="retrieval",
        trace_id="trace",
        retrieval=retrieval,
    )

    assert payload["stage"] == "answer_generation"
    assert payload["code"] == "TimeoutError"
    assert payload["last_successful_stage"] == "retrieval"
    assert payload["trace_id"] == "trace"
    assert payload["retrieval"]["evidence"][0]["chunk_id"] == "c1"
    assert "content" not in payload["retrieval"]["evidence"][0]
    assert payload["root_cause"]["code"] == "runtime_failure"
    assert payload["answer_artifact"]["experimental"] is True
    assert "secret document text" not in str(payload["answer_artifact"])


def test_failure_stage_event_keeps_safe_model_metadata() -> None:
    exc = ModelGatewayError(
        "model gateway request failed",
        metadata={
            "operation": "chat",
            "model_alias": "generator_main",
            "latency_ms": 123,
            "attempts": 3,
            "retries": 2,
            "safe_error_code": "provider_network_error",
        },
        cause=httpx.NetworkError("network down"),
    )

    payload = api_app._failure_stage_event(
        exc,
        stage="answer_generation",
        last_successful_stage="retrieval",
    )

    assert payload["stage"] == "answer_generation"
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "provider_network_error"
    assert payload["model_call"]["model_alias"] == "generator_main"
    assert payload["model_call"]["attempts"] == 3
    assert "provider_payload" not in payload
    assert "prompt" not in payload


def test_context_source_summary_groups_citations_by_subquery() -> None:
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        events=[],
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="A",
                section_path=["A"],
                content="alpha",
                source_url="http://localhost/a",
                metadata={"subquery_id": "sq.decomposition.1", "transform_id": "tr.decomposition.1"},
            ),
            Evidence(
                evidence_id="S2",
                chunk_id="c2",
                title="B",
                section_path=["B"],
                content="beta",
                source_url="http://localhost/b",
                metadata={"subquery_id": "sq.decomposition.2", "transform_id": "tr.decomposition.1"},
            ),
        ],
    )

    summary = api_app._context_source_summary(retrieval, ["S2"])

    assert summary == [
        {
            "subquery_id": "sq.decomposition.1",
            "transform_id": "tr.decomposition.1",
            "evidence_count": 1,
            "citation_ids": [],
        },
        {
            "subquery_id": "sq.decomposition.2",
            "transform_id": "tr.decomposition.1",
            "evidence_count": 1,
            "citation_ids": ["S2"],
        },
    ]
    assert "alpha" not in str(summary)


def test_safe_validation_errors_do_not_echo_rejected_input() -> None:
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "password"),
            "msg": "String should have at least 12 characters",
            "input": "secret",
        }
    ]

    safe = api_app._safe_validation_errors(errors)

    assert safe == [
        {
            "type": "string_too_short",
            "loc": ("body", "password"),
            "msg": "String should have at least 12 characters",
        }
    ]
