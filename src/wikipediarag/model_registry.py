from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from wikipediarag.config import Settings, get_settings

ModelOperation = Literal["chat", "embedding", "rerank"]


class ModelAlias(BaseModel):
    provider: str
    model: str
    operation: ModelOperation
    dimensions: int | None = Field(default=None, ge=1)


class ModelRegistry(BaseModel):
    models: dict[str, ModelAlias]

    def require(self, alias: str, operation: ModelOperation | None = None) -> ModelAlias:
        if alias not in self.models:
            raise KeyError(f"model alias is not configured: {alias}")
        model = self.models[alias]
        if operation is not None and model.operation != operation:
            raise ValueError(f"model alias {alias} is {model.operation}, expected {operation}")
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
