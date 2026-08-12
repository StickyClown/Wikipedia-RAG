from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil
from typing import Any

import httpx

from wikipediarag.config import Settings, get_settings
from wikipediarag.observability import ModelGatewayError, elapsed_ms, model_call_metadata, now_ms, safe_error_code
from wikipediarag.reliability import OperationDeadline

MAX_PROVIDER_ATTEMPTS = 2
TRANSIENT_PROVIDER_STATUS_CODES = {429, 502, 503, 504}
_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_FACTORY: object | None = None


def _http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT, _HTTP_CLIENT_FACTORY
    if (
        _HTTP_CLIENT is None
        or _HTTP_CLIENT_FACTORY is not httpx.AsyncClient
        or bool(getattr(_HTTP_CLIENT, "is_closed", False))
    ):
        _HTTP_CLIENT = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0),
        )
        _HTTP_CLIENT_FACTORY = httpx.AsyncClient
    return _HTTP_CLIENT


def _gateway_error_details(response: httpx.Response) -> tuple[str, bool]:
    """Read only the Model Gateway's safe error metadata."""

    fallback_retryable = response.status_code in TRANSIENT_PROVIDER_STATUS_CODES
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return "DEPENDENCY_UNAVAILABLE", fallback_retryable
    detail = payload.get("detail", payload) if isinstance(payload, dict) else None
    error = detail.get("error", detail) if isinstance(detail, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    retryable = error.get("retryable") if isinstance(error, dict) else None
    return (
        str(code) if isinstance(code, str) and code else "DEPENDENCY_UNAVAILABLE",
        bool(retryable) if isinstance(retryable, bool) else fallback_retryable,
    )


def _request_budget_metadata(
    payload: dict[str, Any], retry_max_output_tokens: int | None, *, requested_tokens: int | None = None
) -> dict[str, int]:
    requested = requested_tokens if isinstance(requested_tokens, int) else payload.get("max_tokens")
    if not isinstance(requested, int):
        return {}
    cap = max(int(requested), int(retry_max_output_tokens or requested))
    return {
        "requested_output_tokens": int(requested),
        "effective_output_tokens": int(requested),
        "stage_output_cap": cap,
        "retry_count": 0,
    }


async def close_http_client() -> None:
    global _HTTP_CLIENT, _HTTP_CLIENT_FACTORY
    if _HTTP_CLIENT is not None:
        close = getattr(_HTTP_CLIENT, "aclose", None)
        if close is not None:
            await close()
        _HTTP_CLIENT = None
        _HTTP_CLIENT_FACTORY = None


async def chat_completion(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    *,
    alias: str = "generator_main",
    stage: str | None = None,
    config_revision_id: str | None = None,
    response_format: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
    retry_max_output_tokens: int | None = None,
    stage_output_cap: int | None = None,
    stage_safety_reserve_tokens: int | None = None,
    max_provider_attempts: int | None = None,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    resolved = settings or get_settings()
    payload: dict[str, Any] = {"messages": messages, "stream": False}
    if stage:
        payload["stage"] = stage
        if config_revision_id:
            payload["config_revision_id"] = config_revision_id
    else:
        payload["model"] = alias
    if response_format is not None:
        payload["response_format"] = response_format
    if max_output_tokens is not None:
        payload["max_tokens"] = int(max_output_tokens)
    if stage_output_cap is not None:
        payload["_stage_output_cap"] = int(stage_output_cap)
    if stage_safety_reserve_tokens is not None:
        payload["_stage_safety_reserve"] = int(stage_safety_reserve_tokens)
    request_options: dict[str, Any] = {
        "timeout_seconds": resolved.model_client_chat_timeout_seconds,
        "max_attempts": (
            max(1, int(max_provider_attempts))
            if max_provider_attempts is not None
            else max(1, int(resolved.safe_external_retry_attempts))
        ),
        "operation": "chat",
        "alias": stage or alias,
        "retry_max_output_tokens": retry_max_output_tokens,
    }
    if deadline is not None:
        request_options["deadline"] = deadline
    if correlation_id:
        request_options["correlation_id"] = correlation_id
    return await _post_json(
        f"{resolved.model_gateway_url.rstrip('/')}/v1/chat/completions",
        payload,
        **request_options,
    )


async def chat_completion_text(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    *,
    alias: str = "generator_main",
    stage: str | None = None,
    config_revision_id: str | None = None,
    response_format: dict[str, Any] | None = None,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> str:
    payload = await chat_completion(
        messages,
        settings,
        alias=alias,
        stage=stage,
        config_revision_id=config_revision_id,
        response_format=response_format,
        deadline=deadline,
        correlation_id=correlation_id,
    )
    return str(payload["choices"][0]["message"]["content"])


async def embeddings(
    inputs: list[str],
    settings: Settings | None = None,
    *,
    alias: str = "embed_default",
    stage: str | None = None,
    config_revision_id: str | None = None,
    dimensions: int | None = None,
    query_instruction: str | None = None,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> tuple[list[list[float]], dict[str, Any]]:
    resolved = settings or get_settings()
    prepared = [f"{query_instruction}\n{text}" if query_instruction else text for text in inputs]
    request: dict[str, Any] = {"input": prepared}
    if stage:
        request["stage"] = stage
        if config_revision_id:
            request["config_revision_id"] = config_revision_id
    else:
        request["model"] = alias
    if dimensions is not None:
        request["dimensions"] = dimensions
    request_options: dict[str, Any] = {
        "timeout_seconds": resolved.model_client_embedding_timeout_seconds,
        "max_attempts": max(1, int(resolved.safe_external_retry_attempts)),
        "operation": "embedding",
        "alias": stage or alias,
    }
    if deadline is not None:
        request_options["deadline"] = deadline
    if correlation_id:
        request_options["correlation_id"] = correlation_id
    payload = await _post_json(
        f"{resolved.model_gateway_url.rstrip('/')}/v1/embeddings",
        request,
        **request_options,
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
    stage: str | None = None,
    config_revision_id: str | None = None,
    top_n: int | None = None,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    resolved = settings or get_settings()
    request_options: dict[str, Any] = {
        "timeout_seconds": resolved.model_client_rerank_timeout_seconds,
        "max_attempts": max(1, int(resolved.safe_external_retry_attempts)),
        "operation": "rerank",
        "alias": alias,
    }
    if deadline is not None:
        request_options["deadline"] = deadline
    if correlation_id:
        request_options["correlation_id"] = correlation_id
    return await _post_json(
        f"{resolved.model_gateway_url.rstrip('/')}/v1/rerank",
        {
            **({"stage": stage, "config_revision_id": config_revision_id} if stage else {"model": alias}),
            "query": query,
            "documents": documents,
            "top_n": top_n or len(documents),
        },
        **request_options,
    )


async def count_tokens(
    text: str,
    settings: Settings | None = None,
    *,
    alias: str = "generator_main",
    stage: str | None = None,
    config_revision_id: str | None = None,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Ask Model Gateway for a safe token count using its configured tokenizer."""
    resolved = settings or get_settings()
    request_options: dict[str, Any] = {
        "timeout_seconds": resolved.model_client_chat_timeout_seconds,
        "max_attempts": 1,
        "operation": "tokenize",
        "alias": stage or alias,
    }
    if deadline is not None:
        request_options["deadline"] = deadline
    if correlation_id:
        request_options["correlation_id"] = correlation_id
    return await _post_json(
        f"{resolved.model_gateway_url.rstrip('/')}/v1/tokenize",
        ({"stage": stage, "config_revision_id": config_revision_id} if stage else {"model": alias}) | {"text": text},
        **request_options,
    )


async def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    max_attempts: int = MAX_PROVIDER_ATTEMPTS,
    operation: str,
    alias: str,
    retry_max_output_tokens: int | None = None,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    started = now_ms()
    initial_output_tokens = payload.get("max_tokens") if isinstance(payload.get("max_tokens"), int) else None
    for attempt in range(max_attempts):
        try:
            effective_timeout = (
                deadline.timeout_seconds(timeout_seconds, stage=f"model_gateway.{operation}")
                if deadline is not None
                else timeout_seconds
            )
            headers = {"X-Request-ID": correlation_id} if correlation_id else None
            response = await _http_client().post(url, json=payload, headers=headers, timeout=effective_timeout)
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
                metadata = model_call_metadata(
                    operation=operation,
                    alias=alias,
                    payload=None,
                    latency_ms=elapsed_ms(started),
                    attempts=attempt + 1,
                    safe_error_code=safe_error_code(exc),
                )
                budget_metadata = _request_budget_metadata(
                    payload, retry_max_output_tokens, requested_tokens=initial_output_tokens
                )
                if budget_metadata:
                    metadata["output_budget"] = budget_metadata
                raise ModelGatewayError(
                    "model gateway request failed",
                    metadata=metadata,
                    cause=exc,
                ) from exc
        except httpx.HTTPStatusError as exc:
            response_code, retryable = _gateway_error_details(exc.response)
            if not retryable or attempt + 1 == max_attempts:
                metadata = model_call_metadata(
                    operation=operation,
                    alias=alias,
                    payload=None,
                    latency_ms=elapsed_ms(started),
                    attempts=attempt + 1,
                    safe_error_code=response_code,
                )
                budget_metadata = _request_budget_metadata(
                    payload, retry_max_output_tokens, requested_tokens=initial_output_tokens
                )
                if budget_metadata:
                    metadata["output_budget"] = budget_metadata
                raise ModelGatewayError(
                    "model gateway request failed",
                    metadata=metadata,
                    cause=exc,
                ) from exc
            retry_after = _retry_after_seconds(exc.response)
            if response_code == "MODEL_OUTPUT_TRUNCATED" and isinstance(payload.get("max_tokens"), int):
                retry_limit = retry_max_output_tokens or int(payload["max_tokens"])
                payload["max_tokens"] = min(
                    max(int(payload["max_tokens"]) + 1, int(payload["max_tokens"]) * 2),
                    max(int(payload["max_tokens"]), int(retry_limit)),
                )
            if retry_after is not None:
                await _bounded_retry_sleep(
                    retry_after, timeout_seconds=timeout_seconds, deadline=deadline, operation=operation
                )
                continue
        await _bounded_retry_sleep(2**attempt, timeout_seconds=timeout_seconds, deadline=deadline, operation=operation)
    raise RuntimeError("unreachable provider retry state")


async def _bounded_retry_sleep(
    requested_seconds: float,
    *,
    timeout_seconds: float,
    deadline: OperationDeadline | None,
    operation: str,
) -> None:
    delay = min(max(0.0, requested_seconds), timeout_seconds)
    if deadline is not None:
        delay = min(delay, deadline.timeout_seconds(delay or 0.001, stage=f"model_gateway.{operation}.retry"))
    await asyncio.sleep(delay)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw_value = response.headers.get("Retry-After")
    if not raw_value:
        return None
    value = raw_value.strip()
    if not value:
        return None
    if value.isdigit():
        return max(0.0, float(value))
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if retry_at.tzinfo is None:
        return None
    seconds = retry_at.timestamp() - datetime.now(UTC).timestamp()
    return max(0.0, float(ceil(seconds)))
