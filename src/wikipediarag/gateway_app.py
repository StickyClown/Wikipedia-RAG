from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from wikipediarag.answering import ANSWER_JSON_SCHEMA
from wikipediarag.config import Settings, get_settings, resolve_openrouter_api_key
from wikipediarag.db import connect
from wikipediarag.model_control_repository import active_revision, get_revision, list_models
from wikipediarag.model_registry import (
    ModelAlias,
    ModelOperation,
    get_model_registry,
    validate_deep_research_model_contract,
)
from wikipediarag.oidc_service import decrypt_server_tokens
from wikipediarag.reliability import DependencyCircuit, DependencyCircuitOpen
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


class StructuredSmokeError(RuntimeError):
    """The provider answered the canary but violated the structured contract."""

    safe_code = "MODEL_ALIAS_UNREADY"

    def __init__(self, alias: str, message: str) -> None:
        super().__init__(message)
        self.alias = alias


_readiness_state = GatewayReadinessState()
_dependency_circuits: dict[str, DependencyCircuit] = {}
_structured_alias_unready: dict[str, str] = {}
_structured_schema_invalid_streak: dict[str, int] = {}

# Keep the startup canary on the exact production generator contract. A tiny
# `{ok: true}` schema would only prove that the provider supports JSON mode,
# not that it can satisfy the grounded answer shape used by Chat/RAG.
_STRUCTURED_SMOKE_SCHEMA: dict[str, Any] = dict(ANSWER_JSON_SCHEMA["json_schema"]["schema"])
_STRUCTURED_SMOKE_MAX_TOKENS = 64


def _structured_smoke_request(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Верни строго JSON по схеме grounded_answer. "
                    "В контексте нет доказательств, поэтому используй "
                    "insufficient_evidence=true и claims=[]."
                ),
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "gateway_smoke", "strict": True, "schema": _STRUCTURED_SMOKE_SCHEMA},
        },
        # Qwen 3.6 enables reasoning by default. The bounded canary checks the
        # structured answer contract, so spending its entire output budget on
        # hidden reasoning would create a false readiness failure.
        "reasoning": {"effort": "none"},
        "max_tokens": _STRUCTURED_SMOKE_MAX_TOKENS,
        "stream": False,
    }


@app.on_event("startup")
async def startup_smoke() -> None:
    global _readiness_state

    settings = get_settings()
    _structured_alias_unready.clear()
    _structured_schema_invalid_streak.clear()
    profile = get_retrieval_profile(settings=settings)
    try:
        validate_deep_research_model_contract(profile, get_model_registry(settings))
    except ValueError as exc:
        failure = ReadinessCheck(
            component="deep_research.model_contract",
            status="failed",
            reason="deep_research_model_context_contract_invalid",
        )
        _readiness_state = GatewayReadinessState(status="degraded", checks=[failure])
        if settings.model_gateway_startup_smoke == "required":
            raise RuntimeError("Deep Research model context contract is invalid") from exc
        return
    _readiness_state = GatewayReadinessState()
    smoke_mode = settings.model_gateway_startup_smoke
    if smoke_mode == "off":
        return
    active_control_plane = await _active_control_plane_readiness(settings)
    if active_control_plane is not None:
        _readiness_state = GatewayReadinessState(
            status="ok" if active_control_plane else "degraded",
            checks=[]
            if active_control_plane
            else [
                ReadinessCheck(
                    component="model_control_plane.active_revision",
                    status="failed",
                    reason="active_revision_validation_failed",
                )
            ],
        )
        return
    try:
        openrouter_api_key = resolve_openrouter_api_key(settings)
    except RuntimeError:
        failure = ReadinessCheck(
            component="openrouter.startup_smoke",
            status="failed",
            reason="openrouter_api_key_file_unreadable",
        )
        _readiness_state = GatewayReadinessState(status="degraded", checks=[failure])
        if smoke_mode == "required":
            raise
        return
    if profile.requires_real_provider and not openrouter_api_key:
        failure = ReadinessCheck(
            component="openrouter.startup_smoke",
            status="failed",
            reason="openrouter_api_key_missing",
        )
        _readiness_state = GatewayReadinessState(status="degraded", checks=[failure])
        if smoke_mode == "required":
            raise RuntimeError("OPENROUTER_API_KEY is required for real-provider retrieval profiles")
        return
    if profile.requires_real_provider:
        try:
            await _openrouter_startup_smoke(settings, profile, api_key=openrouter_api_key)
        except Exception as exc:
            if isinstance(exc, StructuredSmokeError):
                _structured_alias_unready[exc.alias] = "MODEL_ALIAS_UNREADY"
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
    dynamic_checks = [
        ReadinessCheck(component=f"structured.{alias}", status="failed", reason=reason)
        for alias, reason in sorted(_structured_alias_unready.items())
    ]
    checks = [*_readiness_state.checks, *dynamic_checks]
    return {
        "status": "degraded" if checks else _readiness_state.status,
        "checks": [
            {
                "component": check.component,
                "status": check.status,
                "reason": check.reason,
            }
            for check in checks
        ],
    }


async def _active_control_plane_readiness(settings: Settings) -> bool | None:
    """Return DB readiness when an active revision exists, or None for legacy YAML mode."""
    try:
        async with asyncio.timeout(1.0):
            async with connect(settings) as conn:
                revision = await active_revision(conn)
    except Exception:  # noqa: BLE001 - startup retains legacy behavior if DB is unavailable.
        return None
    if revision is None:
        return None
    report = revision.get("validation_report") or {}
    return revision.get("status") == "active" and report.get("status") == "passed"


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    settings = get_settings()
    registry = get_model_registry(settings)
    data = [
        {
            "id": alias,
            "provider": model.provider,
            "operation": model.operation,
            "dimensions": model.dimensions,
            "context_window_tokens": model.context_window_tokens,
            "tokenizer": model.tokenizer,
            "healthy": _alias_available(model),
        }
        for alias, model in sorted(registry.models.items())
    ]
    try:
        async with connect(settings) as conn:
            db_catalog = await list_models(conn)
        known = {str(item["id"]) for item in data}
        data.extend(
            {
                "id": str(row["alias"]),
                "provider": str(row["provider"]),
                "operation": str(row["operation"]),
                "dimensions": row.get("dimensions"),
                "context_window_tokens": row.get("context_window_tokens"),
                "tokenizer": (row.get("tokenizer_contract") or {}).get("name"),
                "healthy": bool(row.get("is_enabled", True)),
            }
            for row in db_catalog
            if str(row["alias"]) not in known
        )
    except Exception:  # noqa: BLE001 - legacy YAML catalog remains available if DB is offline.
        return {"object": "list", "data": data}
    return {
        "object": "list",
        "data": data,
    }


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(payload: dict[str, Any], request: Request) -> Any:
    correlation_id = request.headers.get("X-Request-ID", "")
    payload = await _resolve_stage_payload(payload, "chat")
    if payload.get("stream") is True:
        return await proxy_stream("chat/completions", payload, correlation_id=correlation_id)
    return await proxy("chat/completions", payload, correlation_id=correlation_id)


@app.post("/v1/embeddings")
async def create_embeddings(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    payload = await _resolve_stage_payload(payload, "embedding")
    return await proxy("embeddings", payload, correlation_id=request.headers.get("X-Request-ID", ""))


@app.post("/v1/rerank")
async def create_rerank(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    payload = await _resolve_stage_payload(payload, "rerank")
    return await proxy("rerank", payload, correlation_id=request.headers.get("X-Request-ID", ""))


@app.post("/v1/tokenize")
async def tokenize(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a safe token count using the tokenizer contract selected by the alias."""
    settings = get_settings()
    payload = await _resolve_stage_payload(payload, "chat")
    alias_name = str(payload.get("model") or "")
    alias = get_model_registry(settings).require(alias_name, "chat")
    value = payload.get("text")
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=422, detail="tokenize text is required")
    if len(value) > 1_000_000:
        raise HTTPException(status_code=413, detail="tokenize text is too large")
    return {
        "object": "tokenization",
        "model": alias_name,
        "tokenizer": alias.tokenizer or "gateway_char_estimate_v1",
        "input_tokens": max(1, len(value) // 4),
    }


async def _resolve_stage_payload(payload: dict[str, Any], operation: str) -> dict[str, Any]:
    """Resolve a stage against one immutable database revision.

    Alias requests remain supported for CLI compatibility.  A stage request is
    never silently replaced by a mock or by the current draft.
    """
    stage_key = payload.get("stage")
    if not stage_key:
        return payload
    revision_id = payload.get("config_revision_id")
    if not revision_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_CONFIG_REVISION_REQUIRED",
                "message": "config_revision_id is required for stage calls",
            },
        )
    settings = get_settings()
    async with connect(settings) as conn:
        revision = await get_revision(conn, str(revision_id))
        if revision is None or revision.get("status") not in {"active", "archived"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MODEL_CONFIG_REVISION_UNAVAILABLE",
                    "message": "requested model configuration revision is not active or archived",
                },
            )
        if payload.get("config_hash") and str(payload["config_hash"]) != str(revision["config_hash"]):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MODEL_CONFIG_HASH_MISMATCH",
                    "message": "requested model configuration hash does not match the revision",
                },
            )
        snapshot = revision.get("resolved_snapshot") or {}
        binding = (snapshot.get("stages") or {}).get(str(stage_key))
        if not isinstance(binding, dict):
            raise HTTPException(
                status_code=503,
                detail={"code": "MODEL_STAGE_UNASSIGNED", "message": "requested stage has no model assignment"},
            )
        alias_name = str(binding.get("model_alias") or binding.get("alias") or "")
        model_result = await conn.execute(
            text(
                "SELECT m.*, c.base_url AS connection_base_url, c.endpoint_paths, c.safe_headers, "
                "c.tls_verify, c.driver AS connection_driver, cr.encrypted_payload "
                "FROM model_aliases m LEFT JOIN model_provider_connections c ON c.id=m.connection_id "
                "LEFT JOIN model_connection_credentials cr ON cr.connection_id=c.id AND cr.state='active' "
                "WHERE m.alias=:alias AND m.is_enabled=true"
            ),
            {"alias": alias_name},
        )
        model = model_result.mappings().first()
        if model is None or str(model["operation"]) != operation:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MODEL_STAGE_UNAVAILABLE",
                    "message": "assigned model is unavailable for this operation",
                },
            )
        overrides = binding.get("parameter_overrides") or binding.get("parameters") or {}
        provider_parameters = dict(model.get("model_defaults") or {})
        provider_parameters.update(overrides)
        thinking_policy = binding.get("thinking_policy") or binding.get("thinking")
        from wikipediarag.model_control import (
            ModelDriver,
            ThinkingPolicy,
            compile_token_envelope,
            map_thinking_parameters,
        )

        resolved_thinking = ThinkingPolicy.from_mapping(thinking_policy)
        if thinking_policy:
            provider_driver = ModelDriver(str(model.get("connection_driver") or model.get("provider")))
            provider_parameters.update(map_thinking_parameters(provider_driver, resolved_thinking))
        requested_limit = payload.get("max_tokens", payload.get("max_output_tokens"))
        configured_limit = model.get("max_output_tokens")
        if "max_output_tokens" in provider_parameters and "max_tokens" not in provider_parameters:
            provider_parameters["max_tokens"] = provider_parameters.pop("max_output_tokens")
        if configured_limit is not None:
            provider_parameters["max_tokens"] = (
                min(int(requested_limit), int(configured_limit))
                if requested_limit is not None
                else int(configured_limit)
            )
        if model.get("context_window_tokens") and provider_parameters.get("max_tokens"):
            input_tokens = sum(
                max(1, len(str(message.get("content") or "")) // 4)
                for message in payload.get("messages") or []
                if isinstance(message, dict)
            )
            try:
                compile_token_envelope(
                    max_input=input_tokens,
                    context_window=int(model["context_window_tokens"]),
                    final_output_reserve=int(provider_parameters["max_tokens"]),
                    thinking=resolved_thinking,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": str(exc), "message": "stage token envelope exceeds the model context window"},
                ) from exc
        result = {**payload, "model": alias_name, "_db_model_contract": dict(model)}
        result.pop("stage", None)
        result.pop("config_revision_id", None)
        result.pop("config_hash", None)
        result.update(provider_parameters)
        if model.get("connection_base_url"):
            result["_gateway_base_url"] = str(model["connection_base_url"])
            result["_gateway_paths"] = model.get("endpoint_paths") or {}
            headers = dict(model.get("safe_headers") or {})
            encrypted_payload = model.get("encrypted_payload")
            if encrypted_payload:
                try:
                    credential = decrypt_server_tokens(settings, json.loads(str(encrypted_payload)))
                    token = credential.get("api_key") or credential.get("token") or credential.get("access_token")
                    if token:
                        headers.setdefault("Authorization", f"Bearer {token}")
                except Exception as exc:  # noqa: BLE001 - only expose a safe stable error.
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "MODEL_CREDENTIALS_UNREADABLE", "message": "model credentials are unavailable"},
                    ) from exc
            result["_gateway_headers"] = headers
        result["_model_config_revision_id"] = str(revision["id"])
        result["_model_config_hash"] = str(revision["config_hash"])
        return result


async def proxy(path: str, payload: dict[str, Any], *, correlation_id: str = "") -> dict[str, Any]:
    settings = get_settings()
    model_alias = str(payload.get("model") or "")
    db_contract = payload.get("_db_model_contract")
    if isinstance(db_contract, dict):
        alias = ModelAlias(
            provider=str(db_contract["provider"]),
            model=str(db_contract["provider_model"]),
            operation=_operation_for_path(path),
            dimensions=db_contract.get("dimensions"),
            context_window_tokens=db_contract.get("context_window_tokens"),
            tokenizer=db_contract.get("tokenizer"),
            provider_preferences=db_contract.get("provider_preferences") or {},
        )
    else:
        alias = get_model_registry(settings).require(model_alias, _operation_for_path(path))
    provider_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
    provider_payload["model"] = alias.model
    if alias.provider_preferences and "provider" not in provider_payload:
        provider_payload["provider"] = dict(alias.provider_preferences)
    if alias.dimensions is not None and path == "embeddings":
        provider_payload.setdefault("dimensions", alias.dimensions)

    runtime_base_url = payload.get("_gateway_base_url")
    runtime_headers = payload.get("_gateway_headers")
    runtime_paths_value = payload.get("_gateway_paths")
    runtime_paths: dict[str, Any] = runtime_paths_value if isinstance(runtime_paths_value, dict) else {}
    path_key = "chat" if path == "chat/completions" else path
    provider_path = str(runtime_paths.get(path_key) or runtime_paths.get(path) or path)
    if alias.provider == "mock":
        base_url = settings.mock_provider_url.rstrip("/")
        url = f"{base_url}/v1/{provider_path}"
        headers: dict[str, str] = {}
    elif alias.provider == "openrouter":
        base_url = str(runtime_base_url or settings.openrouter_base_url).rstrip("/")
        url = f"{base_url}/{provider_path.lstrip('/')}"
        if runtime_headers:
            headers = dict(runtime_headers)
        else:
            openrouter_api_key = _openrouter_api_key_or_503(settings)
            if not openrouter_api_key:
                raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured")
            headers = {"Authorization": f"Bearer {openrouter_api_key}"}
    elif alias.provider in {"vllm", "llamacpp", "textgen_webui", "openai_compatible"}:
        if not runtime_base_url:
            raise HTTPException(
                status_code=503,
                detail={"code": "MODEL_CONNECTION_REQUIRED", "message": "model connection is not configured"},
            )
        base_url = str(runtime_base_url).rstrip("/")
        url = f"{base_url}/{provider_path.lstrip('/')}"
        headers = dict(runtime_headers or {})
    else:
        raise HTTPException(status_code=503, detail=f"unsupported model provider {alias.provider}")
    if correlation_id:
        headers["X-Request-ID"] = correlation_id

    circuit = _circuit_for_alias(model_alias, settings)
    if model_alias in _structured_alias_unready and _is_structured_request(provider_payload):
        raise _structured_http_error("MODEL_ALIAS_UNREADY", "model alias failed structured-output readiness")
    try:
        circuit.before_call()
    except DependencyCircuitOpen as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {"code": "DEPENDENCY_CIRCUIT_OPEN", "message": "model dependency temporarily unavailable"}
            },
            headers={"Retry-After": str(max(1, int(exc.retry_after_seconds)))},
        ) from exc
    try:
        async with httpx.AsyncClient(timeout=settings.model_provider_timeout_seconds) as client:
            response = await client.post(url, json=provider_payload, headers=headers)
    except httpx.TimeoutException as exc:
        circuit.record_failure()
        raise _structured_http_error(
            "DEPENDENCY_TIMEOUT", "model dependency did not respond before the deadline", status_code=504
        ) from exc
    except httpx.NetworkError as exc:
        circuit.record_failure()
        raise _structured_http_error(
            "DEPENDENCY_UNAVAILABLE", "model dependency is unavailable", status_code=502
        ) from exc
    if response.status_code >= 400:
        if response.status_code in {429, 502, 503, 504}:
            circuit.record_failure()
        raise _provider_http_error(response)
    try:
        result = dict(response.json())
    except (ValueError, TypeError) as exc:
        circuit.record_failure()
        raise _structured_http_error("MODEL_OUTPUT_INVALID", "model provider returned invalid JSON") from exc
    if _is_structured_request(provider_payload):
        try:
            schema = _structured_schema(provider_payload)
            _validate_structured_provider_response(result, schema)
        except ValueError as exc:
            circuit.record_failure()
            _structured_schema_invalid_streak[model_alias] = _structured_schema_invalid_streak.get(model_alias, 0) + 1
            if _structured_schema_invalid_streak[model_alias] >= 3:
                _structured_alias_unready[model_alias] = "MODEL_ALIAS_UNREADY"
            code = "MODEL_OUTPUT_TRUNCATED" if _provider_output_truncated(result) else "MODEL_OUTPUT_INVALID"
            raise _structured_http_error(code, "model provider violated structured-output contract") from exc
        if _provider_exceeded_output_limit(provider_payload, result):
            circuit.record_failure()
            _structured_schema_invalid_streak[model_alias] = _structured_schema_invalid_streak.get(model_alias, 0) + 1
            if _structured_schema_invalid_streak[model_alias] >= 3:
                _structured_alias_unready[model_alias] = "MODEL_ALIAS_UNREADY"
            raise _structured_http_error("MODEL_OUTPUT_TRUNCATED", "model provider exceeded output token limit")
        _structured_schema_invalid_streak.pop(model_alias, None)
    circuit.record_success()
    result.setdefault("model_alias", model_alias)
    result.setdefault("provider", alias.provider)
    return result


def _is_structured_request(payload: dict[str, Any]) -> bool:
    response_format = payload.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") == "json_schema"


def _structured_schema(payload: dict[str, Any]) -> dict[str, Any]:
    response_format = payload.get("response_format")
    if not isinstance(response_format, dict):
        raise ValueError("structured response format is missing")
    definition = response_format.get("json_schema")
    if not isinstance(definition, dict) or not isinstance(definition.get("schema"), dict):
        raise ValueError("structured response schema is missing")
    return dict(definition["schema"])


def _validate_structured_provider_response(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("provider response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ValueError("provider response has no message content")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("provider structured content is not JSON") from exc
    _validate_json_schema_value(value, schema, path="$", root=schema)


def _validate_json_schema_value(value: Any, schema: dict[str, Any], *, path: str, root: dict[str, Any]) -> None:
    del root
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    if "null" in types and value is None:
        return
    if "object" in types:
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        required = schema.get("required", [])
        if isinstance(required, list) and any(key not in value for key in required):
            raise ValueError(f"{path} is missing required fields")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child_value in value.items():
                if key not in properties and schema.get("additionalProperties") is False:
                    raise ValueError(f"{path}.{key} is not allowed")
                if key in properties and isinstance(properties[key], dict):
                    _validate_json_schema_value(child_value, dict(properties[key]), path=f"{path}.{key}", root=schema)
        return
    if "array" in types:
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema_value(item, dict(item_schema), path=f"{path}[{index}]", root=schema)
        return
    if "string" in types and not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    if "boolean" in types and not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    if "number" in types and (not isinstance(value, int | float) or isinstance(value, bool)):
        raise ValueError(f"{path} must be a number")
    if "integer" in types and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{path} must be an integer")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ValueError(f"{path} is outside enum")


def _provider_output_truncated(payload: dict[str, Any]) -> bool:
    choices = payload.get("choices")
    return bool(
        isinstance(choices, list)
        and choices
        and isinstance(choices[0], dict)
        and choices[0].get("finish_reason") == "length"
    )


def _provider_exceeded_output_limit(request: dict[str, Any], payload: dict[str, Any]) -> bool:
    limit = request.get("max_tokens")
    usage = payload.get("usage")
    completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
    return isinstance(limit, int) and isinstance(completion, int) and completion > limit


def _structured_http_error(code: str, message: str, *, status_code: int = 502) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": {"code": code, "message": message}})


async def proxy_stream(path: str, payload: dict[str, Any], *, correlation_id: str = "") -> StreamingResponse:
    settings = get_settings()
    model_alias = str(payload.get("model") or "")
    db_contract = payload.get("_db_model_contract")
    if isinstance(db_contract, dict):
        alias = ModelAlias(
            provider=str(db_contract["provider"]),
            model=str(db_contract["provider_model"]),
            operation=_operation_for_path(path),
            dimensions=db_contract.get("dimensions"),
            context_window_tokens=db_contract.get("context_window_tokens"),
            tokenizer=db_contract.get("tokenizer"),
            provider_preferences=db_contract.get("provider_preferences") or {},
        )
    else:
        alias = get_model_registry(settings).require(model_alias, _operation_for_path(path))
    provider_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
    provider_payload["model"] = alias.model
    if alias.provider_preferences and "provider" not in provider_payload:
        provider_payload["provider"] = dict(alias.provider_preferences)
    runtime_base_url = payload.get("_gateway_base_url")
    runtime_headers = payload.get("_gateway_headers")
    runtime_paths_value = payload.get("_gateway_paths")
    runtime_paths: dict[str, Any] = runtime_paths_value if isinstance(runtime_paths_value, dict) else {}
    path_key = "chat" if path == "chat/completions" else path
    provider_path = str(runtime_paths.get(path_key) or runtime_paths.get(path) or path)
    if alias.provider == "mock":
        base_url = settings.mock_provider_url.rstrip("/")
        url = f"{base_url}/v1/{provider_path}"
        headers: dict[str, str] = {}
    elif alias.provider == "openrouter":
        base_url = str(runtime_base_url or settings.openrouter_base_url).rstrip("/")
        url = f"{base_url}/{provider_path.lstrip('/')}"
        if runtime_headers:
            headers = dict(runtime_headers)
        else:
            openrouter_api_key = _openrouter_api_key_or_503(settings)
            if not openrouter_api_key:
                raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured")
            headers = {"Authorization": f"Bearer {openrouter_api_key}"}
    elif alias.provider in {"vllm", "llamacpp", "textgen_webui", "openai_compatible"}:
        if not runtime_base_url:
            raise HTTPException(
                status_code=503,
                detail={"code": "MODEL_CONNECTION_REQUIRED", "message": "model connection is not configured"},
            )
        base_url = str(runtime_base_url).rstrip("/")
        url = f"{base_url}/{provider_path.lstrip('/')}"
        headers = dict(runtime_headers or {})
    else:
        raise HTTPException(status_code=503, detail=f"unsupported model provider {alias.provider}")
    if correlation_id:
        headers["X-Request-ID"] = correlation_id

    circuit = _circuit_for_alias(model_alias, settings)
    try:
        circuit.before_call()
    except DependencyCircuitOpen as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {"code": "DEPENDENCY_CIRCUIT_OPEN", "message": "model dependency temporarily unavailable"}
            },
            headers={"Retry-After": str(max(1, int(exc.retry_after_seconds)))},
        ) from exc

    async def stream() -> AsyncIterator[bytes]:
        started_stream = False
        try:
            async with httpx.AsyncClient(timeout=settings.model_provider_timeout_seconds) as client:
                async with client.stream("POST", url, json=provider_payload, headers=headers) as response:
                    if response.status_code >= 400:
                        if response.status_code in {429, 502, 503, 504}:
                            circuit.record_failure()
                        raise _provider_http_error(response, stream=True)
                    async for chunk in response.aiter_bytes():
                        started_stream = True
                        circuit.record_success()
                        yield chunk
        except httpx.TimeoutException as exc:
            circuit.record_failure()
            raise _structured_http_error(
                "DEPENDENCY_TIMEOUT", "model dependency stream timed out", status_code=504
            ) from exc
        except httpx.NetworkError as exc:
            circuit.record_failure()
            raise _structured_http_error(
                "DEPENDENCY_UNAVAILABLE", "model dependency stream is unavailable", status_code=502
            ) from exc
        except Exception:
            if not started_stream:
                circuit.record_failure()
            raise

    return StreamingResponse(stream(), media_type="text/event-stream")


def _operation_for_path(path: str) -> ModelOperation:
    if path == "chat/completions":
        return "chat"
    if path == "embeddings":
        return "embedding"
    if path == "rerank":
        return "rerank"
    raise HTTPException(status_code=404, detail="unknown model gateway path")


def _circuit_for_alias(alias: str, settings: Settings) -> DependencyCircuit:
    circuit = _dependency_circuits.get(alias)
    if circuit is None:
        circuit = DependencyCircuit(
            alias,
            failure_threshold=settings.dependency_circuit_failure_threshold,
            cooldown_seconds=settings.dependency_circuit_cooldown_seconds,
        )
        _dependency_circuits[alias] = circuit
    return circuit


def _alias_available(alias: ModelAlias) -> bool:
    settings = get_settings()
    if alias.provider == "mock":
        return True
    if alias.provider == "openrouter":
        try:
            return bool(resolve_openrouter_api_key(settings)) and not _provider_degraded("openrouter")
        except RuntimeError:
            return False
    return False


def _openrouter_api_key_or_503(settings: Settings) -> str:
    try:
        return resolve_openrouter_api_key(settings)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY_FILE is not readable") from exc


def _provider_degraded(provider: str) -> bool:
    prefix = f"{provider}."
    return any(check.component.startswith(prefix) and check.status != "ok" for check in _readiness_state.checks)


def _safe_smoke_failure_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return "DEPENDENCY_UNAVAILABLE" if exc.response.status_code in {429, 502, 503, 504} else "INTERNAL_ERROR"
    if isinstance(exc, httpx.TimeoutException):
        return "DEPENDENCY_TIMEOUT"
    if isinstance(exc, httpx.NetworkError):
        return "DEPENDENCY_UNAVAILABLE"
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


def _provider_http_error(response: httpx.Response, *, stream: bool = False) -> HTTPException:
    headers: dict[str, str] | None = None
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        headers = {"Retry-After": retry_after}
    if response.status_code == 429:
        status_code = 429
    elif response.status_code in {503, 529}:
        status_code = 503
    elif response.status_code == 524:
        status_code = 504
    else:
        status_code = 502
    detail = "model dependency stream failed" if stream else "model dependency request failed"
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": "DEPENDENCY_UNAVAILABLE", "message": detail}},
        headers=headers,
    )


async def _openrouter_startup_smoke(settings: Settings, profile: RetrievalProfile, *, api_key: str) -> None:
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
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=settings.model_provider_timeout_seconds) as client:
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
        main_alias = aliases[profile.model_aliases.generator_main]
        typed = await client.post(
            f"{base_url}/chat/completions",
            json=_structured_smoke_request(main_alias.model),
            headers=headers,
        )
        typed.raise_for_status()
        typed_payload = typed.json()
        try:
            _validate_structured_provider_response(typed_payload, _STRUCTURED_SMOKE_SCHEMA)
        except ValueError as exc:
            raise StructuredSmokeError(
                profile.model_aliases.generator_main, "structured canary violated schema"
            ) from exc
        if _provider_exceeded_output_limit({"max_tokens": _STRUCTURED_SMOKE_MAX_TOKENS}, typed_payload):
            raise StructuredSmokeError(
                profile.model_aliases.generator_main, "structured generator ignored max token cap"
            )

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
