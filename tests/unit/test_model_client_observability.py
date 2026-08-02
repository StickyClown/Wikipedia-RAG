from __future__ import annotations

import httpx
import pytest

import wikipediarag.model_client as model_client
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
    assert metadata["safe_error_code"] == "provider_timeout"
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
