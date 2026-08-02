from __future__ import annotations

import asyncio
from typing import Any

import httpx

from wikipediarag.config import Settings, get_settings
from wikipediarag.observability import ModelGatewayError, elapsed_ms, model_call_metadata, now_ms, safe_error_code

MAX_PROVIDER_ATTEMPTS = 3
TRANSIENT_PROVIDER_STATUS_CODES = {408, 429, 500, 502, 503, 504}


async def chat_completion(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    *,
    alias: str = "generator_main",
    response_format: dict[str, Any] | None = None,
    max_provider_attempts: int = MAX_PROVIDER_ATTEMPTS,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    payload: dict[str, Any] = {"model": alias, "messages": messages, "stream": False}
    if response_format is not None:
        payload["response_format"] = response_format
    return await _post_json(
        f"{resolved.model_gateway_url.rstrip('/')}/v1/chat/completions",
        payload,
        timeout_seconds=120,
        max_attempts=max_provider_attempts,
        operation="chat",
        alias=alias,
    )


async def chat_completion_text(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    *,
    alias: str = "generator_main",
    response_format: dict[str, Any] | None = None,
) -> str:
    payload = await chat_completion(messages, settings, alias=alias, response_format=response_format)
    return str(payload["choices"][0]["message"]["content"])


async def embeddings(
    inputs: list[str],
    settings: Settings | None = None,
    *,
    alias: str = "embed_default",
    dimensions: int | None = None,
    query_instruction: str | None = None,
) -> tuple[list[list[float]], dict[str, Any]]:
    resolved = settings or get_settings()
    prepared = [f"{query_instruction}\n{text}" if query_instruction else text for text in inputs]
    request: dict[str, Any] = {"model": alias, "input": prepared}
    if dimensions is not None:
        request["dimensions"] = dimensions
    payload = await _post_json(
        f"{resolved.model_gateway_url.rstrip('/')}/v1/embeddings",
        request,
        timeout_seconds=150,
        operation="embedding",
        alias=alias,
    )
    usage = dict(payload.get("usage") or {})
    usage["_gateway_metadata"] = dict(payload.get("_gateway_metadata") or {})
    return [list(item["embedding"]) for item in payload["data"]], usage


async def rerank(
    query: str,
    documents: list[str],
    settings: Settings | None = None,
    *,
    alias: str = "rerank_default",
    top_n: int | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    return await _post_json(
        f"{resolved.model_gateway_url.rstrip('/')}/v1/rerank",
        {"model": alias, "query": query, "documents": documents, "top_n": top_n or len(documents)},
        timeout_seconds=120,
        operation="rerank",
        alias=alias,
    )


async def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    max_attempts: int = MAX_PROVIDER_ATTEMPTS,
    operation: str,
    alias: str,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    started = now_ms()
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(url, json=payload)
            response.raise_for_status()
            result = dict(response.json())
            result["_gateway_metadata"] = model_call_metadata(
                operation=operation,
                alias=alias,
                payload=result,
                latency_ms=elapsed_ms(started),
                attempts=attempt + 1,
            )
            return result
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            if attempt + 1 == max_attempts:
                raise ModelGatewayError(
                    "model gateway request failed",
                    metadata=model_call_metadata(
                        operation=operation,
                        alias=alias,
                        payload=None,
                        latency_ms=elapsed_ms(started),
                        attempts=attempt + 1,
                        safe_error_code=safe_error_code(exc),
                    ),
                    cause=exc,
                ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in TRANSIENT_PROVIDER_STATUS_CODES or attempt + 1 == max_attempts:
                raise ModelGatewayError(
                    "model gateway request failed",
                    metadata=model_call_metadata(
                        operation=operation,
                        alias=alias,
                        payload=None,
                        latency_ms=elapsed_ms(started),
                        attempts=attempt + 1,
                        safe_error_code=safe_error_code(exc),
                    ),
                    cause=exc,
                ) from exc
        await asyncio.sleep(2**attempt)
    raise RuntimeError("unreachable provider retry state")
