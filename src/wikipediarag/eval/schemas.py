from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TaskFamily = Literal[
    "single_hop_factual",
    "alias_redirect_rare",
    "deep_section_fact",
    "comparison_multi_hop",
    "unanswerable",
    "hard_negative",
]

ExpectedMode = Literal["normal_sufficient", "extended_beneficial", "extended_required", "unanswerable"]
EvalGeneratePhase = Literal["preparing", "family_generation", "writing_dataset", "completed", "failed"]
EvalGenerateState = Literal["running", "completed", "failed"]
EvalRunState = Literal["running", "completed", "failed"]
EvalRunPhase = Literal["preparing", "config_running", "completed", "failed"]
RetrievalEvalState = Literal["running", "completed", "failed"]
RetrievalEvalPhase = Literal["preparing", "config_running", "completed", "failed"]
RetrievalEvalConfigStatus = Literal["completed", "unsupported", "failed"]
ReleaseGateState = Literal["running", "completed", "failed"]
ReleaseGatePhase = Literal[
    "preparing",
    "dev_answer",
    "dev_retrieval",
    "test_answer",
    "test_retrieval",
    "gate_evaluation",
    "completed",
    "failed",
]
EvalGenerateRejectReason = Literal[
    "invalid_generator_json",
    "verifier_rejected",
    "invalid_verifier_json",
    "local_validation_rejected",
    "provider_error",
]
EvalGenerateEventType = Literal[
    "run_started",
    "family_started",
    "attempt_started",
    "candidate_generated",
    "candidate_rejected",
    "provider_error",
    "task_accepted",
    "family_completed",
    "run_completed",
    "run_failed",
]


class GoldEvidence(BaseModel):
    evidence_id: str
    document_id: str
    section_id: str
    chunk_id: str
    quote: str
    supports_claim_ids: list[str] = Field(default_factory=list)
    hop: int = Field(default=1, ge=1)
    title: str = ""
    source_url: str = ""


class EvalTask(BaseModel):
    task_id: str
    question: str
    task_family: TaskFamily
    reference_answer: str
    accepted_answers: list[str]
    unanswerable: bool
    expected_mode: ExpectedMode
    gold_page_ids: list[str]
    gold_section_ids: list[str]
    gold_chunk_ids: list[str]
    gold_evidence: list[GoldEvidence]
    reasoning_path: list[str]
    generator_alias: str
    verifier_alias: str
    zim_checksum: str
    snapshot_id: str
    index_version: str
    retrieval_profile_hash: str
    language: str = "ru"
    tags: list[str] = Field(default_factory=list)
    generation_seed: int = 0
    hard_negative_page_ids: list[str] = Field(default_factory=list)


class EvalDatasetManifest(BaseModel):
    dataset_name: str
    dataset_version: str
    dataset_hash: str
    task_count: int
    created_at: str
    snapshot_id: str
    index_version: str
    zim_checksum: str
    retrieval_profile_hash: str
    generator_alias: str
    verifier_alias: str
    jsonl_path: str


class EvalGenerateStats(BaseModel):
    accepted: int = 0
    rejected: int = 0
    errors: int = 0
    retries: int = 0
    family_accepted: dict[TaskFamily, int] = Field(default_factory=dict)
    family_targets: dict[TaskFamily, int] = Field(default_factory=dict)


class EvalGenerateProgressEvent(BaseModel):
    event: EvalGenerateEventType
    elapsed_seconds: float = Field(default=0.0, ge=0)
    count_target: int = Field(ge=1)
    total_accepted: int = Field(default=0, ge=0)
    family: TaskFamily | None = None
    family_target: int | None = Field(default=None, ge=0)
    family_accepted: int | None = Field(default=None, ge=0)
    attempt: int | None = Field(default=None, ge=1)
    question: str = ""
    reason: EvalGenerateRejectReason | None = None
    dataset_name: str = ""
    run_id: str = ""
    snapshot_id: str = ""
    index_version: str = ""
    stats: EvalGenerateStats | None = None


class EvalGenerateModelRef(BaseModel):
    alias: str
    provider: str
    model: str


class EvalGenerateRuntimeConfig(BaseModel):
    run_id: str
    count: int = Field(ge=1)
    concurrency: int = Field(ge=1, le=128)
    generator: EvalGenerateModelRef
    verifier: EvalGenerateModelRef
    family_weights: dict[TaskFamily, float] = Field(default_factory=dict)
    family_targets: dict[TaskFamily, int] = Field(default_factory=dict)


class EvalGenerateAcceptedTaskRecord(BaseModel):
    question: str
    task_family: TaskFamily
    gold_page_ids: list[str] = Field(default_factory=list)
    gold_chunk_ids: list[str] = Field(default_factory=list)
    reasoning_path: list[str] = Field(default_factory=list)
    generation_seed: int = 0


class EvalGenerateRunStatus(BaseModel):
    run_id: str
    state: EvalGenerateState
    phase: EvalGeneratePhase
    started_at: str
    updated_at: str
    active_family: TaskFamily | None = None
    current_attempt: int | None = Field(default=None, ge=1)
    count_target: int = Field(ge=1)
    family_targets: dict[TaskFamily, int] = Field(default_factory=dict)
    family_attempts_started: dict[TaskFamily, int] = Field(default_factory=dict)
    config: EvalGenerateRuntimeConfig
    snapshot_id: str
    index_version: str
    zim_checksum: str
    retrieval_profile_hash: str
    stats: EvalGenerateStats
    accepted_tasks: list[EvalGenerateAcceptedTaskRecord] = Field(default_factory=list)
    dataset_name: str = ""
    dataset_hash: str = ""
    manifest_path: str = ""
    error_message: str = ""


class CandidateRef(BaseModel):
    chunk_id: str
    document_id: str = ""
    section_id: str = ""
    title: str = ""
    source_url: str = ""
    rank: int
    stage: str
    scores: dict[str, float] = Field(default_factory=dict)


class EvalConfig(BaseModel):
    config_id: str
    retrieval_profile: str
    retrieval_overrides: dict[str, Any]
    mode: Literal["normal", "extended", "auto"] = "normal"
    config_hash: str
    model_aliases: dict[str, str] = Field(default_factory=dict)


class TaskScores(BaseModel):
    page_recall: dict[str, float]
    section_recall: dict[str, float]
    chunk_recall: dict[str, float]
    mrr_at_10: float
    ndcg_at_10: float
    full_hop_recall: float
    path_completion: float
    reranker_gold_delta: float | None = None
    exact_match: float
    token_f1: float
    unanswerable_accuracy: float
    citation_precision: float
    citation_recall: float
    unsupported_claim_rate: float
    kiwix_url_ok: float


class EvalTaskResult(BaseModel):
    task_id: str
    config_id: str
    config_hash: str
    status: Literal["completed", "failed", "reused"]
    question: str
    answer: str = ""
    citations: list[str] = Field(default_factory=list)
    cited_chunk_ids: list[str] = Field(default_factory=list)
    retrieved_candidates: list[CandidateRef] = Field(default_factory=list)
    reranked_candidates: list[CandidateRef] = Field(default_factory=list)
    mode_selected: Literal["normal", "harness"] = "normal"
    latency_ms: dict[str, int] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    scores: TaskScores | None = None
    errors: list[str] = Field(default_factory=list)
    query_run_id: str | None = None
    trace_id: str | None = None
    corpus: dict[str, str] = Field(default_factory=dict)
    model_aliases: dict[str, str] = Field(default_factory=dict)
    contract_ids: dict[str, str] = Field(default_factory=dict)


class ConfigSummary(BaseModel):
    config_id: str
    config_hash: str
    task_count: int
    metrics: dict[str, float]
    by_family: dict[str, dict[str, float]]
    failed_task_ids: list[str]
    errors: list[str] = Field(default_factory=list)
    contract_ids: dict[str, list[str]] = Field(default_factory=dict)


class EvalRunManifest(BaseModel):
    run_id: str
    suite: str
    dataset_hash: str
    dataset_path: str
    created_at: str
    config_summaries: list[ConfigSummary]
    run_dir: str


class EvalRunStatus(BaseModel):
    run_id: str
    state: EvalRunState
    phase: EvalRunPhase
    suite: str
    dataset_hash: str
    dataset_path: str
    run_dir: str
    total_configs: int = Field(ge=0)
    total_tasks: int = Field(ge=0)
    total_task_runs: int = Field(ge=0)
    processed_task_runs: int = Field(default=0, ge=0)
    completed_task_runs: int = Field(default=0, ge=0)
    failed_task_runs: int = Field(default=0, ge=0)
    current_config_id: str = ""
    current_config_index: int = Field(default=0, ge=0)
    current_task_id: str = ""
    current_task_index: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    eta_seconds: float | None = None
    avg_seconds_per_task: float = Field(default=0.0, ge=0)
    last_latency_ms: int = Field(default=0, ge=0)
    started_at: str
    updated_at: str
    error_message: str = ""


class RetrievalTaskScores(BaseModel):
    page_recall: dict[str, float]
    section_recall: dict[str, float]
    chunk_recall: dict[str, float]
    mrr_at_10: float
    ndcg_at_10: float
    full_hop_recall: float
    path_completion: float
    reranker_gold_delta: float | None = None
    retrieved_gold_leak_rate: float = 0.0
    false_positive_evidence_rate: float = 0.0
    hard_negative_page_hit_at_10: float = 0.0
    hard_negative_page_hit_at_20: float = 0.0
    gold_vs_hard_negative_rank_margin: float | None = None


class RetrievalTaskResult(BaseModel):
    task_id: str
    config_id: str
    config_hash: str
    status: Literal["completed", "failed"]
    question: str
    task_family: TaskFamily
    unanswerable: bool
    batch_index: int
    task_index: int
    retrieved_candidates: list[CandidateRef] = Field(default_factory=list)
    reranked_candidates: list[CandidateRef] = Field(default_factory=list)
    final_candidates: list[CandidateRef] = Field(default_factory=list)
    latency_ms: dict[str, int] = Field(default_factory=dict)
    scores: RetrievalTaskScores | None = None
    errors: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    corpus: dict[str, str] = Field(default_factory=dict)
    model_aliases: dict[str, str] = Field(default_factory=dict)
    contract_ids: dict[str, str] = Field(default_factory=dict)


class RetrievalConfigSummary(BaseModel):
    config_id: str
    config_hash: str
    status: RetrievalEvalConfigStatus
    task_count: int
    metrics: dict[str, float]
    by_family: dict[str, dict[str, float]]
    failed_task_ids: list[str]
    errors: list[str] = Field(default_factory=list)
    contract_ids: dict[str, list[str]] = Field(default_factory=dict)


class RetrievalEvalStatus(BaseModel):
    run_id: str
    state: RetrievalEvalState
    phase: RetrievalEvalPhase
    suite: str
    dataset_hash: str
    dataset_path: str
    run_dir: str
    batch_size: int = Field(ge=1)
    total_configs: int = Field(ge=0)
    supported_configs: int = Field(ge=0)
    total_tasks: int = Field(ge=0)
    total_task_runs: int = Field(ge=0)
    processed_task_runs: int = Field(default=0, ge=0)
    completed_task_runs: int = Field(default=0, ge=0)
    failed_task_runs: int = Field(default=0, ge=0)
    current_config_id: str = ""
    current_config_index: int = Field(default=0, ge=0)
    current_task_id: str = ""
    current_task_index: int = Field(default=0, ge=0)
    current_batch: int = Field(default=0, ge=0)
    total_batches: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    eta_seconds: float | None = None
    avg_seconds_per_task: float = Field(default=0.0, ge=0)
    last_latency_ms: int = Field(default=0, ge=0)
    started_at: str
    updated_at: str
    error_message: str = ""


class RetrievalRunManifest(BaseModel):
    run_id: str
    suite: str
    dataset_hash: str
    dataset_path: str
    created_at: str
    batch_size: int
    config_summaries: list[RetrievalConfigSummary]
    run_dir: str


class ReleaseGateStatus(BaseModel):
    run_id: str
    state: ReleaseGateState
    phase: ReleaseGatePhase
    suite: str
    api: str
    run_dir: str
    total_stages: int = Field(ge=0)
    stage_index: int = Field(default=0, ge=0)
    current_stage: str = ""
    child_run_id: str = ""
    child_run_dir: str = ""
    child_processed_task_runs: int = Field(default=0, ge=0)
    child_total_task_runs: int = Field(default=0, ge=0)
    child_failed_task_runs: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    passed: bool | None = None
    blocking_failures: int = Field(default=0, ge=0)
    started_at: str
    updated_at: str
    error_message: str = ""
