from __future__ import annotations

from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException

from wikipediarag.config import get_settings

app = FastAPI(title="WikipediaRag Model Gateway")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    settings = get_settings()
    provider = settings.model_provider
    return {
        "object": "list",
        "data": [
            {"id": "generator_fast", "provider": provider, "capabilities": ["chat"]},
            {"id": "embed_default", "provider": provider, "capabilities": ["embeddings"]},
            {"id": "rerank_default", "provider": provider, "capabilities": ["rerank"]},
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy("chat/completions", payload)


@app.post("/v1/embeddings")
async def create_embeddings(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy("embeddings", payload)


@app.post("/v1/rerank")
async def create_rerank(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy("rerank", payload)


async def proxy(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if settings.model_provider == "mock":
        base_url = settings.mock_provider_url.rstrip("/")
        url = f"{base_url}/v1/{path}"
        headers: dict[str, str] = {}
    elif settings.model_provider == "openrouter":
        if not settings.openrouter_api_key:
            raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured")
        base_url = settings.openrouter_base_url.rstrip("/")
        url = f"{base_url}/{path}"
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    else:
        raise HTTPException(status_code=503, detail=f"unsupported model provider {settings.model_provider}")

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="model provider request failed")
    return dict(response.json())


def main() -> None:
    uvicorn.run("wikipediarag.gateway_app:app", host="0.0.0.0", port=8080)  # noqa: S104


if __name__ == "__main__":
    main()
