from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from fastapi import HTTPException

import wikipediarag.gateway_app as gateway_app
from wikipediarag.answering import ANSWER_JSON_SCHEMA
from wikipediarag.config import Settings, resolve_openrouter_api_key
from wikipediarag.model_registry import ModelAlias
from wikipediarag.reliability import DependencyCircuit


def _settings(*, startup_smoke: Literal["required", "warn", "off"], api_key: str = "test-key") -> Settings:
    return Settings(
        retrieval_profile="sota_mvp",
        openrouter_api_key=api_key,
        model_gateway_startup_smoke=startup_smoke,
    )


def _forbidden_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://openrouter.example.test/api/v1/models")
    response = httpx.Response(403, request=request)
    return httpx.HTTPStatusError("forbidden", request=request, response=response)


class _TimeoutAsyncClient:
    captured_timeout: object = None

    def __init__(self, *_args: object, **kwargs: object) -> None:
        type(self).captured_timeout = kwargs.get("timeout")

    async def __aenter__(self) -> _TimeoutAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.TimeoutException("provider timeout with raw payload")


class _RecordingProxyAsyncClient:
    captured_timeout: object = None
    captured_url: object = None
    captured_json: object = None
    captured_headers: object = None

    def __init__(self, *_args: object, **kwargs: object) -> None:
        type(self).captured_timeout = kwargs.get("timeout")

    async def __aenter__(self) -> _RecordingProxyAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, *, json: object, headers: object) -> httpx.Response:
        type(self).captured_url = url
        type(self).captured_json = json
        type(self).captured_headers = headers
        request = httpx.Request("POST", url, headers=cast(dict[str, str], headers))
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})


def test_openrouter_api_key_resolves_from_secret_file(tmp_path: Path) -> None:
    key_file = tmp_path / "openrouter.key"
    key_file.write_text("test-openrouter-key\n", encoding="utf-8")
    settings = Settings(openrouter_api_key="", openrouter_api_key_file=key_file)

    assert resolve_openrouter_api_key(settings) == "test-openrouter-key"


def test_empty_openrouter_api_key_file_is_ignored() -> None:
    settings = Settings(openrouter_api_key="", openrouter_api_key_file=cast(Any, ""))

    assert settings.openrouter_api_key_file is None
    assert resolve_openrouter_api_key(settings) == ""


def test_structured_startup_canary_uses_production_answer_schema() -> None:
    production_schema = ANSWER_JSON_SCHEMA["json_schema"]["schema"]

    assert gateway_app._STRUCTURED_SMOKE_SCHEMA == production_schema
    assert "answer_markdown" in gateway_app._STRUCTURED_SMOKE_SCHEMA["required"]
    assert "claims" in gateway_app._STRUCTURED_SMOKE_SCHEMA["required"]
    assert "insufficient_evidence" in gateway_app._STRUCTURED_SMOKE_SCHEMA["required"]


def test_gateway_enforces_structured_length_and_item_constraints() -> None:
    schema = ANSWER_JSON_SCHEMA["json_schema"]["schema"]
    invalid = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "answer_markdown": "",
                            "answer_mode": "single",
                            "interpretations": [],
                            "clarification_question": None,
                            "claims": [],
                            "insufficient_evidence": True,
                        }
                    )
                }
            }
        ]
    }

    with pytest.raises(ValueError, match="shorter than required"):
        gateway_app._validate_structured_provider_response(invalid, schema)


def test_gateway_canonicalizes_fully_fenced_structured_json() -> None:
    schema = ANSWER_JSON_SCHEMA["json_schema"]["schema"]
    payload = _valid_structured_smoke_response()
    message = payload["choices"][0]["message"]
    message["content"] = f"```json\n{message['content']}\n```"

    gateway_app._validate_structured_provider_response(payload, schema)

    assert message["content"].startswith("{")
    assert not message["content"].startswith("```")


def test_structured_startup_canary_disables_default_reasoning_with_bounded_output() -> None:
    payload = gateway_app._structured_smoke_request("qwen/example", provider_preferences={"require_parameters": True})

    assert payload["model"] == "qwen/example"
    assert payload["max_tokens"] == 4096
    assert payload["stream"] is False
    assert "S1" in payload["messages"][0]["content"]
    assert payload["provider"] == {"require_parameters": True}


def _valid_structured_smoke_response(*, completion_tokens: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "answer_markdown": "Проверочный факт подтверждён [S1]",
                            "answer_mode": "single",
                            "interpretations": [],
                            "clarification_question": None,
                            "claims": [
                                {
                                    "claim_id": "smoke-claim",
                                    "text": "Проверочный факт подтверждён",
                                    "evidence_ids": ["S1"],
                                    "type": "fact",
                                }
                            ],
                            "insufficient_evidence": False,
                        }
                    )
                }
            }
        ]
    }
    if completion_tokens is not None:
        payload["usage"] = {"completion_tokens": completion_tokens}
    return payload


def test_structured_startup_canary_requires_a_grounded_claim() -> None:
    payload = _valid_structured_smoke_response()

    gateway_app._validate_structured_smoke_response(payload, alias="generator_main")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer_markdown": "Недостаточно данных.",
                                "answer_mode": "single",
                                "interpretations": [],
                                "clarification_question": None,
                                "claims": [],
                                "insufficient_evidence": True,
                            }
                        )
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer_markdown": "Проверочный факт подтверждён [S1]",
                                "answer_mode": "single",
                                "interpretations": [],
                                "clarification_question": None,
                                "claims": [
                                    {
                                        "claim_id": "wrong-evidence",
                                        "text": "Проверочный факт подтверждён",
                                        "evidence_ids": ["S2"],
                                        "type": "fact",
                                    }
                                ],
                                "insufficient_evidence": False,
                            }
                        )
                    }
                }
            ]
        },
    ],
)
def test_structured_startup_canary_rejects_semantic_contract_violations(payload: dict[str, Any]) -> None:
    with pytest.raises(gateway_app.StructuredSmokeError) as error:
        gateway_app._validate_structured_smoke_response(payload, alias="generator_main")

    assert error.value.reason == "structured_grounded_contract_invalid"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]},
            "structured_output_truncated",
        ),
        ({"choices": [{"message": {"content": "{}"}}]}, "structured_schema_invalid"),
        (
            _valid_structured_smoke_response(completion_tokens=4097),
            "structured_output_limit_exceeded",
        ),
    ],
)
def test_structured_startup_canary_classifies_contract_failures_safely(payload: dict[str, Any], reason: str) -> None:
    with pytest.raises(gateway_app.StructuredSmokeError) as exc_info:
        gateway_app._validate_structured_smoke_response(payload, alias="generator_main")

    assert exc_info.value.alias == "generator_main"
    assert exc_info.value.reason == reason
    assert gateway_app._safe_smoke_failure_reason(exc_info.value) == reason


def test_structured_requests_use_explicit_adapter_thinking_policy() -> None:
    alias = ModelAlias(provider="openrouter", model="qwen/example", operation="chat", context_window_tokens=1024)
    defaulted = {"response_format": ANSWER_JSON_SCHEMA, "max_tokens": 800, "messages": [{"content": "x" * 3072}]}
    explicit = {
        "response_format": ANSWER_JSON_SCHEMA,
        "reasoning": {"effort": "high"},
        "max_tokens": 800,
        "messages": [{"content": "x" * 3072}],
    }

    metadata = gateway_app._apply_provider_request_defaults(defaulted, alias)
    gateway_app._apply_provider_request_defaults(explicit, alias)

    assert "reasoning" not in defaulted
    assert explicit["reasoning"] == {"effort": "high"}
    assert defaulted["max_tokens"] == 224
    assert metadata["effective_output_tokens"] == 224


@pytest.mark.asyncio
async def test_gateway_warn_mode_starts_degraded_when_openrouter_smoke_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_smoke(*_args: object, **_kwargs: object) -> None:
        raise _forbidden_error()

    monkeypatch.setattr(gateway_app, "get_settings", lambda: _settings(startup_smoke="warn"))
    monkeypatch.setattr(gateway_app, "_openrouter_startup_smoke", fail_smoke)

    await gateway_app.startup_smoke()

    assert await gateway_app.health() == {"status": "ok"}
    ready = await gateway_app.ready()
    assert ready == {
        "status": "degraded",
        "checks": [
            {
                "component": "openrouter.startup_smoke",
                "status": "failed",
                "reason": "INTERNAL_ERROR",
            }
        ],
    }


@pytest.mark.asyncio
async def test_gateway_warn_mode_reports_safe_structured_canary_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_smoke(*_args: object, **_kwargs: object) -> None:
        raise gateway_app.StructuredSmokeError(
            "generator_main",
            "provider response content must never reach readiness",
            reason="structured_schema_invalid",
        )

    monkeypatch.setattr(gateway_app, "get_settings", lambda: _settings(startup_smoke="warn"))
    monkeypatch.setattr(gateway_app, "_openrouter_startup_smoke", fail_smoke)

    await gateway_app.startup_smoke()

    ready = await gateway_app.ready()
    assert {
        "component": "openrouter.startup_smoke",
        "status": "failed",
        "reason": "structured_schema_invalid",
    } in ready["checks"]
    assert {"component": "structured.generator_main", "status": "failed", "reason": "MODEL_ALIAS_UNREADY"} in ready[
        "checks"
    ]


@pytest.mark.asyncio
async def test_gateway_models_marks_openrouter_aliases_unhealthy_after_smoke_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_smoke(*_args: object, **_kwargs: object) -> None:
        raise _forbidden_error()

    monkeypatch.setattr(gateway_app, "get_settings", lambda: _settings(startup_smoke="warn"))
    monkeypatch.setattr(gateway_app, "_openrouter_startup_smoke", fail_smoke)

    await gateway_app.startup_smoke()
    payload = await gateway_app.models()

    by_alias = {item["id"]: item for item in payload["data"]}
    assert by_alias["generator_fast"]["healthy"] is False
    assert by_alias["embed_default"]["healthy"] is False
    assert by_alias["rerank_default"]["healthy"] is False
    assert by_alias["mock_generator_fast"]["healthy"] is True


@pytest.mark.asyncio
async def test_gateway_ready_exposes_recent_runtime_alias_failure_until_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    circuit = DependencyCircuit(
        "generator_fast",
        cooldown_seconds=10,
        now=lambda: clock[0],
    )
    circuit.record_failure()
    monkeypatch.setattr(gateway_app, "_dependency_circuits", {"generator_fast": circuit})

    ready = await gateway_app.ready()

    assert ready["status"] == "degraded"
    assert {
        "component": "runtime.generator_fast",
        "status": "failed",
        "reason": "DEPENDENCY_RUNTIME_FAILURE",
    } in ready["checks"]
    circuit.record_success()
    assert all(check["component"] != "runtime.generator_fast" for check in (await gateway_app.ready())["checks"])


@pytest.mark.asyncio
async def test_gateway_required_mode_preserves_fatal_startup_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_smoke(*_args: object, **_kwargs: object) -> None:
        raise _forbidden_error()

    monkeypatch.setattr(gateway_app, "get_settings", lambda: _settings(startup_smoke="required"))
    monkeypatch.setattr(gateway_app, "_openrouter_startup_smoke", fail_smoke)

    with pytest.raises(httpx.HTTPStatusError):
        await gateway_app.startup_smoke()


@pytest.mark.asyncio
async def test_gateway_models_expose_context_window_and_tokenizer() -> None:
    payload = await gateway_app.models()

    by_alias = {item["id"]: item for item in payload["data"]}
    assert by_alias["generator_fast"]["context_window_tokens"] == 80_000
    assert by_alias["generator_fast"]["tokenizer"] == "qwen3"
    assert by_alias["mock_generator_fast"]["tokenizer"] == "mock_char_estimate_v1"


@pytest.mark.asyncio
async def test_gateway_warn_mode_reports_missing_key_without_fatal_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_app, "get_settings", lambda: _settings(startup_smoke="warn", api_key=""))

    await gateway_app.startup_smoke()

    ready = await gateway_app.ready()
    assert ready["status"] == "degraded"
    assert ready["checks"][0]["reason"] == "openrouter_api_key_missing"


@pytest.mark.asyncio
async def test_gateway_proxy_maps_provider_timeout_without_raw_details(monkeypatch: pytest.MonkeyPatch) -> None:
    _TimeoutAsyncClient.captured_timeout = None
    monkeypatch.setattr(gateway_app, "get_settings", lambda: Settings(model_provider_timeout_seconds=222))
    monkeypatch.setattr("wikipediarag.gateway_app.httpx.AsyncClient", _TimeoutAsyncClient)

    with pytest.raises(HTTPException) as exc_info:
        await gateway_app.proxy("chat/completions", {"model": "mock_generator_fast", "messages": []})

    assert exc_info.value.status_code == 504
    assert cast(Any, exc_info.value.detail) == {
        "error": {
            "code": "DEPENDENCY_TIMEOUT",
            "message": "model dependency did not respond before the deadline",
            "retryable": True,
        }
    }
    assert _TimeoutAsyncClient.captured_timeout == 222


@pytest.mark.asyncio
async def test_gateway_proxy_forwards_alias_provider_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingProxyAsyncClient.captured_timeout = None
    _RecordingProxyAsyncClient.captured_url = None
    _RecordingProxyAsyncClient.captured_json = None
    _RecordingProxyAsyncClient.captured_headers = None
    monkeypatch.setattr(gateway_app, "get_settings", lambda: Settings(openrouter_api_key="test-key"))
    monkeypatch.setattr("wikipediarag.gateway_app.httpx.AsyncClient", _RecordingProxyAsyncClient)

    payload = await gateway_app.proxy("chat/completions", {"model": "generator_fast", "messages": []})

    assert payload["model_alias"] == "generator_fast"
    assert payload["provider"] == "openrouter"
    assert _RecordingProxyAsyncClient.captured_timeout == Settings().model_provider_timeout_seconds
    assert _RecordingProxyAsyncClient.captured_url == "https://openrouter.ai/api/v1/chat/completions"
    assert cast(dict[str, object], _RecordingProxyAsyncClient.captured_json) == {
        "model": "qwen/qwen3.5-9b",
        "messages": [],
        "provider": {"require_parameters": True},
    }
    assert _RecordingProxyAsyncClient.captured_headers == {"Authorization": "Bearer test-key"}


def test_provider_http_error_preserves_retry_after_header() -> None:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        headers={"Retry-After": "11"},
    )

    exc = gateway_app._provider_http_error(response)

    assert exc.status_code == 429
    assert cast(Any, exc.detail) == {
        "error": {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": "model dependency request failed",
            "retryable": True,
        }
    }
    assert exc.headers == {"Retry-After": "11"}


def test_provider_http_error_classifies_auth_rejection_as_non_retryable() -> None:
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        text="provider body must not be exposed",
    )

    exc = gateway_app._provider_http_error(response)

    assert exc.status_code == 502
    assert cast(Any, exc.detail) == {
        "error": {
            "code": "MODEL_PROVIDER_REJECTED",
            "message": "model dependency request failed",
            "retryable": False,
        }
    }


def test_structured_schema_invalid_is_not_retryable() -> None:
    exc = gateway_app._structured_http_error("MODEL_OUTPUT_INVALID", "safe", retryable=False)

    assert cast(Any, exc.detail)["error"] == {
        "code": "MODEL_OUTPUT_INVALID",
        "message": "safe",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_gateway_tokenize_returns_safe_estimate() -> None:
    payload = await gateway_app.tokenize({"model": "mock_generator_fast", "text": "abcd" * 20})

    assert payload == {
        "object": "tokenization",
        "model": "mock_generator_fast",
        "tokenizer": "mock_char_estimate_v1",
        "input_tokens": 20,
    }
