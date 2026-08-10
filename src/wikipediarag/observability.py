from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import httpx

from wikipediarag.config import Settings, get_settings
from wikipediarag.ids import stable_hash

ContentCaptureMode = Literal["off", "masked"]

CONTENT_KEYS = {
    "answer",
    "comment",
    "content",
    "documents",
    "input",
    "input_text",
    "messages",
    "normalized_query",
    "original_query",
    "prompt",
    "provider_payload",
    "query",
    "rewritten_query",
    "source_text",
    "text",
    "parent_text",
    "acl",
    "document_access",
    "provider_response",
}
STORAGE_KEY_MARKERS = ("object_key", "storage_key", "minio_key", "artifact_key")
MASK_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})|(\b\d{6,}\b)")


class ModelGatewayError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any], cause: Exception) -> None:
        super().__init__(message)
        self.metadata = metadata
        self.__cause__ = cause


def now_ms() -> float:
    return time.perf_counter()


def elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def content_policy(settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    return {
        "content_capture": resolved.telemetry_content_capture,
        "max_text_chars": resolved.telemetry_max_text_chars,
        "retention_days": resolved.telemetry_retention_days,
        "default_export": "ids_hashes_ranks_scores_timings_model_metadata_safe_statuses",
    }


def safe_text_projection(text: str, *, settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    value = str(text)
    payload: dict[str, Any] = {
        "hash": stable_hash(["telemetry_text", value], 32),
        "length_chars": len(value),
    }
    if resolved.telemetry_content_capture == "masked":
        masked = MASK_RE.sub("[REDACTED]", value)
        payload["masked_text"] = masked[: max(0, resolved.telemetry_max_text_chars)]
        payload["truncated"] = len(masked) > resolved.telemetry_max_text_chars
    return payload


def safe_telemetry_payload(payload: Any, *, settings: Settings | None = None) -> Any:
    if isinstance(payload, dict):
        output: dict[str, Any] = {}
        for key, value in payload.items():
            normalized_key = str(key).casefold()
            if normalized_key in CONTENT_KEYS or any(marker in normalized_key for marker in STORAGE_KEY_MARKERS):
                output[str(key)] = safe_text_projection(_stringify_content(value), settings=settings)
            else:
                output[str(key)] = safe_telemetry_payload(value, settings=settings)
        return output
    if isinstance(payload, list):
        return [safe_telemetry_payload(item, settings=settings) for item in payload]
    return payload


def safe_error_code(exc: Exception) -> str:
    explicit_code = getattr(exc, "safe_code", None)
    if isinstance(explicit_code, str) and explicit_code:
        return explicit_code
    domain_code = getattr(exc, "code", None)
    if isinstance(domain_code, str) and domain_code:
        return domain_code
    if isinstance(exc, ModelGatewayError):
        code = exc.metadata.get("safe_error_code")
        return str(code) if code else "INTERNAL_ERROR"
    if isinstance(exc, httpx.TimeoutException):
        return "DEPENDENCY_TIMEOUT"
    if isinstance(exc, httpx.NetworkError):
        return "DEPENDENCY_UNAVAILABLE"
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
            if isinstance(payload, dict):
                envelope = payload.get("error")
                if isinstance(envelope, dict):
                    code = envelope.get("code")
                    if isinstance(code, str) and code:
                        return code
                detail = payload.get("detail")
                if isinstance(detail, dict):
                    envelope = detail.get("error")
                    if isinstance(envelope, dict):
                        code = envelope.get("code")
                        if isinstance(code, str) and code:
                            return code
        except (ValueError, TypeError):
            pass
        return "DEPENDENCY_UNAVAILABLE" if exc.response.status_code in {429, 502, 503, 504} else "INTERNAL_ERROR"
    return "INTERNAL_ERROR"


def model_call_metadata(
    *,
    operation: str,
    alias: str,
    payload: dict[str, Any] | None,
    latency_ms: int,
    attempts: int,
    safe_error_code: str | None = None,
) -> dict[str, Any]:
    response = dict(payload or {})
    usage = dict(response.get("usage") or {})
    metadata: dict[str, Any] = {
        "operation": operation,
        "model_alias": alias,
        "provider": response.get("provider"),
        "provider_model": response.get("model"),
        "provider_request_id": response.get("id"),
        "latency_ms": latency_ms,
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "usage": usage,
    }
    if safe_error_code:
        metadata["safe_error_code"] = safe_error_code
    reasoning_tokens = _reasoning_tokens(usage)
    if reasoning_tokens is not None:
        metadata["reasoning_tokens"] = reasoning_tokens
    summary = response.get("reasoning_summary")
    if isinstance(summary, str) and summary:
        metadata["reasoning_summary"] = safe_text_projection(summary)
    return metadata


@contextmanager
def retrieval_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    try:
        from opentelemetry import trace as otel_trace
    except Exception:
        yield
        return
    tracer = otel_trace.get_tracer("wikipediarag.retrieval")
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if isinstance(value, str | bool | int | float):
                span.set_attribute(key, value)
        yield


def _stringify_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_stringify_content(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key}={_stringify_content(item)}" for key, item in value.items())
    return str(value)


def _reasoning_tokens(usage: dict[str, Any]) -> int | None:
    for key in ("reasoning_tokens", "reasoning_token_count"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
        return int(details["reasoning_tokens"])
    return None
