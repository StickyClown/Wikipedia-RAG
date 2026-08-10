from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, model_validator

from wikipediarag.research_tool_registry import DEFAULT_RESEARCH_TOOL_MODE, ResearchToolMode


class ModelConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    driver: str = Field(pattern=r"^(openrouter|vllm|llamacpp|textgen_webui|openai_compatible|mock)$")
    base_url: str = Field(min_length=1, max_length=2048)
    endpoint_paths: dict[str, str] = Field(default_factory=dict)
    safe_headers: dict[str, str] = Field(default_factory=dict)
    tls_verify: bool = True
    enabled: bool = True
    credentials: dict[str, str] | None = None


class ModelConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    endpoint_paths: dict[str, str] | None = None
    safe_headers: dict[str, str] | None = None
    tls_verify: bool | None = None
    enabled: bool | None = None
    credentials: dict[str, str] | None = None


class ModelCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    provider_model: str = Field(min_length=1, max_length=512)
    operation: str = Field(pattern=r"^(chat|embedding|rerank)$")
    connection_id: str | None = None
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    capabilities: dict[str, Any] = Field(default_factory=dict)
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    dimensions: int | None = Field(default=None, ge=1)
    tokenizer_contract: dict[str, Any] = Field(default_factory=dict)
    model_defaults: dict[str, Any] = Field(default_factory=dict)
    thinking_capabilities: dict[str, Any] = Field(default_factory=dict)


class ModelPatch(BaseModel):
    provider_model: str | None = Field(default=None, min_length=1, max_length=512)
    connection_id: str | None = None
    input_modalities: list[str] | None = None
    capabilities: dict[str, Any] | None = None
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    dimensions: int | None = Field(default=None, ge=1)
    tokenizer_contract: dict[str, Any] | None = None
    model_defaults: dict[str, Any] | None = None
    thinking_capabilities: dict[str, Any] | None = None
    is_enabled: bool | None = None


class ModelConfigurationDraft(BaseModel):
    revision_id: str | None = None
    row_version: int | None = None
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)


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
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=50)
    retrieval_profile: str | None = Field(default=None, max_length=80)
    retrieval_overrides: dict[str, Any] = Field(default_factory=dict)


class RetrievalProfileOption(BaseModel):
    name: str
    compatible: bool
    reason_code: str | None = None


class RetrievalProfileCatalogResponse(BaseModel):
    resolved_default: str
    scope_contract_hash: str
    profiles: list[RetrievalProfileOption]


class SearchFilters(BaseModel):
    document_type: str | None = Field(default=None, min_length=1, max_length=120)
    language: str | None = Field(default=None, min_length=2, max_length=16)
    date_from: date | None = None
    date_to: date | None = None
    source: str | None = Field(default=None, min_length=1, max_length=240)
    source_kind: str | None = Field(default=None, min_length=1, max_length=120)
    source_id: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_date_range(self) -> SearchFilters:
        if self.date_from is not None and self.date_to is not None and self.date_from > self.date_to:
            raise ValueError("date_from must be <= date_to")
        return self


class FilterExpression(BaseModel):
    field: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    operator: str = Field(pattern=r"^(eq|contains|in|gte|lte)$")
    value: str | int | float | bool | date | list[str] | list[int] | list[float] | list[bool]
    scope: str = Field(default="document", pattern=r"^(document|chunk|source|metadata)$")
    source: str = Field(default="user", pattern=r"^(user|system|simple_filter)$")


class SearchHighlight(BaseModel):
    field: str
    fragments: list[str] = Field(default_factory=list)


class SearchFacetBucket(BaseModel):
    value: str
    count: int


class SearchFacet(BaseModel):
    field: str
    buckets: list[SearchFacetBucket] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=32000)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=50)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=500)
    cursor: str | None = Field(default=None, max_length=512)
    ranking_profile: str | None = Field(default=None, max_length=80)
    group_by_document: bool = False
    include_highlights: bool = True
    include_facets: bool = False
    filters: SearchFilters = Field(default_factory=SearchFilters)
    filter_expressions: list[FilterExpression] = Field(default_factory=list, max_length=32)


class SearchResult(BaseModel):
    chunk_id: str
    document_id: str
    document_version_id: str | None = None
    knowledge_base_id: str
    title: str
    snippet: str
    section_path: list[str] = Field(default_factory=list)
    source_url: str
    source_type: str
    document_type: str | None = None
    language: str | None = None
    document_date: date | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    score: float
    ranks: dict[str, int] = Field(default_factory=dict)
    highlights: list[SearchHighlight] = Field(default_factory=list)


class SearchDocumentGroup(BaseModel):
    document_id: str
    document_version_id: str | None = None
    knowledge_base_id: str
    title: str
    source_url: str
    source_type: str
    best_score: float
    hit_count: int
    hits: list[SearchResult] = Field(default_factory=list)


class SearchResponse(BaseModel):
    results: list[SearchResult]
    limit: int
    offset: int
    has_more: bool
    next_cursor: str | None = None
    facets: list[SearchFacet] = Field(default_factory=list)
    groups: list[SearchDocumentGroup] = Field(default_factory=list)
    facet_scope: str = "lexical_filtered_corpus"


class DocumentSection(BaseModel):
    section_id: str
    parent_section_id: str | None = None
    title: str
    level: int
    path: list[str] = Field(default_factory=list)
    ordinal: int
    locator: dict[str, Any] = Field(default_factory=dict)
    first_chunk_id: str | None = None
    last_chunk_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentStructureResponse(BaseModel):
    document_id: str
    document_version_id: str | None = None
    knowledge_base_id: str
    title: str
    source_type: str
    source_url: str | None = None
    sections: list[DocumentSection] = Field(default_factory=list)
    public_metadata: dict[str, Any] = Field(default_factory=dict)
    document_access: dict[str, Any] = Field(default_factory=dict)
    document_access_origin: str | None = None


class DocumentContextChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_version_id: str | None = None
    knowledge_base_id: str
    title: str
    section_path: list[str] = Field(default_factory=list)
    content: str
    source_url: str
    locator: dict[str, Any] = Field(default_factory=dict)
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    chunk_ordinal: int | None = None
    highlighted: bool = False


class DocumentContextResponse(BaseModel):
    document_id: str
    document_version_id: str | None = None
    anchor_chunk_id: str | None = None
    section_id: str | None = None
    chunks: list[DocumentContextChunk]
    limit: int
    offset: int


class DocumentSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=32000)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=500)


class DocumentSearchResult(BaseModel):
    chunk_id: str
    document_id: str
    document_version_id: str | None = None
    knowledge_base_id: str
    title: str
    snippet: str
    section_path: list[str] = Field(default_factory=list)
    source_url: str
    locator: dict[str, Any] = Field(default_factory=dict)
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    score: float
    ranks: dict[str, int] = Field(default_factory=dict)


class DocumentSearchResponse(BaseModel):
    document_id: str
    document_version_id: str | None = None
    results: list[DocumentSearchResult]
    limit: int
    offset: int
    has_more: bool


class QueryRunFeedbackRequest(BaseModel):
    rating: int | None = Field(default=None, ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryRunEvaluationRequest(BaseModel):
    evaluator: str = Field(min_length=1, max_length=120)
    scores: dict[str, float] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchRunStatus(StrEnum):
    received = "received"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ResearchPlanStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    archived = "archived"


class ResearchQuestionStatus(StrEnum):
    open = "open"
    running = "running"
    covered = "covered"
    partial = "partial"
    missing = "missing"
    conflicting = "conflicting"
    exhausted = "exhausted"
    failed = "failed"


class ResearchQuestionExecutionState(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"


class ResearchQuestionOutcome(StrEnum):
    covered = "covered"
    partial = "partial"
    exhausted = "exhausted"
    failed = "failed"


class ResearchContextPolicyOverride(BaseModel):
    productive_target: float | None = Field(default=None, gt=0.0, le=0.95)
    soft_limit: float | None = Field(default=None, gt=0.0, le=0.95)
    hard_input_limit: float | None = Field(default=None, gt=0.0, le=0.95)

    @model_validator(mode="after")
    def validate_ratio_order(self) -> ResearchContextPolicyOverride:
        productive = self.productive_target if self.productive_target is not None else 0.45
        soft = self.soft_limit if self.soft_limit is not None else 0.55
        hard = self.hard_input_limit if self.hard_input_limit is not None else 0.70
        if not productive <= soft <= hard:
            raise ValueError("context policy ratios must satisfy productive_target <= soft_limit <= hard_input_limit")
        return self


class ResearchPlanQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    ordinal: int = Field(default=1, ge=1, le=200)
    kind: str = Field(default="primary", min_length=1, max_length=80)


class ResearchPlanCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=32000)
    knowledge_base_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=3)
    retrieval_profile: str | None = Field(default=None, max_length=80)
    retrieval_overrides: dict[str, Any] = Field(default_factory=dict)
    context_policy_override: ResearchContextPolicyOverride | None = None
    tool_mode: ResearchToolMode = DEFAULT_RESEARCH_TOOL_MODE
    questions: list[ResearchPlanQuestion] = Field(default_factory=list, max_length=24)
    notes: str = Field(default="", max_length=4000)
    client_request_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_plan_scope(self) -> ResearchPlanCreate:
        seen: set[str] = set()
        for knowledge_base_id in self.knowledge_base_ids:
            normalized = str(knowledge_base_id).strip()
            if not normalized or normalized in seen:
                raise ValueError("knowledge_base_ids must contain unique non-empty ids")
            seen.add(normalized)
        if self.knowledge_base_id and self.knowledge_base_ids and self.knowledge_base_id not in seen:
            if len(self.knowledge_base_ids) >= 3:
                raise ValueError("knowledge_base_id must be included in knowledge_base_ids")
        seen_questions: set[str] = set()
        for question in self.questions:
            normalized = " ".join(question.question.casefold().replace("?", " ").replace(".", " ").split())
            if not normalized or normalized in seen_questions:
                raise ValueError("questions must contain unique non-empty items")
            seen_questions.add(normalized)
        return self


class ResearchPlanPatch(BaseModel):
    topic: str | None = Field(default=None, min_length=1, max_length=32000)
    knowledge_base_id: str | None = None
    knowledge_base_ids: list[str] | None = Field(default=None, max_length=3)
    retrieval_profile: str | None = Field(default=None, max_length=80)
    retrieval_overrides: dict[str, Any] | None = None
    context_policy_override: ResearchContextPolicyOverride | None = None
    tool_mode: ResearchToolMode | None = None
    questions: list[ResearchPlanQuestion] | None = Field(default=None, max_length=24)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_plan_patch(self) -> ResearchPlanPatch:
        if self.knowledge_base_ids is not None:
            seen: set[str] = set()
            for knowledge_base_id in self.knowledge_base_ids:
                normalized = str(knowledge_base_id).strip()
                if not normalized or normalized in seen:
                    raise ValueError("knowledge_base_ids must contain unique non-empty ids")
                seen.add(normalized)
        if self.questions is not None:
            seen_questions: set[str] = set()
            for question in self.questions:
                normalized = " ".join(question.question.casefold().replace("?", " ").replace(".", " ").split())
                if not normalized or normalized in seen_questions:
                    raise ValueError("questions must contain unique non-empty items")
                seen_questions.add(normalized)
        return self


class ResearchRunCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=32000)
    knowledge_base_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=3)
    retrieval_profile: str | None = Field(default=None, max_length=80)
    retrieval_overrides: dict[str, Any] = Field(default_factory=dict)
    context_policy_override: ResearchContextPolicyOverride | None = None
    tool_mode: ResearchToolMode = DEFAULT_RESEARCH_TOOL_MODE
    research_plan_id: str | None = Field(default=None, max_length=64)
    client_request_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_knowledge_base_scope(self) -> ResearchRunCreate:
        seen: set[str] = set()
        for knowledge_base_id in self.knowledge_base_ids:
            normalized = str(knowledge_base_id).strip()
            if not normalized or normalized in seen:
                raise ValueError("knowledge_base_ids must contain unique non-empty ids")
            seen.add(normalized)
        if self.knowledge_base_id and self.knowledge_base_ids and self.knowledge_base_id not in seen:
            if len(self.knowledge_base_ids) >= 3:
                raise ValueError("knowledge_base_id must be included in knowledge_base_ids")
        return self


class ResearchRunSummary(BaseModel):
    id: str
    knowledge_base_id: str
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=3)
    user_id: str | None = None
    topic: str
    retrieval_profile: str
    tool_mode: ResearchToolMode = DEFAULT_RESEARCH_TOOL_MODE
    status: ResearchRunStatus
    progress: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None
    error_code: str | None = None
    active_job_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class ResearchPlanSummary(BaseModel):
    id: str
    knowledge_base_id: str
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=3)
    user_id: str | None = None
    topic: str
    retrieval_profile: str
    tool_mode: ResearchToolMode = DEFAULT_RESEARCH_TOOL_MODE
    status: ResearchPlanStatus
    notes: str = ""
    question_count: int = 0
    approved_run_id: str | None = None
    approved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ResearchPlanDetail(BaseModel):
    plan: ResearchPlanSummary
    questions: list[ResearchPlanQuestion] = Field(default_factory=list)
    retrieval_overrides: dict[str, Any] = Field(default_factory=dict)
    context_policy: dict[str, Any] = Field(default_factory=dict)


class ResearchQuestionRecord(BaseModel):
    id: str
    question: str
    ordinal: int
    kind: str
    status: ResearchQuestionStatus
    execution_state: ResearchQuestionExecutionState = ResearchQuestionExecutionState.pending
    outcome: ResearchQuestionOutcome | None = None
    attempt_count: int = Field(default=0, ge=0)
    rewrite_count: int = Field(default=0, ge=0)
    depth: int = Field(default=0, ge=0)
    budget: dict[str, Any] = Field(default_factory=dict)
    acceptance: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchEvidenceRecord(BaseModel):
    id: str
    question_id: str | None = None
    chunk_id: str
    document_id: str | None = None
    document_version_id: str | None = None
    knowledge_base_id: str
    evidence_ref: str
    title: str
    source_url: str
    section_path: list[str] = Field(default_factory=list)
    content_abstract: str
    evidence_fingerprint: str | None = None
    support_status: str
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchClaimRecord(BaseModel):
    id: str
    question_id: str | None = None
    claim_text: str
    support_status: str
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchCoverageRecord(BaseModel):
    id: str
    question_id: str
    status: str
    required_evidence_count: int
    linked_evidence_ids: list[str] = Field(default_factory=list)
    reason: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class ResearchReflectionRecord(BaseModel):
    id: str
    episode_id: str | None = None
    reflection_type: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ResearchEpisodeRecord(BaseModel):
    id: str
    query_run_id: str | None = None
    episode_index: int
    question_id: str | None = None
    status: str
    stage: str
    context_summary: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class ResearchToolCallRecord(BaseModel):
    id: str
    episode_id: str | None = None
    question_id: str | None = None
    query_run_id: str | None = None
    tool_name: str
    tool_query_hash: str
    status: str
    result_summary: dict[str, Any] = Field(default_factory=dict)
    safe_metadata: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ResearchDecisionRecord(BaseModel):
    id: str
    episode_id: str | None = None
    question_id: str | None = None
    decision_type: str
    selected_strategy: str
    reason: str
    evidence_gain: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ResearchClaimRelationRecord(BaseModel):
    id: str
    source_claim_id: str
    target_claim_id: str
    relation: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ResearchRunDetail(BaseModel):
    run: ResearchRunSummary
    questions: list[ResearchQuestionRecord] = Field(default_factory=list)
    coverage: list[ResearchCoverageRecord] = Field(default_factory=list)
    evidence: list[ResearchEvidenceRecord] = Field(default_factory=list)
    claims: list[ResearchClaimRecord] = Field(default_factory=list)
    relations: list[ResearchClaimRelationRecord] = Field(default_factory=list)
    reflections: list[ResearchReflectionRecord] = Field(default_factory=list)
    episodes: list[ResearchEpisodeRecord] = Field(default_factory=list)
    tool_calls: list[ResearchToolCallRecord] = Field(default_factory=list)
    decisions: list[ResearchDecisionRecord] = Field(default_factory=list)
    final_report: dict[str, Any] = Field(default_factory=dict)


class ResearchRunListResponse(BaseModel):
    runs: list[ResearchRunSummary] = Field(default_factory=list)


class ResearchPlanListResponse(BaseModel):
    plans: list[ResearchPlanSummary] = Field(default_factory=list)


class ResearchRunActionResponse(BaseModel):
    run_id: str
    status: ResearchRunStatus | str
    job_id: str | None = None


class ResearchPlanActionResponse(BaseModel):
    plan_id: str
    status: ResearchPlanStatus | str
    run_id: str | None = None


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


class KnowledgeBasePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    platform_role: str = "USER"
    is_disabled: bool = False


class UserPatch(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    platform_role: str | None = None
    is_disabled: bool | None = None


class TenantCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)


class TenantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    group_type: str = "LOCAL"
    external_id: str | None = Field(default=None, max_length=500)
    member_user_ids: list[str] = Field(default_factory=list, max_length=1000)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    member_user_ids: list[str] | None = Field(default=None, max_length=1000)


class KnowledgeBaseGrantCreate(BaseModel):
    subject_type: str
    subject_id: str = Field(min_length=1, max_length=500)
    role: str


class KnowledgeBaseGrantPatch(BaseModel):
    role: str


class LocalLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)
    remember_me: bool = False


class LocalPasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class TenantSelectionRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)


class AuthUserResponse(BaseModel):
    id: str
    username: str | None = None
    display_name: str | None = None
    platform_role: str
    password_change_required: bool = False


class AuthSessionResponse(BaseModel):
    authenticated: bool
    user: AuthUserResponse | None = None
    active_tenant_id: str | None = None
    tenant_role: str | None = None
    authentication_method: str | None = None
    session_id: str | None = None
    csrf_token: str | None = None
    expires_at: datetime | None = None


class AuthOidcStartResponse(BaseModel):
    authorization_url: str
    expires_at: datetime


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
    knowledge_base_id: str = ""
    title: str
    section_path: list[str]
    content: str
    source_url: str
    scores: dict[str, float] = Field(default_factory=dict)
    ranks: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    document_id: str = ""
    content_unit_id: str = ""
    supporting_chunk_ids: list[str] = Field(default_factory=list)
    provenance_refs: list[str] = Field(default_factory=list)


class AnswerabilityStatus(StrEnum):
    answerable = "ANSWERABLE"
    partial = "PARTIAL"
    unanswerable = "UNANSWERABLE"
    conflicting = "CONFLICTING"


class AnswerabilityDecision(BaseModel):
    version: str = "answerability_gate_v4"
    status: AnswerabilityStatus
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    required_parts: list[str] = Field(default_factory=list)
    covered_parts: list[str] = Field(default_factory=list)
    missing_parts: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fill_reason_codes(self) -> AnswerabilityDecision:
        if not self.reason_codes:
            self.reason_codes = [self.reason, f"status_{self.status.value.lower()}"]
        return self


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


class SourceCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=240)
    knowledge_base_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    document_access_default: dict[str, Any] | None = None
    refresh_interval_seconds: int | None = Field(default=None, ge=60)


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    status: str | None = Field(default=None, pattern=r"^(active|disabled|failed)$")
    config: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    refresh_interval_seconds: int | None = Field(default=None, ge=60)


class SourceResponse(BaseModel):
    id: str
    knowledge_base_id: str
    kind: str
    name: str
    status: str
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    document_access_default: dict[str, Any] = Field(default_factory=dict)
    refresh_interval_seconds: int | None = None
    last_sync_run_id: str | None = None
    last_sync_status: str | None = None
    last_synced_at: datetime | None = None
    next_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DocumentAccessPatch(BaseModel):
    policy: str = Field(pattern=r"^(kb|tenant|restricted)$")
    user_ids: list[str] = Field(default_factory=list, max_length=1000)
    group_ids: list[str] = Field(default_factory=list, max_length=1000)


class SourceAccessPatch(DocumentAccessPatch):
    apply_to_existing: bool = True


class DocumentAccessResponse(BaseModel):
    document_id: str
    knowledge_base_id: str
    document_access: dict[str, Any]
    document_access_origin: str


class SourceAccessResponse(BaseModel):
    source_id: str
    knowledge_base_id: str
    document_access_default: dict[str, Any]
    updated_documents: int = 0


class AccessGroupResponse(BaseModel):
    id: str
    name: str
    group_type: str
    external_id: str | None = None


class SourceHealthResponse(BaseModel):
    source_id: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class SourceSyncRequest(BaseModel):
    mode: str = Field(default="incremental", pattern=r"^(full|incremental)$")


class SourceSyncResponse(BaseModel):
    source_id: str
    run_id: str
    job_id: str
    status: str


class SourceSyncRunResponse(BaseModel):
    id: str
    source_id: str
    knowledge_base_id: str
    mode: str
    status: str
    cursor_before: dict[str, Any] = Field(default_factory=dict)
    cursor_after: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UploadSessionCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    content_type: str = Field(default="application/octet-stream", max_length=200)
    size_bytes: int = Field(ge=1)
    checksum_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    knowledge_base_id: str | None = None
    parser_profile: str = Field(default="standard", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UploadSessionAccepted(BaseModel):
    upload_session_id: str
    upload_url: str
    expires_at: datetime
    required_headers: dict[str, str] = Field(default_factory=dict)


class UploadBatchItemCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    content_type: str = Field(default="application/octet-stream", max_length=200)
    size_bytes: int = Field(ge=1)
    checksum_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    parser_profile: str = Field(default="standard", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UploadBatchCreate(BaseModel):
    knowledge_base_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    items: list[UploadBatchItemCreate] = Field(min_length=1, max_length=25)


class UploadBatchItemAccepted(UploadSessionAccepted):
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str


class UploadBatchAccepted(BaseModel):
    batch_id: str
    knowledge_base_id: str
    status: str
    total_items: int
    items: list[UploadBatchItemAccepted]


class UploadSessionComplete(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class UploadCompleteResponse(BaseModel):
    document_id: str
    document_version_id: str
    job_id: str
    status: str


class UploadBatchItemStatus(BaseModel):
    upload_session_id: str
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    status: str
    upload_completed_at: datetime | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    job_id: str | None = None
    job_status: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class UploadBatchStatus(BaseModel):
    batch_id: str
    knowledge_base_id: str
    status: str
    total_items: int
    completed_items: int
    failed_items: int
    cancelled_items: int
    pending_items: int
    items: list[UploadBatchItemStatus]


class DocumentReprocessResponse(BaseModel):
    document_id: str
    document_version_id: str
    job_id: str
    status: str


class DocumentDeleteResponse(BaseModel):
    document_id: str
    job_id: str | None = None
    lifecycle_state: str
    purge_after: datetime | None = None
