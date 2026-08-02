from __future__ import annotations

from types import TracebackType

import httpx
import pytest

import wikipediarag.cli as cli
from wikipediarag.config import Settings


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


def test_eval_release_gate_runs_openrouter_smoke_before_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def ready(_api: str) -> httpx.Response:
        calls.append(("ready", _api))
        request = httpx.Request("GET", "http://api.test/ready")
        return httpx.Response(200, request=request, json={"status": "ok"})

    def smoke(gateway: str, provider: str) -> None:
        calls.append(("smoke", f"{provider}:{gateway}"))

    async def gate(**_kwargs: object) -> dict[str, object]:
        calls.append(("gate", "started"))
        return {"passed": True}

    monkeypatch.setattr(cli, "_require_api_ready", ready)
    monkeypatch.setattr(cli, "smoke_models", smoke)
    monkeypatch.setattr("wikipediarag.config.get_settings", lambda: Settings(model_provider="openrouter"))
    monkeypatch.setattr("wikipediarag.eval.commands.eval_release_gate", gate)

    cli.run_eval_release_gate("reviewed-wikipedia-smoke-v1", "http://api.test")

    assert calls == [
        ("ready", "http://api.test"),
        ("smoke", "openrouter:http://localhost:8081"),
        ("gate", "started"),
    ]


def test_eval_release_gate_skips_provider_smoke_for_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def ready(_api: str) -> httpx.Response:
        request = httpx.Request("GET", "http://api.test/ready")
        return httpx.Response(200, request=request, json={"status": "ok"})

    def smoke(_gateway: str, _provider: str) -> None:
        calls.append("smoke")

    async def gate(**_kwargs: object) -> dict[str, object]:
        calls.append("gate")
        return {"passed": True}

    monkeypatch.setattr(cli, "_require_api_ready", ready)
    monkeypatch.setattr(cli, "smoke_models", smoke)
    monkeypatch.setattr("wikipediarag.config.get_settings", lambda: Settings(model_provider="mock"))
    monkeypatch.setattr("wikipediarag.eval.commands.eval_release_gate", gate)

    cli.run_eval_release_gate("reviewed-wikipedia-smoke-v1", "http://api.test")

    assert calls == ["gate"]


def test_eval_release_gate_refuses_to_start_when_openrouter_smoke_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def ready(_api: str) -> httpx.Response:
        request = httpx.Request("GET", "http://api.test/ready")
        return httpx.Response(200, request=request, json={"status": "ok"})

    def smoke(_gateway: str, _provider: str) -> None:
        calls.append("smoke")
        raise SystemExit("model gateway aliases are unhealthy")

    async def gate(**_kwargs: object) -> dict[str, object]:
        calls.append("gate")
        return {"passed": True}

    monkeypatch.setattr(cli, "_require_api_ready", ready)
    monkeypatch.setattr(cli, "smoke_models", smoke)
    monkeypatch.setattr("wikipediarag.config.get_settings", lambda: Settings(model_provider="openrouter"))
    monkeypatch.setattr("wikipediarag.eval.commands.eval_release_gate", gate)

    with pytest.raises(SystemExit, match="model gateway aliases are unhealthy"):
        cli.run_eval_release_gate("reviewed-wikipedia-smoke-v1", "http://api.test")

    assert calls == ["smoke"]
