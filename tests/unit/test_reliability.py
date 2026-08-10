from __future__ import annotations

import json
import time
from threading import Lock
from typing import Any, cast

import httpx
import pytest

import wikipediarag.cli as cli
import wikipediarag.eval.document_benchmark as document_benchmark
import wikipediarag.model_client as model_client
from wikipediarag.config import Settings
from wikipediarag.document_ingestion import ParserServiceError
from wikipediarag.eval.api_client import HttpEvalApiClient
from wikipediarag.ingestion import _retryable_document_ingestion_error
from wikipediarag.reliability import (
    DependencyCircuit,
    DependencyCircuitOpen,
    OperationDeadline,
    OperationDeadlineExceeded,
    is_retryable_exception,
    safe_failure_from_exception,
)


def test_operation_deadline_exposes_only_remaining_time() -> None:
    deadline = OperationDeadline.after(0)

    assert deadline.remaining_ms() == 0
    with pytest.raises(OperationDeadlineExceeded):
        deadline.ensure_remaining(stage="generation")


@pytest.mark.asyncio
async def test_model_client_uses_shared_deadline_and_correlation_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class RecordingClient:
        async def post(self, *_args: object, **kwargs: object) -> httpx.Response:
            captured.update(kwargs)
            return httpx.Response(
                200,
                request=httpx.Request("POST", "http://gateway.test/v1/chat/completions"),
                json={"choices": [{"message": {"content": "{}"}}]},
            )

    monkeypatch.setattr(model_client, "_http_client", lambda: RecordingClient())
    deadline = OperationDeadline.after(1)

    await model_client._post_json(
        "http://gateway.test/v1/chat/completions",
        {"model": "generator_main", "messages": []},
        timeout_seconds=300,
        max_attempts=1,
        operation="chat",
        alias="generator_main",
        deadline=deadline,
        correlation_id="query-run-1",
    )

    assert 0 < float(cast(float, captured["timeout"])) <= 1
    assert captured["headers"] == {"X-Request-ID": "query-run-1"}


def test_dependency_circuit_opens_then_allows_one_half_open_probe() -> None:
    clock = [10.0]
    circuit = DependencyCircuit("generator_main", failure_threshold=2, cooldown_seconds=5, now=lambda: clock[0])

    circuit.record_failure()
    circuit.record_failure()
    with pytest.raises(DependencyCircuitOpen):
        circuit.before_call()

    clock[0] = 15.1
    circuit.before_call()
    with pytest.raises(DependencyCircuitOpen):
        circuit.before_call()
    circuit.record_success()
    circuit.before_call()


def test_safe_failure_is_redacted_and_retryable_only_for_transient_transport() -> None:
    transient = httpx.ConnectError("provider payload must not leak")
    failure = safe_failure_from_exception(transient, stage="gateway", operation_id="run-1")

    assert failure.error_code == "DEPENDENCY_UNAVAILABLE"
    assert failure.retryable is True
    assert "payload" not in str(failure.model_dump())
    assert is_retryable_exception(ValueError("bad request")) is False


@pytest.mark.asyncio
async def test_async_eval_client_validates_sse_and_keeps_only_terminally_relevant_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def event(name: str, sequence: int, data: dict[str, object]) -> str:
        return f"event: {name}\ndata: {json.dumps({'query_run_id': 'run-1', 'sequence': sequence, 'data': data})}\n\n"

    stream = "".join(
        [
            event("run.started", 1, {"trace_id": "trace-1", "stage": "started"}),
            event("stage.heartbeat", 2, {"stage": "answer_generation"}),
            event("usage.updated", 3, {"retrieval": {"evidence": []}}),
            event("run.completed", 4, {"answer": "ok", "stage": "completed"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["idempotency-key"].startswith("eval-")
        return httpx.Response(200, text=stream)

    real_async_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    client = HttpEvalApiClient()

    result = await client.run_chat_async(
        "Вопрос",
        api="http://api.test",
        retrieval_profile="upload_sota_mvp",
        retrieval_overrides={},
        mode="normal",
        knowledge_base_ids=["kb-1"],
    )

    assert result["failed"] is False
    assert result["query_run_id"] == "run-1"
    assert result["answer"] == "ok"
    assert [item["event"] for item in result["events"]] == ["run.started", "usage.updated", "run.completed"]


@pytest.mark.asyncio
async def test_async_eval_client_marks_truncated_sse_as_retryable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = (
        'event: run.started\ndata: {"query_run_id":"run-1","sequence":1,"data":{"stage":"started"}}\n\n'
        'event: stage.heartbeat\ndata: {"query_run_id":"run-1","sequence":2,"data":{"stage":"retrieval"}}\n\n'
    )

    real_async_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(
            *args, transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=stream)), **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    result = await HttpEvalApiClient().run_chat_async(
        "Вопрос",
        api="http://api.test",
        retrieval_profile="upload_sota_mvp",
        retrieval_overrides={},
        mode="normal",
    )

    assert result["failed"] is True
    assert result["failure"] == {"code": "STREAM_PROTOCOL_ERROR", "stage": "retrieval", "retryable": True}


def test_document_ingestion_retry_is_limited_to_classified_transient_failures() -> None:
    settings = Settings(safe_external_retry_attempts=2)
    first_attempt = {"attempts": 1}
    final_attempt = {"attempts": 2}

    assert _retryable_document_ingestion_error(
        ParserServiceError("xberg", "HTTP_503", "parser unavailable"),
        item=first_attempt,
        settings=settings,
    )
    assert not _retryable_document_ingestion_error(
        ParserServiceError("xberg", "HTTP_400", "parser rejected input"),
        item=first_attempt,
        settings=settings,
    )
    assert not _retryable_document_ingestion_error(
        ParserServiceError("xberg", "HTTP_503", "parser unavailable"),
        item=final_attempt,
        settings=settings,
    )


def test_rrncb_upload_retry_budget_is_spent_only_after_a_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_state = {"used": 0}
    responses = iter(
        [
            httpx.Response(503, request=httpx.Request("PUT", "http://storage.test/object")),
            httpx.Response(200, request=httpx.Request("PUT", "http://storage.test/object")),
        ]
    )
    monkeypatch.setattr(httpx, "put", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    duration = document_benchmark._upload_one(
        "http://storage.test/object",
        b"pdf",
        {"Content-Type": "application/pdf"},
        retry_state,
        Lock(),
    )

    assert duration >= 0
    assert retry_state == {"used": 1}


def test_isolated_reliability_smoke_command_is_registered() -> None:
    args = cli.build_parser().parse_args(["reliability-smoke", "--skip-compose", "--down-after"])

    assert args.command == "reliability-smoke"
    assert args.skip_compose is True
    assert args.down_after is True
