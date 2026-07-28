from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, Field


class JobStatus(StrEnum):
    received = "received"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ChatMode(StrEnum):
    normal = "normal"
    extended = "extended"
    auto = "auto"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    conversation_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=50)
    mode: ChatMode = ChatMode.normal
    stream: bool = True
    client_request_id: str | None = Field(default=None, max_length=128)
    debug: bool = False
    retrieval_profile: str | None = Field(default=None, max_length=80)
    retrieval_overrides: dict[str, Any] = Field(default_factory=dict)


class DebugSearchRequest(BaseModel):
    message: str = Field(validation_alias=AliasChoices("message", "query"), min_length=1, max_length=32000)
    top_k: int = Field(default=10, ge=1, le=50)
    retrieval_profile: str | None = Field(default=None, max_length=80)
    retrieval_overrides: dict[str, Any] = Field(default_factory=dict)


class ImportRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1)
    xml_path: str | None = None
    index_path: str | None = None
    snapshot_id: str | None = None
    reset: bool = False


class ZimImportRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1)
    zim_path: str | None = None
    zim_filename: str | None = None
    snapshot_id: str | None = None
    reset: bool = False


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class SseEvent(BaseModel):
    event: str
    request_id: str
    query_run_id: str | None = None
    sequence: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    evidence_id: str
    chunk_id: str
    title: str
    section_path: list[str]
    content: str
    source_url: str
    scores: dict[str, float] = Field(default_factory=dict)
    ranks: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnswerabilityStatus(StrEnum):
    answerable = "ANSWERABLE"
    partial = "PARTIAL"
    unanswerable = "UNANSWERABLE"
    conflicting = "CONFLICTING"


class AnswerabilityDecision(BaseModel):
    version: str = "answerability_gate_v1"
    status: AnswerabilityStatus
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    required_parts: list[str] = Field(default_factory=list)
    covered_parts: list[str] = Field(default_factory=list)
    missing_parts: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    query: str
    trace_id: str
    evidence: list[Evidence]
    events: list[dict[str, Any]]
    insufficient_evidence: bool = False
    answerability: AnswerabilityDecision | None = None
    index_contract_id: str = ""
    run_contract_id: str = ""


class UploadResponse(BaseModel):
    document_id: str
    chunks_indexed: int
