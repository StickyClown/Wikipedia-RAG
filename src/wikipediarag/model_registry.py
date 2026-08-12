from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from wikipediarag.config import Settings, get_settings

ModelOperation = Literal["chat", "embedding", "rerank"]


class ModelAlias(BaseModel):
    provider: str
    model: str
    operation: ModelOperation
    dimensions: int | None = Field(default=None, ge=1)
    context_window_tokens: int | None = Field(default=None, ge=256)
    tokenizer: str | None = Field(default=None, min_length=1, max_length=120)
    provider_preferences: dict[str, Any] = Field(default_factory=dict)
    model_defaults: dict[str, Any] = Field(default_factory=dict)
    request_adapter: dict[str, Any] = Field(default_factory=dict)
    request_defaults: dict[str, Any] = Field(default_factory=dict)
    startup_canary: dict[str, Any] = Field(default_factory=dict)


class ModelRegistry(BaseModel):
    models: dict[str, ModelAlias]

    def require(self, alias: str, operation: ModelOperation | None = None) -> ModelAlias:
        if alias not in self.models:
            raise KeyError(f"model alias is not configured: {alias}")
        model = self.models[alias]
        if operation is not None and model.operation != operation:
            raise ValueError(f"model alias {alias} is {model.operation}, expected {operation}")
        return model

    def require_context_window(self, alias: str, minimum_tokens: int) -> ModelAlias:
        model = self.require(alias, "chat")
        if model.context_window_tokens is None:
            raise ValueError(f"model alias {alias} does not declare a context window")
        if model.context_window_tokens < minimum_tokens:
            raise ValueError(
                f"model alias {alias} context window {model.context_window_tokens} is below {minimum_tokens}"
            )
        return model


def load_model_registry(path: Path) -> ModelRegistry:
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    return ModelRegistry.model_validate(payload)


@lru_cache
def get_model_registry_cached(path: str) -> ModelRegistry:
    return load_model_registry(Path(path))


def get_model_registry(settings: Settings | None = None) -> ModelRegistry:
    resolved = settings or get_settings()
    return get_model_registry_cached(str(resolved.models_config_path))


def validate_deep_research_model_contract(profile: object, registry: ModelRegistry) -> None:
    """Validate server-owned Deep Research stage windows against Gateway aliases."""
    policy = getattr(profile, "deep_research", None)
    stages = getattr(policy, "stages", {})
    if not isinstance(stages, dict):
        raise ValueError("deep research profile does not expose stage configuration")
    for stage_name, stage in stages.items():
        alias = str(getattr(stage, "model_alias", ""))
        maximum = int(getattr(stage, "max_context_tokens", 0))
        try:
            registry.require_context_window(alias, maximum)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"deep research stage {stage_name} has invalid model contract") from exc
