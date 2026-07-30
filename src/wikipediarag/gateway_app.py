from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from wikipediarag.config import Settings, get_settings
from wikipediarag.model_registry import ModelAlias, ModelOperation, get_model_registry
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile

app = FastAPI(title="WikipediaRag Model Gateway")


@dataclass
class ReadinessCheck:
    component: str
    status: str
    reason: str


@dataclass
class GatewayReadinessState:
    status: str = "ok"
    checks: list[ReadinessCheck] = field(default_factory=list)


_readiness_state = GatewayReadinessState()


@app.on_event("startup")
async def startup_smoke() -> None:
    global _readiness_state

    settings = get_settings()
    profile = get_retrieval_profile(settings=settings)
    _readiness_state = GatewayReadinessState()
    smoke_mode = settings.model_gateway_startup_smoke
    if smoke_mode == "off":
        return
    if profile.requires_real_provider and not settings.openrouter_api_key:
        failure = ReadinessCheck(
            component="openrouter.startup_smoke",
            status="failed",
            reason="openrouter_api_key_missing",
        )
        _readiness_state = GatewayReadinessState(status="degraded", checks=[failure])
        if smoke_mode == "required":
            raise RuntimeError("OPENROUTER_API_KEY is required for sota_mvp")
        return
    if profile.requires_real_provider:
        try:
            await _openrouter_startup_smoke(settings, profile)
        except Exception as exc:
            failure = ReadinessCheck(
                component="openrouter.startup_smoke",
                status="failed",
                reason=_safe_smoke_failure_reason(exc),
            )
            _readiness_state = GatewayReadinessState(status="degraded", checks=[failure])
            if smoke_mode == "required":
                raise


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    return {
        "status": _readiness_state.status,
        "checks": [
            {
                "component": check.component,
                "status": check.status,
                "reason": check.reason,
            }
            for check in _readiness_state.checks
        ],
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    settings = get_settings()
    registry = get_model_registry(settings)
    return {
        "object": "list",
        "data": [
            {
                "id": alias,
                "provider": model.provider,
                "operation": model.operation,
                "dimensions": model.dimensions,
                "healthy": _alias_available(model),
            }
            for alias, model in sorted(registry.models.items())
        ],
    }


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(payload: dict[str, Any]) -> Any:
    if payload.get("stream") is True:
        return await proxy_stream("chat/completions", payload)
    return await proxy("chat/completions", payload)


@app.post("/v1/embeddings")
async def create_embeddings(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy("embeddings", payload)


@app.post("/v1/rerank")
async def create_rerank(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy("rerank", payload)


async def proxy(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    model_alias = str(payload.get("model") or "")
    alias = get_model_registry(settings).require(model_alias, _operation_for_path(path))
    provider_payload = {**payload, "model": alias.model}
    if alias.dimensions is not None and path == "embeddings":
        provider_payload.setdefault("dimensions", alias.dimensions)

    if alias.provider == "mock":
        base_url = settings.mock_provider_url.rstrip("/")
        url = f"{base_url}/v1/{path}"
        headers: dict[str, str] = {}
    elif alias.provider == "openrouter":
        if not settings.openrouter_api_key:
            raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured")
        base_url = settings.openrouter_base_url.rstrip("/")
        url = f"{base_url}/{path}"
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    else:
        raise HTTPException(status_code=503, detail=f"unsupported model provider {alias.provider}")

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(url, json=provider_payload, headers=headers)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="model provider request failed")
    result = dict(response.json())
    result.setdefault("model_alias", model_alias)
    result.setdefault("provider", alias.provider)
    return result


async def proxy_stream(path: str, payload: dict[str, Any]) -> StreamingResponse:
    settings = get_settings()
    model_alias = str(payload.get("model") or "")
    alias = get_model_registry(settings).require(model_alias, _operation_for_path(path))
    provider_payload = {**payload, "model": alias.model}
    if alias.provider == "mock":
        base_url = settings.mock_provider_url.rstrip("/")
        url = f"{base_url}/v1/{path}"
        headers: dict[str, str] = {}
    elif alias.provider == "openrouter":
        if not settings.openrouter_api_key:
            raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured")
        base_url = settings.openrouter_base_url.rstrip("/")
        url = f"{base_url}/{path}"
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    else:
        raise HTTPException(status_code=503, detail=f"unsupported model provider {alias.provider}")

    async def stream() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=provider_payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise HTTPException(status_code=502, detail="model provider stream failed")
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream")


def _operation_for_path(path: str) -> ModelOperation:
    if path == "chat/completions":
        return "chat"
    if path == "embeddings":
        return "embedding"
    if path == "rerank":
        return "rerank"
    raise HTTPException(status_code=404, detail="unknown model gateway path")


def _alias_available(alias: ModelAlias) -> bool:
    settings = get_settings()
    if alias.provider == "mock":
        return True
    if alias.provider == "openrouter":
        return bool(settings.openrouter_api_key) and not _provider_degraded("openrouter")
    return False


def _provider_degraded(provider: str) -> bool:
    prefix = f"{provider}."
    return any(check.component.startswith(prefix) and check.status != "ok" for check in _readiness_state.checks)


def _safe_smoke_failure_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"provider_http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "provider_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "provider_network_error"
    message = str(exc)
    if message.startswith("OpenRouter catalog does not list required models"):
        return "catalog_missing_required_models"
    if "embedding returned" in message:
        return "embedding_dimensions_mismatch"
    if "chat streaming returned" in message:
        return "chat_streaming_smoke_failed"
    if "rerank did not return ordered results" in message:
        return "rerank_ordering_smoke_failed"
    return "provider_smoke_failed"


async def _openrouter_startup_smoke(settings: Settings, profile: RetrievalProfile) -> None:
    registry = get_model_registry(settings)
    alias_names: dict[str, ModelOperation] = {
        profile.model_aliases.embed: "embedding",
        profile.model_aliases.generator_fast: "chat",
        profile.model_aliases.generator_main: "chat",
        profile.model_aliases.verifier: "chat",
        profile.model_aliases.rerank: "rerank",
    }
    aliases = {name: registry.require(name, operation) for name, operation in alias_names.items()}
    if any(alias.provider != "openrouter" for alias in aliases.values()):
        raise RuntimeError("sota_mvp requires OpenRouter-backed aliases")

    base_url = settings.openrouter_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    async with httpx.AsyncClient(timeout=120) as client:
        models = await client.get(f"{base_url}/models?output_modalities=all", headers=headers)
        models.raise_for_status()
        catalog_ids = {str(item.get("id")) for item in models.json().get("data", [])}
        missing_models = sorted({alias.model for alias in aliases.values()} - catalog_ids)
        if missing_models:
            raise RuntimeError(f"OpenRouter catalog does not list required models: {missing_models}")

        embed_alias = aliases[profile.model_aliases.embed]
        dimensions = int(embed_alias.dimensions or 0)
        embedding = await client.post(
            f"{base_url}/embeddings",
            json={
                "model": embed_alias.model,
                "input": ["Россия - государство"],
                "dimensions": dimensions,
            },
            headers=headers,
        )
        embedding.raise_for_status()
        vector = embedding.json()["data"][0]["embedding"]
        if len(vector) != dimensions:
            raise RuntimeError(f"OpenRouter embedding returned {len(vector)} dimensions, expected {dimensions}")

        fast_alias = aliases[profile.model_aliases.generator_fast]
        typed = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": fast_alias.model,
                "messages": [{"role": "user", "content": 'Верни JSON {"ok": true}'}],
                "response_format": {"type": "json_object"},
                "stream": False,
            },
            headers=headers,
        )
        typed.raise_for_status()
        json.loads(str(typed.json()["choices"][0]["message"]["content"]))

        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            json={
                "model": fast_alias.model,
                "messages": [{"role": "user", "content": "Ответь одним словом: ok"}],
                "stream": True,
            },
            headers=headers,
        ) as response:
            response.raise_for_status()
            stream_iter = response.aiter_bytes()
            try:
                first_chunk = await anext(stream_iter)
            except StopAsyncIteration as exc:
                raise RuntimeError("OpenRouter chat streaming returned no chunks") from exc
            if not first_chunk:
                raise RuntimeError("OpenRouter chat streaming returned an empty first chunk")

        rerank_alias = aliases[profile.model_aliases.rerank]
        rerank = await client.post(
            f"{base_url}/rerank",
            json={
                "model": rerank_alias.model,
                "query": "столица Франции",
                "documents": ["Париж - столица Франции.", "Берлин - столица Германии."],
                "top_n": 2,
            },
            headers=headers,
        )
        rerank.raise_for_status()
        results = rerank.json().get("results", [])
        if len(results) != 2 or float(results[0]["relevance_score"]) < float(results[-1]["relevance_score"]):
            raise RuntimeError("OpenRouter rerank did not return ordered results")


def main() -> None:
    uvicorn.run("wikipediarag.gateway_app:app", host="0.0.0.0", port=8080)  # noqa: S104


if __name__ == "__main__":
    main()
