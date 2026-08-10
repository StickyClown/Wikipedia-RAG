"""Provider-neutral model control-plane contracts.

This module deliberately contains no network or process-management code.  It is
the safe, deterministic part of the control plane used by the admin API and by
the gateway when resolving an immutable configuration revision.
"""

from __future__ import annotations

import hashlib
import json
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
    StageSpec("chat.answer", ModelOperation.chat, frozenset({"chat"})),
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


class ModelControlError(ValueError):
    """Safe validation error suitable for exposing as a stable error code."""


def config_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def merge_parameters(
    driver_defaults: Mapping[str, Any] | None,
    model_defaults: Mapping[str, Any] | None,
    stage_overrides: Mapping[str, Any] | None,
    *,
    capabilities: Mapping[str, Any] | None = None,
    workload_max_tokens: int | None = None,
) -> dict[str, Any]:
    """Resolve defaults in the documented order and reject unknown fields."""
    result: dict[str, Any] = {}
    for source in (driver_defaults or {}, model_defaults or {}, stage_overrides or {}):
        unknown = set(source) - SUPPORTED_PARAMETERS
        if unknown:
            raise ModelControlError(f"unknown model parameter(s): {', '.join(sorted(unknown))}")
        result.update(source)
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
    if not stage.required_capabilities.issubset(model.capabilities):
        raise ModelControlError("MODEL_CAPABILITY_UNSUPPORTED")
    if stage.required_modalities - set(model.input_modalities):
        raise ModelControlError("MODEL_MODALITY_UNSUPPORTED")
    if model.vision_input and stage.operation is ModelOperation.chat and "image" not in stage.required_modalities:
        # Vision is catalog-only until a dedicated stage is added to the registry.
        raise ModelControlError("MODEL_VISION_CATALOG_ONLY")
    if thinking.mode is ThinkingMode.on and not bool(model.thinking_capabilities.get("reasoning_control", False)):
        raise ModelControlError("MODEL_THINKING_UNSUPPORTED")
    if thinking.mode is ThinkingMode.auto and not bool(model.thinking_capabilities.get("canary_auto", False)):
        raise ModelControlError("MODEL_THINKING_AUTO_UNCONFIRMED")
    return stage


def map_thinking_parameters(driver: ModelDriver, policy: ThinkingPolicy) -> dict[str, Any]:
    """Map the stable contract to documented endpoint parameters only."""
    effort: str | None
    if policy.mode is ThinkingMode.off:
        effort = "none"
    else:
        effort = policy.effort
    if driver is ModelDriver.openrouter:
        return {"reasoning": {"effort": effort, "exclude": not policy.return_reasoning}}
    if driver is ModelDriver.vllm:
        return {
            "reasoning_effort": effort,
            "chat_template_kwargs": {"enable_thinking": policy.mode is not ThinkingMode.off},
        }
    if driver is ModelDriver.llamacpp:
        return {
            "reasoning_effort": effort,
            "chat_template_kwargs": {"enable_thinking": policy.mode is not ThinkingMode.off},
        }
    if driver is ModelDriver.textgen_webui:
        return {
            "reasoning_effort": effort,
            "enable_thinking": policy.mode is not ThinkingMode.off,
        }
    if driver is ModelDriver.mock:
        return {"thinking": policy.as_dict()}
    # OpenAI-compatible endpoints have no safe universal translation.
    if policy.mode is not ThinkingMode.off:
        raise ModelControlError("MODEL_THINKING_MAPPING_UNCONFIRMED")
    return {}


def redact_connection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return an API-safe connection representation; never echo credentials."""
    allowed = {
        "id",
        "name",
        "driver",
        "base_url",
        "endpoint_paths",
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
