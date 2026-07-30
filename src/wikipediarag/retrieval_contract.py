from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.answerability import GATE_VERSION as ANSWERABILITY_GATE_VERSION
from wikipediarag.config import Settings, get_settings
from wikipediarag.model_registry import ModelOperation, get_model_registry
from wikipediarag.repository import get_knowledge_base, load_index_version_by_read_alias
from wikipediarag.retrieval_profile import RetrievalProfile

INDEX_CONTRACT_SCHEMA_VERSION = 1
RUN_CONTRACT_SCHEMA_VERSION = 1
VECTOR_FIELD = "embedding"
CLAIM_VERIFIER_VERSION = "claim_verifier_v1"
NEGATIVE_EVIDENCE_POLICY_VERSION = "explicit_negative_title_v1"


class KnowledgeBaseNotReady(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = "KB_NOT_READY"
        self.details = details or {}


class ModelRef(BaseModel):
    alias: str
    provider: str
    model: str
    operation: str
    dimensions: int | None = None


class IndexContract(BaseModel):
    schema_version: int = INDEX_CONTRACT_SCHEMA_VERSION
    source_type: str
    snapshot_id: str
    index_version: str
    physical_index: str
    read_alias: str
    vector_field: str = VECTOR_FIELD
    embedding: ModelRef
    embedding_dimensions: int = Field(ge=1)
    retrieval_profile: str
    retrieval_profile_source: str
    retrieval_profile_version: int
    chunking: dict[str, Any]

    @property
    def contract_id(self) -> str:
        return contract_id(self.model_dump(mode="json"))


class RunContract(BaseModel):
    schema_version: int = RUN_CONTRACT_SCHEMA_VERSION
    index_contract_id: str
    retrieval_profile: str
    retrieval_profile_hash: str
    retrieval_overrides_hash: str
    model_aliases: dict[str, ModelRef]
    answerability_gate_version: str = ANSWERABILITY_GATE_VERSION
    negative_evidence_policy_version: str = NEGATIVE_EVIDENCE_POLICY_VERSION
    verification_policy: dict[str, Any]
    claim_verifier_version: str = CLAIM_VERIFIER_VERSION

    @property
    def contract_id(self) -> str:
        return contract_id(self.model_dump(mode="json"))


class ActiveRetrievalContract(BaseModel):
    read_alias: str
    index_version: str
    index_contract: IndexContract
    index_contract_id: str
    run_contract: RunContract
    run_contract_id: str

    def event_payload(self) -> dict[str, str]:
        return {
            "read_alias": self.read_alias,
            "index_version": self.index_version,
            "index_contract_id": self.index_contract_id,
            "run_contract_id": self.run_contract_id,
        }


def contract_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def profile_hash(profile: RetrievalProfile) -> str:
    return contract_id(profile.model_dump(mode="json"))


def overrides_hash(overrides: dict[str, Any] | None) -> str:
    return contract_id(overrides or {})


def model_ref(alias: str, operation: ModelOperation, settings: Settings | None = None) -> ModelRef:
    resolved = settings or get_settings()
    model = get_model_registry(resolved).require(alias, operation)
    return ModelRef(
        alias=alias,
        provider=model.provider,
        model=model.model,
        operation=model.operation,
        dimensions=model.dimensions,
    )


def build_index_contract(
    *,
    index_version: str,
    source_type: str,
    snapshot_id: str,
    physical_index: str,
    read_alias: str,
    embedding_alias: str,
    embedding_dimensions: int,
    profile: RetrievalProfile,
    settings: Settings | None = None,
) -> IndexContract:
    return IndexContract(
        source_type=source_type,
        snapshot_id=snapshot_id,
        index_version=index_version,
        physical_index=physical_index,
        read_alias=read_alias,
        embedding=model_ref(embedding_alias, "embedding", settings),
        embedding_dimensions=embedding_dimensions,
        retrieval_profile=profile.name,
        retrieval_profile_source=profile.source,
        retrieval_profile_version=profile.version,
        chunking=profile.chunking.model_dump(mode="json"),
    )


def build_run_contract(
    *,
    index_contract_id: str,
    profile: RetrievalProfile,
    retrieval_overrides: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> RunContract:
    aliases = profile.model_aliases
    return RunContract(
        index_contract_id=index_contract_id,
        retrieval_profile=profile.name,
        retrieval_profile_hash=profile_hash(profile),
        retrieval_overrides_hash=overrides_hash(retrieval_overrides),
        model_aliases={
            "embed": model_ref(aliases.embed, "embedding", settings),
            "generator_fast": model_ref(aliases.generator_fast, "chat", settings),
            "generator_main": model_ref(aliases.generator_main, "chat", settings),
            "verifier": model_ref(aliases.verifier, "chat", settings),
            "rerank": model_ref(aliases.rerank, "rerank", settings),
        },
        verification_policy=profile.answer.verification.model_dump(mode="json"),
    )


async def validate_active_retrieval_contract(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    profile: RetrievalProfile,
    retrieval_overrides: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> ActiveRetrievalContract:
    resolved = settings or get_settings()
    kb = await get_knowledge_base(conn, tenant_id, knowledge_base_id)
    if kb is None:
        raise KnowledgeBaseNotReady(
            "knowledge base is not available",
            details={"knowledge_base_id": knowledge_base_id},
        )
    read_alias = str(kb.get("active_index") or "")
    if not read_alias:
        raise KnowledgeBaseNotReady(
            "knowledge base has no active index",
            details={"knowledge_base_id": knowledge_base_id},
        )
    row = await load_index_version_by_read_alias(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        read_alias=read_alias,
    )
    if row is None:
        raise KnowledgeBaseNotReady(
            "active index has no registered index version",
            details={"knowledge_base_id": knowledge_base_id, "read_alias": read_alias},
        )
    return validate_index_version_contract(
        row,
        profile=profile,
        retrieval_overrides=retrieval_overrides,
        settings=resolved,
    )


def validate_index_version_contract(
    row: dict[str, Any],
    *,
    profile: RetrievalProfile,
    retrieval_overrides: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> ActiveRetrievalContract:
    resolved = settings or get_settings()
    read_alias = str(row["read_alias"])
    expected_source = _source_type_for_profile(profile)
    actual_source = str(row["source_type"])
    if actual_source != expected_source:
        raise KnowledgeBaseNotReady(
            "active index source is incompatible with retrieval profile",
            details={
                "expected_source_type": expected_source,
                "actual_source_type": actual_source,
                "read_alias": read_alias,
            },
        )
    embedding_alias = str(row["embedding_alias"])
    embedding = model_ref(embedding_alias, "embedding", resolved)
    dimensions = int(row["embedding_dimensions"])
    if embedding.dimensions is not None and embedding.dimensions != dimensions:
        raise KnowledgeBaseNotReady(
            "active index embedding dimensions do not match the configured model alias",
            details={
                "embedding_alias": embedding_alias,
                "index_dimensions": dimensions,
                "model_dimensions": embedding.dimensions,
                "read_alias": read_alias,
            },
        )
    if profile.model_aliases.embed != embedding_alias:
        raise KnowledgeBaseNotReady(
            "active index embedding alias is incompatible with retrieval profile",
            details={
                "profile_embedding_alias": profile.model_aliases.embed,
                "index_embedding_alias": embedding_alias,
                "read_alias": read_alias,
            },
        )
    index_contract = build_index_contract(
        index_version=str(row["id"]),
        source_type=actual_source,
        snapshot_id=str(row["snapshot_id"]),
        physical_index=str(row["physical_index"]),
        read_alias=read_alias,
        embedding_alias=embedding_alias,
        embedding_dimensions=dimensions,
        profile=profile,
        settings=resolved,
    )
    metadata = dict(row.get("metadata") or {})
    stored_id = metadata.get("index_contract_id")
    stored_contract = metadata.get("index_contract")
    if stored_id is not None and stored_id != index_contract.contract_id:
        raise KnowledgeBaseNotReady(
            "active index contract id does not match current configuration",
            details={"stored_index_contract_id": stored_id, "expected_index_contract_id": index_contract.contract_id},
        )
    if isinstance(stored_contract, dict):
        stored_payload = dict(stored_contract)
        stored_payload.pop("contract_id", None)
        if contract_id(stored_payload) != index_contract.contract_id:
            raise KnowledgeBaseNotReady(
                "active index contract payload does not match current configuration",
                details={"read_alias": read_alias, "expected_index_contract_id": index_contract.contract_id},
            )
    run_contract = build_run_contract(
        index_contract_id=index_contract.contract_id,
        profile=profile,
        retrieval_overrides=retrieval_overrides,
        settings=resolved,
    )
    return ActiveRetrievalContract(
        read_alias=read_alias,
        index_version=str(row["id"]),
        index_contract=index_contract,
        index_contract_id=index_contract.contract_id,
        run_contract=run_contract,
        run_contract_id=run_contract.contract_id,
    )


def index_contract_metadata(index_contract: IndexContract) -> dict[str, Any]:
    return {
        "index_contract_id": index_contract.contract_id,
        "index_contract": index_contract.model_dump(mode="json"),
    }


def _source_type_for_profile(profile: RetrievalProfile) -> str:
    if profile.source == "zim":
        return "zim"
    if profile.source == "xml":
        return "wikipedia_xml"
    return profile.source
