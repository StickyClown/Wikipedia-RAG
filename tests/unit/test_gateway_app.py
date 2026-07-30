from __future__ import annotations

from typing import Literal

import httpx
import pytest

import wikipediarag.gateway_app as gateway_app
from wikipediarag.config import Settings


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
                "reason": "provider_http_403",
            }
        ],
    }


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
async def test_gateway_warn_mode_reports_missing_key_without_fatal_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_app, "get_settings", lambda: _settings(startup_smoke="warn", api_key=""))

    await gateway_app.startup_smoke()

    ready = await gateway_app.ready()
    assert ready["status"] == "degraded"
    assert ready["checks"][0]["reason"] == "openrouter_api_key_missing"
