from __future__ import annotations

from types import TracebackType

import httpx
import pytest

import wikipediarag.cli as cli


class _DegradedReadyClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def __enter__(self) -> _DegradedReadyClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def get(self, _url: str) -> httpx.Response:
        request = httpx.Request("GET", "http://api.test/ready")
        return httpx.Response(
            200,
            request=request,
            json={
                "status": "degraded",
                "components": {
                    "postgres": "ok",
                    "model_gateway": "failed",
                },
            },
        )


class _UnhealthyModelClient:
    post_called = False

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def __enter__(self) -> _UnhealthyModelClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def get(self, _url: str) -> httpx.Response:
        request = httpx.Request("GET", "http://gateway.test/v1/models")
        aliases = {
            "embed_default",
            "generator_fast",
            "generator_main",
            "verifier",
            "rerank_default",
        }
        return httpx.Response(
            200,
            request=request,
            json={
                "object": "list",
                "data": [{"id": alias, "healthy": False} for alias in sorted(aliases)],
            },
        )

    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        self.post_called = True
        request = httpx.Request("POST", "http://gateway.test/v1/embeddings")
        return httpx.Response(200, request=request, json={})


def test_eval_release_gate_refuses_to_start_when_api_ready_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "Client", _DegradedReadyClient)

    with pytest.raises(SystemExit, match="API is not ready"):
        cli.run_eval_release_gate("reviewed-wikipedia-smoke-v1", "http://api.test")


def test_smoke_models_refuses_unhealthy_aliases_before_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _UnhealthyModelClient()
    monkeypatch.setattr(httpx, "Client", lambda *_args, **_kwargs: client)

    with pytest.raises(SystemExit, match="model gateway aliases are unhealthy"):
        cli.smoke_models("http://gateway.test", "openrouter")

    assert client.post_called is False
