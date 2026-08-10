from __future__ import annotations

import httpx
import pytest

import wikipediarag.model_client as model_client
from wikipediarag.config import Settings
from wikipediarag.observability import ModelGatewayError


class _FailingAsyncClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def __aenter__(self) -> _FailingAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.TimeoutException("provider timeout with raw payload")


class _RetryThenOkAsyncClient:
    calls = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def __aenter__(self) -> _RetryThenOkAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        type(self).calls += 1
        if type(self).calls == 1:
            raise httpx.NetworkError("temporary network issue")
        request = httpx.Request("POST", "http://gateway.test/v1/chat/completions")
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "provider-request",
                "model": "provider-model",
                "provider": "mock",
                "usage": {"total_tokens": 3},
                "choices": [{"message": {"content": "{}"}}],
            },
        )


class _RetryAfterThenOkAsyncClient:
    calls = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def __aenter__(self) -> _RetryAfterThenOkAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        type(self).calls += 1
        request = httpx.Request("POST", "http://gateway.test/v1/chat/completions")
        if type(self).calls == 1:
            return httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "7"},
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "provider-request",
                "model": "provider-model",
                "provider": "mock",
                "usage": {"total_tokens": 3},
                "choices": [{"message": {"content": "{}"}}],
            },
        )


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_model_gateway_failure_metadata_has_safe_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wikipediarag.model_client.httpx.AsyncClient", _FailingAsyncClient)
    monkeypatch.setattr("wikipediarag.model_client.asyncio.sleep", _no_sleep)

    with pytest.raises(ModelGatewayError) as exc_info:
        await model_client._post_json(
            "http://gateway.test/v1/chat/completions",
            {"model": "alias", "messages": [{"role": "user", "content": "secret"}]},
            timeout_seconds=1,
            max_attempts=2,
            operation="chat",
            alias="alias",
        )

    metadata = exc_info.value.metadata
    assert metadata["safe_error_code"] == "DEPENDENCY_TIMEOUT"
    assert metadata["attempts"] == 2
    assert metadata["retries"] == 1
    assert "secret" not in str(metadata)


@pytest.mark.asyncio
async def test_model_gateway_success_metadata_records_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _RetryThenOkAsyncClient.calls = 0
    monkeypatch.setattr("wikipediarag.model_client.httpx.AsyncClient", _RetryThenOkAsyncClient)
    monkeypatch.setattr("wikipediarag.model_client.asyncio.sleep", _no_sleep)

    payload = await model_client._post_json(
        "http://gateway.test/v1/chat/completions",
        {"model": "alias", "messages": []},
        timeout_seconds=1,
        max_attempts=2,
        operation="chat",
        alias="alias",
    )

    metadata = payload["_gateway_metadata"]
    assert metadata == {
        "operation": "chat",
        "model_alias": "alias",
        "provider": "mock",
        "provider_model": "provider-model",
        "provider_request_id": "provider-request",
        "latency_ms": metadata["latency_ms"],
        "attempts": 2,
        "retries": 1,
        "usage": {"total_tokens": 3},
    }


@pytest.mark.asyncio
async def test_model_gateway_honors_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        recorded_sleeps.append(seconds)

    _RetryAfterThenOkAsyncClient.calls = 0
    monkeypatch.setattr("wikipediarag.model_client.httpx.AsyncClient", _RetryAfterThenOkAsyncClient)
    monkeypatch.setattr("wikipediarag.model_client.asyncio.sleep", record_sleep)

    payload = await model_client._post_json(
        "http://gateway.test/v1/chat/completions",
        {"model": "alias", "messages": []},
        timeout_seconds=1,
        max_attempts=2,
        operation="chat",
        alias="alias",
    )

    assert payload["_gateway_metadata"]["attempts"] == 2
    assert recorded_sleeps == [1.0]


@pytest.mark.asyncio
async def test_chat_completion_uses_configured_gateway_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post_json(*_args: object, **kwargs: object) -> dict[str, object]:
        captured["timeout_seconds"] = kwargs["timeout_seconds"]
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(model_client, "_post_json", fake_post_json)

    await model_client.chat_completion(
        [{"role": "user", "content": "{}"}],
        Settings(model_client_chat_timeout_seconds=321),
        alias="generator_fast",
    )

    assert captured["timeout_seconds"] == 321


@pytest.mark.asyncio
async def test_count_tokens_uses_gateway_tokenize_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post_json(url: str, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        captured["url"] = url
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {"object": "tokenization", "input_tokens": 42}

    monkeypatch.setattr(model_client, "_post_json", fake_post_json)

    payload = await model_client.count_tokens(
        "example text",
        Settings(model_gateway_url="http://gateway.test"),
        alias="generator_fast",
    )

    assert payload["input_tokens"] == 42
    assert captured["url"] == "http://gateway.test/v1/tokenize"
    assert captured["payload"] == {"model": "generator_fast", "text": "example text"}
    assert captured["kwargs"] == {
        "timeout_seconds": 300.0,
        "max_attempts": 1,
        "operation": "tokenize",
        "alias": "generator_fast",
    }
