from __future__ import annotations

import httpx

from wikipediarag.config import Settings, get_settings


async def chat_completion(messages: list[dict[str, str]], settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{resolved.model_gateway_url.rstrip('/')}/v1/chat/completions",
            json={"model": "generator_fast", "messages": messages, "stream": False},
        )
        response.raise_for_status()
        payload = response.json()
    return str(payload["choices"][0]["message"]["content"])


async def embeddings(inputs: list[str], settings: Settings | None = None) -> list[list[float]]:
    resolved = settings or get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{resolved.model_gateway_url.rstrip('/')}/v1/embeddings",
            json={"model": "embed_default", "input": inputs},
        )
        response.raise_for_status()
        payload = response.json()
    return [list(item["embedding"]) for item in payload["data"]]
