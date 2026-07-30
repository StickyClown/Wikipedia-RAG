from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx
import pytest

import wikipediarag.api_app as api_app
from wikipediarag.config import Settings
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
