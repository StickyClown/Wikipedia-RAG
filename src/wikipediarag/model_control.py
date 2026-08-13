"""Provider-neutral model control-plane contracts.

This module deliberately contains no network or process-management code.  It is
the safe, deterministic part of the control plane used by the admin API and by
the gateway when resolving an immutable configuration revision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ModelDriver(StrEnum):
    openrouter = "openrouter"
    vllm = "vllm"
    llamacpp = "llamacpp"
    textgen_webui = "textgen_webui"
    openai_compatible = "openai_compatible"
    mock = "mock"


class ModelOperation(StrEnum):
    chat = "chat"
    embedding = "embedding"
    rerank = "rerank"


class ThinkingMode(StrEnum):
    off = "off"
    on = "on"
    auto = "auto"


# Kept as documentation for callers that want to expose common controls.  The
# gateway intentionally does not reject additional JSON parameters: vendor
# extensions are configured declaratively by the connection adapter.
SUPPORTED_PARAMETERS: frozenset[str] = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "seed",
        "stop",
        "max_output_tokens",
        "embedding_batch_size",
        "dimensions",
        "timeout",
        "thinking",
    }
)


@dataclass(frozen=True, slots=True)
class ThinkingPolicy:
    mode: ThinkingMode = ThinkingMode.off
    effort: str | None = "none"
    budget_tokens: int | None = None
    return_reasoning: bool = False

    def __post_init__(self) -> None:
        if self.effort is not None and self.budget_tokens is not None:
            raise ValueError("thinking effort and budget_tokens are mutually exclusive")
        if self.budget_tokens is not None and self.budget_tokens < 0:
            raise ValueError("thinking budget_tokens must be non-negative")
        if self.effort is not None and self.effort not in {"none", "minimal", "low", "medium", "high", "max"}:
            raise ValueError("unsupported thinking effort")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ThinkingPolicy:
        data = dict(value or {})
        return cls(
            mode=ThinkingMode(str(data.get("mode", "off"))),
            effort=data.get("effort", "none"),
            budget_tokens=data.get("budget_tokens"),
            return_reasoning=bool(data.get("return_reasoning", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "effort": self.effort,
            "budget_tokens": self.budget_tokens,
            "return_reasoning": self.return_reasoning,
        }


@dataclass(frozen=True, slots=True)
class StageSpec:
    key: str
    operation: ModelOperation
    required_capabilities: frozenset[str] = frozenset()
    required_modalities: frozenset[str] = frozenset({"text"})
    embedding_fingerprint_required: bool = False


STAGE_CATALOG: tuple[StageSpec, ...] = (
    StageSpec(
        "ingestion.embedding", ModelOperation.embedding, frozenset({"embedding"}), embedding_fingerprint_required=True
    ),
    StageSpec(
        "retrieval.query_embedding",
        ModelOperation.embedding,
        frozenset({"embedding"}),
        embedding_fingerprint_required=True,
    ),
    StageSpec("retrieval.rerank", ModelOperation.rerank, frozenset({"rerank"})),
    StageSpec("chat.answer", ModelOperation.chat, frozenset({"chat", "structured_output"})),
    StageSpec("chat.claim_verification", ModelOperation.chat, frozenset({"chat", "structured_output"})),
    StageSpec("deep_research.planner", ModelOperation.chat, frozenset({"chat", "structured_output"})),
    StageSpec("deep_research.verifier", ModelOperation.chat, frozenset({"chat", "structured_output"})),
    StageSpec("deep_research.synthesis", ModelOperation.chat, frozenset({"chat", "structured_output"})),
)
STAGE_BY_KEY: dict[str, StageSpec] = {stage.key: stage for stage in STAGE_CATALOG}


@dataclass(frozen=True, slots=True)
class ModelContract:
    alias: str
    provider_model: str
    operation: ModelOperation
    capabilities: frozenset[str] = frozenset()
    input_modalities: tuple[str, ...] = ("text",)
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    dimensions: int | None = None
    tokenizer_contract: Mapping[str, Any] = field(default_factory=dict)
    model_defaults: Mapping[str, Any] = field(default_factory=dict)
    thinking_capabilities: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @property
    def vision_input(self) -> bool:
        return "image" in self.input_modalities or bool(self.thinking_capabilities.get("vision_input", False))

    @property
    def embedding_fingerprint(self) -> str | None:
        if self.operation is not ModelOperation.embedding:
            return None
        payload = {
            "model": self.provider_model,
            "dimensions": self.dimensions,
            "normalization": self.tokenizer_contract.get("normalization"),
            "query_instruction": self.tokenizer_contract.get("query_instruction"),
            "document_instruction": self.tokenizer_contract.get("document_instruction"),
        }
        return config_hash(payload)


@dataclass(frozen=True, slots=True)
class TokenEnvelope:
    max_input: int
    reasoning_reserve: int
    final_output_reserve: int
    safety_reserve: int
    context_window: int

    @property
    def total(self) -> int:
        return self.max_input + self.reasoning_reserve + self.final_output_reserve + self.safety_reserve

    @property
    def fits(self) -> bool:
        return self.total <= self.context_window


@dataclass(frozen=True, slots=True)
class OutputBudget:
    requested_tokens: int
    effective_tokens: int
    stage_cap: int
    input_tokens: int
    reasoning_reserve: int
    safety_reserve: int
    context_window: int

    def as_metadata(self) -> dict[str, int]:
        return {
            "requested_output_tokens": self.requested_tokens,
            "effective_output_tokens": self.effective_tokens,
            "stage_output_cap": self.stage_cap,
            "input_tokens_estimate": self.input_tokens,
            "reasoning_reserve_tokens": self.reasoning_reserve,
            "safety_reserve_tokens": self.safety_reserve,
            "context_window_tokens": self.context_window,
        }


class ModelControlError(ValueError):
    """Safe validation error suitable for exposing as a stable error code."""


def resolve_output_budget(
    requested_tokens: int,
    *,
    input_tokens: int,
    context_window: int,
    stage_cap: int | None = None,
    reasoning_reserve: int = 0,
    safety_reserve: int = 32,
    minimum_tokens: int = 64,
) -> OutputBudget:
    """Resolve a bounded completion budget without exceeding the model envelope."""

    values = (requested_tokens, input_tokens, context_window, reasoning_reserve, safety_reserve, minimum_tokens)
    if min(values) < 0 or (stage_cap is not None and stage_cap < 0):
        raise ModelControlError("token envelope values must be non-negative")
    cap = int(stage_cap if stage_cap is not None else requested_tokens)
    available = int(context_window) - int(input_tokens) - int(reasoning_reserve) - int(safety_reserve)
    effective = min(int(requested_tokens), cap, available)
    if effective < int(minimum_tokens):
        raise ModelControlError("MODEL_TOKEN_BUDGET_EXCEEDED")
    return OutputBudget(
        requested_tokens=int(requested_tokens),
        effective_tokens=effective,
        stage_cap=cap,
        input_tokens=int(input_tokens),
        reasoning_reserve=int(reasoning_reserve),
        safety_reserve=int(safety_reserve),
        context_window=int(context_window),
    )


def config_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any, *, path: str = "parameter") -> Any:
    """Validate and copy a JSON-serializable configuration value."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelControlError(f"non-finite JSON value at {path}")
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, path=f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise ModelControlError(f"parameter is not JSON-serializable: {path}")


def deep_merge_json(*sources: Mapping[str, Any] | None) -> dict[str, Any]:
    """Recursively merge JSON objects; scalar and array values are replaced."""
    result: dict[str, Any] = {}
    for source in sources:
        if source is None:
            continue
        checked = _json_value(source)
        if not isinstance(checked, dict):
            raise ModelControlError("parameter source must be a JSON object")
        for key, value in checked.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = deep_merge_json(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
    return result


def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    current = payload
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise ModelControlError("request adapter path cannot be empty")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = copy.deepcopy(value)


def _pop_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        return None
    for part in parts[:-1]:
        current = current.get(part)
        if not isinstance(current, dict):
            return None
    return current.pop(parts[-1], None)


def _adapter_path(spec: Any) -> tuple[str, str | None]:
    if isinstance(spec, str):
        return spec, None
    if isinstance(spec, Mapping):
        path = spec.get("path")
        if not isinstance(path, str) or not path:
            raise ModelControlError("request adapter path must be a non-empty string")
        return path, str(spec.get("transform")) if spec.get("transform") is not None else None
    raise ModelControlError("request adapter path must be a string or object")


def compile_provider_payload(
    payload: Mapping[str, Any],
    *,
    connection_defaults: Mapping[str, Any] | None = None,
    model_defaults: Mapping[str, Any] | None = None,
    stage_overrides: Mapping[str, Any] | None = None,
    request_adapter: Mapping[str, Any] | None = None,
    thinking: Mapping[str, Any] | ThinkingPolicy | None = None,
    protected_fields: frozenset[str] = frozenset({"model", "messages", "stream", "response_format"}),
) -> dict[str, Any]:
    """Compile the stable gateway request into an OpenAI-compatible wire body.

    The adapter is data, not a provider switch.  It may map any standard field
    to a dotted vendor path and declares the paths for thinking controls.
    """
    workload = deep_merge_json(dict(payload))
    extra_body = workload.pop("extra_body", None)
    if extra_body is not None:
        if not isinstance(extra_body, Mapping):
            raise ModelControlError("extra_body must be a JSON object")
        workload = deep_merge_json(extra_body, workload)
    canonical = {key: copy.deepcopy(workload[key]) for key in protected_fields if key in workload}
    result = deep_merge_json(connection_defaults, model_defaults, stage_overrides)
    result = deep_merge_json(result, {key: value for key, value in workload.items() if key not in protected_fields})
    # Existing callers use the internal ``max_tokens`` spelling; normalize it
    # to the stable contract before the declarative adapter runs.
    if "max_tokens" in result and "max_output_tokens" not in result:
        result["max_output_tokens"] = result.pop("max_tokens")

    adapter = _json_value(request_adapter or {})
    if not isinstance(adapter, dict):
        raise ModelControlError("request_adapter must be a JSON object")
    parameter_map = adapter.get("parameter_map", {"max_output_tokens": "max_tokens"})
    if parameter_map is None:
        parameter_map = {}
    if not isinstance(parameter_map, Mapping):
        raise ModelControlError("request_adapter.parameter_map must be an object")
    for source, spec in parameter_map.items():
        if source not in result:
            continue
        path, transform = _adapter_path(spec)
        value = result.pop(source)
        if transform == "inverse":
            value = not bool(value)
        elif transform not in {None, "identity"}:
            raise ModelControlError(f"unsupported request adapter transform: {transform}")
        _set_path(result, path, value)

    if thinking is not None:
        policy = thinking.as_dict() if isinstance(thinking, ThinkingPolicy) else dict(thinking)
        mode = str(policy.get("mode", "off"))
        thinking_map = adapter.get("thinking", {})
        if thinking_map is not None and not isinstance(thinking_map, Mapping):
            raise ModelControlError("request_adapter.thinking must be an object")
        values = {
            "enabled": mode != ThinkingMode.off.value,
            "effort": "none" if mode == ThinkingMode.off.value else policy.get("effort"),
            "budget_tokens": policy.get("budget_tokens"),
            "return_reasoning": bool(policy.get("return_reasoning", False)),
        }
        for name, spec in (thinking_map or {}).items():
            if name not in values or values[name] is None:
                continue
            path, transform = _adapter_path(spec)
            value = values[name]
            if isinstance(spec, Mapping) and name == "enabled":
                value = spec.get("on" if value else "off", value)
            if transform == "inverse":
                value = not bool(value)
            elif transform not in {None, "identity"}:
                raise ModelControlError(f"unsupported request adapter transform: {transform}")
            _set_path(result, path, value)
    result.pop("thinking", None)
    for key, value in canonical.items():
        result[key] = value
    return result


def merge_parameters(
    driver_defaults: Mapping[str, Any] | None,
    model_defaults: Mapping[str, Any] | None,
    stage_overrides: Mapping[str, Any] | None,
    *,
    capabilities: Mapping[str, Any] | None = None,
    workload_max_tokens: int | None = None,
) -> dict[str, Any]:
    """Resolve defaults in order while allowing arbitrary JSON vendor fields."""
    result = deep_merge_json(driver_defaults, model_defaults, stage_overrides)
    capability_map = capabilities or {}
    for name in result:
        if capability_map and capability_map.get(name) is False:
            raise ModelControlError(f"parameter is not supported by model capability contract: {name}")
    if workload_max_tokens is not None:
        configured = result.get("max_output_tokens")
        result["max_output_tokens"] = (
            min(int(configured), workload_max_tokens) if configured is not None else workload_max_tokens
        )
    if result.get("max_output_tokens", 0) < 0:
        raise ModelControlError("max_output_tokens must be non-negative")
    return result


def compile_token_envelope(
    *,
    max_input: int,
    context_window: int,
    final_output_reserve: int,
    thinking: ThinkingPolicy,
    safety_reserve: int = 32,
) -> TokenEnvelope:
    if min(max_input, context_window, final_output_reserve, safety_reserve) < 0:
        raise ModelControlError("token envelope values must be non-negative")
    reasoning_reserve = thinking.budget_tokens or 0 if thinking.mode is not ThinkingMode.off else 0
    envelope = TokenEnvelope(max_input, reasoning_reserve, final_output_reserve, safety_reserve, context_window)
    if not envelope.fits:
        raise ModelControlError("MODEL_TOKEN_BUDGET_EXCEEDED")
    return envelope


def schema_canary_reserve(minimal_json_tokens: int) -> int:
    if minimal_json_tokens < 0:
        raise ModelControlError("minimal JSON token count must be non-negative")
    return minimal_json_tokens + max(int(minimal_json_tokens * 0.25), 32)


def validate_tokenizer_calibration(local_tokens: int, provider_tokens: int) -> None:
    if local_tokens < 0 or provider_tokens < 0:
        raise ModelControlError("token counts must be non-negative")
    deviation = abs(local_tokens - provider_tokens)
    if deviation > max(int(provider_tokens * 0.05), 32):
        raise ModelControlError("MODEL_TOKENIZER_CALIBRATION_FAILED")


def validate_stage_binding(stage_key: str, model: ModelContract, thinking: ThinkingPolicy) -> StageSpec:
    stage = STAGE_BY_KEY.get(stage_key)
    if stage is None:
        raise ModelControlError("MODEL_STAGE_UNKNOWN")
    if not model.enabled or model.operation is not stage.operation:
        raise ModelControlError("MODEL_OPERATION_UNSUPPORTED")
    if stage.required_modalities - set(model.input_modalities):
        raise ModelControlError("MODEL_MODALITY_UNSUPPORTED")
    if model.vision_input and stage.operation is ModelOperation.chat and "image" not in stage.required_modalities:
        # Vision is catalog-only until a dedicated stage is added to the registry.
        raise ModelControlError("MODEL_VISION_CATALOG_ONLY")
    if not stage.required_capabilities.issubset(model.capabilities):
        raise ModelControlError("MODEL_CAPABILITY_UNSUPPORTED")
    if thinking.mode is ThinkingMode.on and not bool(model.thinking_capabilities.get("reasoning_control", False)):
        raise ModelControlError("MODEL_THINKING_UNSUPPORTED")
    if thinking.mode is ThinkingMode.auto and not bool(model.thinking_capabilities.get("canary_auto", False)):
        raise ModelControlError("MODEL_THINKING_AUTO_UNCONFIRMED")
    return stage


def map_thinking_parameters(driver: ModelDriver, policy: ThinkingPolicy) -> dict[str, Any]:
    """Backward-compatible helper returning the stable field only.

    Provider wire paths are intentionally declared in ``request_adapter``;
    this compatibility function no longer switches on a provider enum.
    """
    del driver
    return {"thinking": policy.as_dict()}


def redact_connection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return an API-safe connection representation; never echo credentials."""
    allowed = {
        "id",
        "name",
        "driver",
        "base_url",
        "endpoint_paths",
        "request_adapter",
        "request_defaults",
        "safe_headers",
        "tls_verify",
        "enabled",
        "row_version",
        "last_status",
        "last_checked_at",
        "created_at",
        "updated_at",
    }
    result = {key: value[key] for key in allowed if key in value}
    result["has_credentials"] = bool(value.get("has_credentials", False))
    result["credential_source"] = value.get("credential_source", "database")
    return result


def resolved_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return config_hash(snapshot)
