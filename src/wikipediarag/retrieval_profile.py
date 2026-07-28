from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, Field, model_validator

from wikipediarag.config import Settings, get_settings

FusionMode = Literal["rrf", "none"]
TransformMode = Literal["off", "conditional", "always"]
ParentExpansionMode = Literal["off", "selective", "always"]
ContextPackingMode = Literal["token_budget", "top_k"]
ExtendedMode = Literal["off", "conditional", "always"]


class ModelAliases(BaseModel):
    embed: str
    generator_fast: str
    generator_main: str
    verifier: str
    rerank: str


class ChunkingConfig(BaseModel):
    parent_child: bool = True
    child_tokens_min: int = Field(default=180, ge=1)
    child_tokens_max: int = Field(default=220, ge=1)
    parent_tokens_min: int = Field(default=700, ge=1)
    parent_tokens_max: int = Field(default=900, ge=1)
    hard_overlap: int = Field(default=0, ge=0)
    preserve_sections: bool = True
    preserve_paragraphs: bool = True

    @model_validator(mode="after")
    def validate_ranges(self) -> ChunkingConfig:
        if self.child_tokens_min > self.child_tokens_max:
            raise ValueError("child_tokens_min must be <= child_tokens_max")
        if self.parent_tokens_min > self.parent_tokens_max:
            raise ValueError("parent_tokens_min must be <= parent_tokens_max")
        return self


class RetrievalConfig(BaseModel):
    bm25: bool = True
    dense: bool = True
    fusion: FusionMode = "rrf"
    bm25_top_k: int = Field(default=100, ge=1)
    dense_top_k: int = Field(default=100, ge=1)
    fusion_top_k: int = Field(default=60, ge=1)
    rerank: bool = True
    rerank_top_k: int = Field(default=50, ge=1)
    query_rewrite: TransformMode = "conditional"
    query_decomposition: TransformMode = "conditional"
    top_k: int = Field(default=12, ge=1)


class PostprocessConfig(BaseModel):
    dedup: bool = True
    page_quota: int = Field(default=2, ge=1)
    parent_expansion: ParentExpansionMode = "selective"
    context_packing: ContextPackingMode = "token_budget"
    final_evidence_min: int = Field(default=6, ge=1)
    final_evidence_max: int = Field(default=12, ge=1)
    max_context_tokens: int = Field(default=30000, ge=256)
    claim_support_checker: bool = False
    extended_search: ExtendedMode = "conditional"

    @model_validator(mode="after")
    def validate_evidence_range(self) -> PostprocessConfig:
        if self.final_evidence_min > self.final_evidence_max:
            raise ValueError("final_evidence_min must be <= final_evidence_max")
        return self


class AnswerConfig(BaseModel):
    citations_required: bool = True
    deterministic_citation_validation: bool = True
    insufficient_evidence_mode: bool = True


class RetrievalProfile(BaseModel):
    name: str = ""
    source: Literal["zim", "xml", "upload"] = "zim"
    version: int = Field(default=1, ge=1)
    model_aliases: ModelAliases
    chunking: ChunkingConfig
    retrieval: RetrievalConfig
    postprocess: PostprocessConfig
    answer: AnswerConfig

    @model_validator(mode="after")
    def validate_profile(self) -> RetrievalProfile:
        if not self.retrieval.bm25 and not self.retrieval.dense:
            raise ValueError("at least one first-stage retriever must be enabled")
        if self.retrieval.fusion == "rrf" and not (self.retrieval.bm25 and self.retrieval.dense):
            raise ValueError("rrf fusion requires both bm25 and dense retrieval")
        return self

    @property
    def requires_real_provider(self) -> bool:
        return self.name == "sota_mvp"

    def embedding_dimensions(self, default: int) -> int:
        if self.name == "sota_mvp":
            return 1024
        return default


class RetrievalProfileCatalog(BaseModel):
    profiles: dict[str, RetrievalProfile]


def load_profile_catalog(path: Path) -> RetrievalProfileCatalog:
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    raw_profiles = payload.get("profiles", {})
    resolved: dict[str, dict[str, Any]] = {}
    for name in raw_profiles:
        resolved[name] = _resolve_profile(name, raw_profiles, [])
        resolved[name]["name"] = name
    return RetrievalProfileCatalog.model_validate({"profiles": resolved})


def _resolve_profile(name: str, profiles: dict[str, Any], stack: list[str]) -> dict[str, Any]:
    if name in stack:
        raise ValueError(f"cyclic retrieval profile inheritance: {' -> '.join([*stack, name])}")
    if name not in profiles:
        raise KeyError(f"unknown retrieval profile: {name}")
    current = deepcopy(profiles[name])
    parent_name = current.pop("based_on", None)
    if parent_name is None:
        return cast(dict[str, Any], current)
    parent = _resolve_profile(str(parent_name), profiles, [*stack, name])
    return _deep_merge(parent, current)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


@lru_cache
def get_profile_catalog_cached(path: str) -> RetrievalProfileCatalog:
    return load_profile_catalog(Path(path))


def get_profile_catalog(settings: Settings | None = None) -> RetrievalProfileCatalog:
    resolved = settings or get_settings()
    return get_profile_catalog_cached(str(resolved.retrieval_config_path))


def get_retrieval_profile(
    name: str | None = None,
    settings: Settings | None = None,
    overrides: dict[str, Any] | None = None,
) -> RetrievalProfile:
    resolved = settings or get_settings()
    profile_name = name or resolved.retrieval_profile
    catalog = get_profile_catalog(resolved)
    if profile_name not in catalog.profiles:
        raise KeyError(f"retrieval profile is not configured: {profile_name}")
    profile = catalog.profiles[profile_name]
    if not overrides:
        return profile
    payload = _deep_merge(profile.model_dump(), overrides)
    payload["name"] = profile_name
    return RetrievalProfile.model_validate(payload)
