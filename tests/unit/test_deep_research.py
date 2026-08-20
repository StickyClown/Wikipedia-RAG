from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import wikipediarag.deep_research as deep_research
import wikipediarag.research_tools as research_tools
from wikipediarag.deep_research import (
    EpisodeExecutionOutcome,
    ResearchContextBudget,
    _claim_record_status,
    _claims_from_evidence,
    _coverage_status,
    _deterministic_synthesis,
    _ensure_original_research_query,
    _episode_derived_questions,
    _episode_progress_made,
    _retrieval_rewrite_count,
    _reuse_terminal_duplicate_question,
    _run_deadline_expired,
    _run_deadline_remaining,
    _terminalize_question_after_tick,
    build_public_research_report,
    build_research_questions,
    context_budget_for_profile,
    context_policy_for_profile,
    pack_research_context,
    visible_research_evidence,
)
from wikipediarag.repository import (
    _derived_question_priority,
    append_research_questions,
    create_query_run,
    create_research_episode,
    create_research_resume_job,
    create_research_run,
    create_research_tool_call,
    load_next_research_question,
    load_resumable_research_episode,
    request_research_cancel,
    request_research_pause,
    research_evidence_fingerprint,
    select_next_question,
    terminalize_research_questions,
    touch_research_heartbeat,
    update_job,
    update_research_episode,
    update_research_run,
    update_research_tool_call,
    upsert_research_evidence_record,
)
from wikipediarag.research_planner import (
    PlannerProposal,
    ResearchDerivedQuestion,
    _parse_planner_payload,
    _planner_prompt_json,
    _repair_planner_payload,
    _validate_planner_output,
    fallback_research_plan,
)
from wikipediarag.research_tool_registry import DEFAULT_RESEARCH_TOOL_MODE
from wikipediarag.research_tools import (
    ToolRequest,
    ToolResult,
    assert_safe_tool_metadata,
    classify_tool_error,
    research_tool_call_metadata,
)
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import (
    AnswerabilityDecision,
    AnswerabilityStatus,
    Evidence,
    JobStatus,
    ResearchContextPolicyOverride,
    ResearchPlanCreate,
    ResearchPlanPatch,
    ResearchPlanQuestion,
    ResearchRunCreate,
    RetrievalResult,
)


class _EmptyResult:
    def mappings(self) -> _EmptyResult:
        return self

    def first(self) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(())


class _RowsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> _RowsResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class _RecordingConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[object | None] = []

    async def execute(self, statement: object, _params: object | None = None) -> _EmptyResult | _RowsResult:
        self.sql.append(str(statement))
        self.params.append(_params)
        return _EmptyResult()


class _ResearchQuestionConnection(_RecordingConnection):
    def __init__(self, existing: list[dict[str, Any]]) -> None:
        super().__init__()
        self.existing = existing

    async def execute(self, statement: object, _params: object | None = None) -> _RowsResult:
        sql = str(statement)
        self.sql.append(sql)
        self.params.append(_params)
        if "SELECT id, question, ordinal, kind, status, acceptance, metadata" in sql:
            return _RowsResult(self.existing)
        if "COALESCE(MAX(ordinal), 0) AS max_ordinal" in sql:
            return _RowsResult([{"max_ordinal": len(self.existing), "total": len(self.existing)}])
        return _RowsResult([])


def test_context_budget_uses_declared_model_context_percentages() -> None:
    profile = get_retrieval_profile("test_mock")

    budget = context_budget_for_profile(profile, stage="planner")
    verifier_budget = context_budget_for_profile(profile, stage="verifier")
    policy = context_policy_for_profile(profile, stage="planner")

    assert budget.max_context_tokens == 80_000
    assert budget.productive_target_tokens == 36_000
    assert budget.soft_limit_tokens == 44_000
    assert budget.hard_input_limit_tokens == 56_000
    assert budget.output_reserve_tokens == 12_000
    assert budget.safety_reserve_tokens == 12_000
    assert verifier_budget.max_context_tokens == 24_000
    assert verifier_budget.productive_target_tokens == 10_800
    assert verifier_budget.soft_limit_tokens == 13_200
    assert verifier_budget.hard_input_limit_tokens == 16_800
    assert profile.postprocess.max_context_tokens == 12_000
    assert policy["ratios"] == {
        "productive_target": 0.45,
        "soft_limit": 0.55,
        "hard_input_limit": 0.70,
        "output_reserve": 0.15,
        "safety_reserve": 0.15,
    }


def test_context_policy_override_changes_runtime_budget_without_changing_default() -> None:
    profile = get_retrieval_profile("test_mock")
    override = ResearchContextPolicyOverride(
        productive_target=0.35,
        soft_limit=0.45,
        hard_input_limit=0.60,
    )

    policy = context_policy_for_profile(profile, override, stage="planner")
    budget = context_budget_for_profile(profile, policy, stage="planner")

    assert policy["override"] == {
        "productive_target": 0.35,
        "soft_limit": 0.45,
        "hard_input_limit": 0.60,
    }
    assert budget.productive_target_tokens == 28_000
    assert budget.soft_limit_tokens == 36_000
    assert budget.hard_input_limit_tokens == 48_000


def test_planner_proposal_accepts_minimal_unicode_and_rejects_extra_or_wrong_fields() -> None:
    valid = PlannerProposal.model_validate(
        {
            "search_queries": ["Project Lantern RB-17"],
            "tool_candidates": ["extended_search"],
            "discovered_questions": ["Что известно про LTN-42?"],
        }
    )

    assert valid.discovered_questions == ["Что известно про LTN-42?"]
    assert (
        PlannerProposal.model_validate(
            {"search_queries": [], "tool_candidates": [], "discovered_questions": []}
        ).search_queries
        == []
    )
    with pytest.raises(ValueError):
        PlannerProposal.model_validate(
            {
                "search_queries": ["Project Lantern"],
                "tool_candidates": ["extended_search"],
                "discovered_questions": [],
                "finish": True,
            }
        )
    with pytest.raises(ValueError):
        PlannerProposal.model_validate(
            {"search_queries": [123], "tool_candidates": ["extended_search"], "discovered_questions": []}
        )
    with pytest.raises(ValueError):
        PlannerProposal.model_validate({"search_queries": ["question"]})
    with pytest.raises(ValueError):
        PlannerProposal.model_validate(
            {
                "search_queries": ["question"],
                "tool_candidates": ["extended_search"],
                "discovered_questions": ["8a41938f-2231-448a-8d30-8e2f1d2e6212"],
            }
        )


def test_stage_failure_diagnostics_exclude_exception_text_and_sensitive_fields() -> None:
    failure = deep_research._safe_stage_failure(
        operation="create_research_episode",
        stage="before_create_episode",
        research_run_id="run",
        question_id="question",
        attempt_id="1",
        episode_id=None,
        tool_call_id=None,
        exc=ValueError("raw query SECRET provider_payload should not be copied"),
    )

    serialized = json.dumps(failure, ensure_ascii=False)
    assert failure["operation"] == "create_research_episode"
    assert failure["stage"] == "before_create_episode"
    assert "SECRET" not in serialized
    assert "raw query" not in serialized
    assert "provider_payload" not in serialized


def test_controller_routes_document_tool_only_from_visible_evidence() -> None:
    proposal = PlannerProposal(
        search_queries=["owner"],
        tool_candidates=["search_within_document"],
        discovered_questions=[],
    )

    decision = deep_research._controller_planner_decision(
        proposal,
        question_text="Who owns the service?",
        allowed_tools=("extended_search", "search_within_document"),
        timeout_seconds=30,
        visible_evidence_records=[{"id": "evidence-1", "section_path": ["Ownership"]}],
        previous_tool_query_hashes=set(),
    )

    assert isinstance(decision.tool_request, ToolRequest)
    assert decision.tool_name == "search_within_document"
    assert decision.tool_args == {"source_evidence_id": "evidence-1"}


def test_controller_source_router_uses_unseen_relevant_alternative() -> None:
    proposal = PlannerProposal(
        search_queries=["owner"],
        tool_candidates=["search_within_document"],
        discovered_questions=[],
    )
    visible = [
        {
            "id": "e1",
            "document_id": "d1",
            "title": "Ownership",
            "content_abstract": "service owner team",
            "section_path": ["Ownership"],
            "score": 0.9,
        },
        {
            "id": "e2",
            "document_id": "d2",
            "title": "Ownership",
            "content_abstract": "service owner team",
            "section_path": ["Ownership"],
            "score": 0.8,
        },
    ]

    decision = deep_research._controller_planner_decision(
        proposal,
        question_text="Who owns the service?",
        question_id="q1",
        allowed_tools=("search_within_document",),
        timeout_seconds=30,
        visible_evidence_records=visible,
        previous_tool_query_hashes=set(),
        tool_routing_history=[
            {"question_id": "q1", "validated_args": {"source_evidence_id": "e1"}},
        ],
    )

    assert decision.tool_args == {"source_evidence_id": "e2"}
    assert decision.routing_metadata["reason"] == "new_relevant_source"


def test_planner_prompt_json_serializes_runtime_uuid_context() -> None:
    context_id = uuid.uuid4()

    payload = _planner_prompt_json({"context": {"question_id": context_id}})

    assert json.loads(payload)["context"]["question_id"] == str(context_id)


def test_research_tool_query_always_preserves_original_question() -> None:
    query = _ensure_original_research_query(
        "Какой срок удержания логов применить к кейсу Aster-North и почему?",
        "InformerNort Кейс Aster-North лог retention purpose",
    )

    assert query.startswith("Какой срок удержания логов применить к кейсу Aster-North и почему?")
    assert "InformerNort" in query


def test_planner_payload_repair_does_not_add_controller_fields() -> None:
    payload = {"search_queries": ["question"], "tool_candidates": ["extended_search"], "discovered_questions": []}
    assert _repair_planner_payload(payload) == payload


def test_planner_payload_markdown_or_invalid_json_is_rejected_for_fallback() -> None:
    payload, error = _parse_planner_payload(
        "planner output:\n"
        "```json\n"
        "{“next_action”: “finish”, “tool_name”: null, "
        "“tool_query”: null, “tool_args”: {}, “derived_questions”: [], "
        "“needed_evidence”: [], “stop_reason”: “done”,}\n"
        "```"
    )

    assert payload is None
    assert error is not None
    fallback = fallback_research_plan("Вопрос с Unicode: кто владелец RB-17?")
    assert fallback.search_queries == ["Вопрос с Unicode: кто владелец RB-17?"]
    assert fallback.tool_candidates == ["extended_search"]


def test_planner_schema_rejects_controller_fields() -> None:
    with pytest.raises(ValueError):
        PlannerProposal.model_validate(
            {
                "search_queries": ["owner"],
                "tool_candidates": ["search_within_document"],
                "discovered_questions": [],
                "tool_args": {"source_evidence_id": "E1"},
            }
        )


def test_validate_planner_output_blocks_document_tools_outside_mode_allowlist() -> None:
    with pytest.raises(Exception, match="outside the current allowlist"):
        _validate_planner_output(
            {
                "search_queries": ["owner"],
                "tool_candidates": ["search_within_document"],
                "discovered_questions": [],
            },
            allowed_tool_names=("extended_search",),
        )


def test_build_research_questions_is_bounded_and_deterministic() -> None:
    questions = build_research_questions(
        "Исследуй Qwen локально? и оцени экономию контекста? и проверь риски? и сделай вывод?"
    )

    assert questions == [
        "Исследуй Qwen локально? и оцени экономию контекста? и проверь риски? и сделай вывод?",
        "Исследуй Qwen локально?",
        "оцени экономию контекста?",
        "проверь риски?",
        "сделай вывод?",
    ]


def test_context_packer_trims_in_order_and_does_not_leak_full_trace() -> None:
    budget = ResearchContextBudget(
        max_context_tokens=300,
        productive_target_tokens=100,
        soft_limit_tokens=120,
        hard_input_limit_tokens=160,
        output_reserve_tokens=45,
        safety_reserve_tokens=45,
    )
    long_abstract = "evidence " * 500
    evidence = [
        {
            "id": f"e-{index}",
            "evidence_ref": f"E{index}",
            "title": f"Title {index}",
            "content_abstract": long_abstract,
            "support_status": "supports",
            "score": 1.0 / index,
            "metadata": {"full_trace": "SECRET_TRACE_SHOULD_NOT_LEAK"},
            "content": "FULL_RAW_CHUNK_SHOULD_NOT_LEAK",
            "final_report": "DRAFT_REPORT_SHOULD_NOT_LEAK",
        }
        for index in range(1, 9)
    ]
    reflections = [
        {"id": "r1", "body": "old reflection " * 120},
        {"id": "r2", "body": "middle reflection " * 120},
        {"id": "r3", "body": "latest reflection"},
    ]

    packed = pack_research_context(
        topic="topic",
        current_question="question?",
        run_progress={"stage": "running", "full_trace": "RUN_TRACE_SHOULD_NOT_LEAK"},
        coverage_records=[{"id": "c1", "question_id": "q1", "status": "missing", "reason": "gap"}],
        evidence_records=evidence,
        reflections=reflections,
        budget=budget,
    )
    serialized = json.dumps(packed, ensure_ascii=False)

    assert packed["envelope"]["pinned"]["topic"] == "topic"
    assert packed["envelope"]["pinned"]["current_question"] == "question?"
    assert packed["trimming"] == ["older_reflections", "low_value_evidence_abstracts", "raw_passages"]
    assert len(packed["envelope"]["reflections"]) == 1
    assert len(packed["envelope"]["evidence"]) == 3
    assert "SECRET_TRACE_SHOULD_NOT_LEAK" not in serialized
    assert "FULL_RAW_CHUNK_SHOULD_NOT_LEAK" not in serialized
    assert "DRAFT_REPORT_SHOULD_NOT_LEAK" not in serialized


def test_visible_research_evidence_uses_current_retrievability_only() -> None:
    records = [
        {
            "id": "visible",
            "metadata": {
                "document_metadata": {"document_access": {"policy": "restricted", "user_ids": ["u1"], "group_ids": []}}
            },
        },
        {
            "id": "hidden",
            "metadata": {
                "document_metadata": {"document_access": {"policy": "restricted", "user_ids": ["u2"], "group_ids": []}}
            },
        },
        {
            "id": "kb-visible",
            "metadata": {"document_metadata": {"document_access": {"policy": "kb"}}},
        },
    ]

    visible = visible_research_evidence(records)

    assert [row["id"] for row in visible] == ["visible", "hidden", "kb-visible"]


def test_visible_research_evidence_does_not_read_legacy_acl_metadata() -> None:
    records = [
        {
            "id": "kb-a-visible",
            "knowledge_base_id": "kb-a",
            "metadata": {
                "document_metadata": {"document_access": {"policy": "restricted", "user_ids": ["u1"], "group_ids": []}}
            },
        },
        {
            "id": "kb-b-hidden",
            "knowledge_base_id": "kb-b",
            "metadata": {
                "document_metadata": {"document_access": {"policy": "restricted", "user_ids": ["u2"], "group_ids": []}}
            },
        },
    ]

    visible = visible_research_evidence(records)

    assert [row["id"] for row in visible] == ["kb-a-visible", "kb-b-hidden"]


def test_public_report_uses_only_passed_visible_evidence_and_reports_gaps() -> None:
    run = {"topic": "topic", "status": "completed", "stop_reason": "all_questions_processed"}
    report = build_public_research_report(
        run,
        questions=[{"id": "q1", "question": "Question?"}],
        coverage=[{"question_id": "q1", "status": "missing", "reason": "no evidence"}],
        evidence=[
            {
                "evidence_ref": "E1",
                "title": "Visible",
                "content_abstract": "Visible abstract.",
                "source_url": "https://example.test/visible",
            }
        ],
        claims=[{"claim_text": "Visible claim", "support_status": "partial", "evidence_ids": ["E1"]}],
        reflections=[{"body": "Operational note only."}],
    )

    assert report["coverage"] == {"covered": 0, "total": 1}
    assert report["status"] == "completed"
    assert "Status: completed" in report["markdown"]
    assert "Visible abstract." in report["markdown"]
    assert "Question?: missing (no evidence)" in report["markdown"]
    assert report["latest_reflection"] == "Operational note only."
    assert run["status"] == "completed"


def test_public_report_exposes_stable_evidence_refs_in_claims() -> None:
    evidence_id = uuid.UUID("4f61b3d9-c8fa-4cec-a519-83f4f04cb682")
    report = build_public_research_report(
        {"topic": "topic", "status": "completed"},
        questions=[],
        coverage=[],
        evidence=[{"id": evidence_id, "title": "Visible", "content_abstract": "Abstract"}],
        claims=[
            {
                "id": uuid.uuid4(),
                "question_id": uuid.uuid4(),
                "claim_text": "Claim",
                "support_status": "supported",
                "evidence_ids": [evidence_id],
            }
        ],
        reflections=[],
    )

    expected_ref = "E-4f61b3d9c8fa4ceca51983f4f04cb682"
    assert report["claims"][0]["evidence_ids"] == [expected_ref]
    assert report["claims"][0]["evidence_refs"] == [expected_ref]
    assert expected_ref in report["markdown"]
    assert str(evidence_id) not in report["markdown"]
    json.dumps(report)


def test_public_report_separates_unsupported_and_conflicting_claims_from_confident_findings() -> None:
    report = build_public_research_report(
        {"topic": "topic", "status": "completed"},
        questions=[],
        coverage=[],
        evidence=[],
        claims=[
            {"claim_text": "Supported claim", "support_status": "supported", "evidence_ids": ["e1"]},
            {"claim_text": "Blocked claim", "support_status": "unsupported", "evidence_ids": []},
            {"claim_text": "Conflicting claim", "support_status": "conflicting", "evidence_ids": ["e2"]},
        ],
        reflections=[],
    )

    assert "Supported claim [supported" in report["markdown"]
    assert "## Blocked Or Conflicting Claims" in report["markdown"]
    assert "Blocked claim [unsupported; not used as a confident finding]" in report["markdown"]
    assert "Conflicting claim [conflicting; not used as a confident finding]" in report["markdown"]


def test_public_report_marks_partial_terminal_and_failure_taxonomy() -> None:
    report = build_public_research_report(
        {
            "topic": "topic",
            "status": "completed",
            "stop_reason": "mode_insufficient_tools",
            "error_code": "planner_invalid_schema",
        },
        questions=[],
        coverage=[],
        evidence=[],
        claims=[],
        reflections=[],
    )

    assert report["partial_terminal"] is True
    assert report["completion_kind"] == "partial"
    assert report["failure_taxonomy"] == {
        "stop_reason": "mode_insufficient_tools",
        "error_code": "planner_invalid_schema",
    }
    assert "Stop reason: mode_insufficient_tools" in report["markdown"]
    assert "Error code: planner_invalid_schema" in report["markdown"]
    assert "Terminal mode: partial" in report["markdown"]


def test_claim_candidates_are_evidence_linked_and_contradiction_status_is_explicit() -> None:
    retrieval = RetrievalResult(
        query="Можно ли считать заявку VX-9 готовой к запуску?",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                knowledge_base_id="kb",
                title="Checklist CHK-18",
                section_path=[],
                content="CHK-18 has status security approved and performance approved.",
                source_url="https://example.test/checklist",
            ),
            Evidence(
                evidence_id="S2",
                chunk_id="c2",
                knowledge_base_id="kb",
                title="Prism Edge release note",
                section_path=[],
                content="Prism Edge blocked: data residency waiver missing.",
                source_url="https://example.test/blocker",
            ),
        ],
        events=[],
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.partial,
            confidence=0.5,
            reason="partial_context_coverage",
        ),
    )

    claims = _claims_from_evidence(retrieval.evidence)

    assert claims[0]["evidence_ids"] == ["S1"]
    assert claims[1]["evidence_ids"] == ["S2"]
    assert _coverage_status(retrieval, question_text=retrieval.query) == "conflicting"
    assert _claim_record_status("supported", "conflicting", str(claims[0]["text"])) == "conflicting"


def test_episode_derived_questions_skip_expansion_without_progress_but_keep_conflict_follow_up() -> None:
    retrieval = RetrievalResult(
        query="Можно ли выпускать VX-9?",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                knowledge_base_id="kb",
                title="Checklist CHK-18",
                section_path=[],
                content="Security approved.",
                source_url="https://example.test/checklist",
            )
        ],
        events=[],
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.partial,
            confidence=0.4,
            reason="coverage_gap",
        ),
    )

    no_progress_questions = _episode_derived_questions(
        topic="topic",
        current_question="Можно ли выпускать VX-9?",
        planner_questions=[],
        retrieval=retrieval,
        existing_questions=[],
        coverage_status="partial",
        allow_expansion=False,
    )
    conflict_questions = _episode_derived_questions(
        topic="topic",
        current_question="Можно ли выпускать VX-9?",
        planner_questions=[],
        retrieval=retrieval,
        existing_questions=[],
        coverage_status="conflicting",
        allow_expansion=False,
    )

    assert no_progress_questions == []
    assert len(conflict_questions) == 1
    assert "противоречат" in conflict_questions[0].question


@pytest.mark.asyncio
async def test_research_question_selection_does_not_reopen_terminal_coverage() -> None:
    conn = _RecordingConnection()

    await load_next_research_question(cast(Any, conn), tenant_id="tenant", research_run_id="run")

    assert "status IN ('open','running','partial','conflicting')" in conn.sql[0]


def test_select_next_question_prioritizes_required_then_bridge_with_stable_tie_break() -> None:
    selected = select_next_question(
        [
            {
                "id": "normal",
                "question": "normal",
                "kind": "derived",
                "status": "open",
                "execution_state": "pending",
                "acceptance": {"priority": "normal"},
                "metadata": {},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "bridge",
                "question": "bridge",
                "kind": "derived",
                "status": "open",
                "execution_state": "pending",
                "acceptance": {"priority": "bridge"},
                "metadata": {},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "required-b",
                "question": "required b",
                "kind": "primary",
                "status": "open",
                "execution_state": "pending",
                "acceptance": {"priority": "required"},
                "metadata": {},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "required-a",
                "question": "required a",
                "kind": "decomposition",
                "status": "open",
                "execution_state": "pending",
                "acceptance": {"priority": "required"},
                "metadata": {},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        ]
    )

    assert selected is not None
    assert selected["id"] == "required-a"


def test_invalid_planner_schema_fallback_keeps_immutable_question_and_no_discovery() -> None:
    question = "Which service owns runbook RB-17 for Night Harbor?"

    fallback = fallback_research_plan(question)

    assert fallback.tool_candidates == ["extended_search"]
    assert fallback.search_queries == [question]
    assert fallback.discovered_questions == []


def test_duplicate_evidence_fingerprint_is_not_progress_for_rb17_and_night_harbor() -> None:
    duplicate = research_evidence_fingerprint(
        knowledge_base_id="kb",
        document_id="doc",
        document_version_id="v1",
        chunk_id="chunk-rb17",
        source_url="https://example.test/runbook",
        title="RB-17",
        content_abstract="Night Harbor owner",
    )
    result = type(
        "Persisted",
        (),
        {
            "new_evidence_record_ids": [],
            "supported_claim_count": 1,
            "coverage_status": "partial",
            "duplicate_evidence_count": 2,
        },
    )()

    assert duplicate == research_evidence_fingerprint(
        knowledge_base_id="kb",
        document_id="doc",
        document_version_id="v1",
        chunk_id="chunk-rb17",
        source_url="https://example.test/runbook",
        title="RB-17",
        content_abstract="Night Harbor owner",
    )
    assert _episode_progress_made(result) is False


def test_alias_bridge_question_uses_generic_bridge_priority() -> None:
    assert (
        _derived_question_priority(
            {"rationale": "alias discovered in local evidence", "needed_evidence": ["downstream link"]}
        )
        == "bridge"
    )


@pytest.mark.asyncio
async def test_terminalization_closes_rb17_and_night_harbor_required_questions() -> None:
    conn = _ResearchQuestionConnection(
        [
            {
                "id": "rb-17",
                "question": "Which service owns RB-17?",
                "ordinal": 1,
                "kind": "primary",
                "status": "open",
                "acceptance": {"priority": "required"},
                "metadata": {},
            },
            {
                "id": "night-harbor",
                "question": "What is Night Harbor?",
                "ordinal": 2,
                "kind": "decomposition",
                "status": "partial",
                "acceptance": {"priority": "required"},
                "metadata": {},
            },
        ]
    )

    await terminalize_research_questions(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        reason="run_deadline_exhausted",
    )

    updates = [params for sql, params in zip(conn.sql, conn.params, strict=True) if "UPDATE research_questions" in sql]
    assert len(updates) == 2
    assert all(cast(dict[str, Any], params)["execution_state"] == "done" for params in updates)
    assert all(cast(dict[str, Any], params)["outcome"] == "exhausted" for params in updates)


@pytest.mark.asyncio
async def test_duplicate_only_attempt_terminalizes_useful_question_as_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    transitions: list[dict[str, Any]] = []

    async def record_transition(*args: Any, **kwargs: Any) -> None:
        transitions.append(kwargs)

    monkeypatch.setattr(deep_research, "transition_research_question", record_transition)

    await _terminalize_question_after_tick(
        cast(Any, _RecordingConnection()),
        tenant_id="tenant",
        research_run_id="run",
        question={
            "id": "question",
            "execution_state": "running",
            "attempt_count": 1,
            "budget": {"max_attempts": 3},
        },
        records={"evidence": [{"question_id": "question"}], "claims": []},
        outcome=EpisodeExecutionOutcome(
            progress_made=False,
            duplicate_evidence_count=1,
        ),
        budget={"max_attempts": 3},
    )

    assert transitions == [
        {
            "tenant_id": "tenant",
            "research_run_id": "run",
            "question_id": "question",
            "execution_state": "done",
            "outcome": "partial",
            "reason": "duplicate_evidence_no_progress",
        }
    ]


@pytest.mark.asyncio
async def test_duplicate_question_reuses_terminal_partial_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    transitions: list[dict[str, Any]] = []

    async def record_transition(*args: Any, **kwargs: Any) -> None:
        transitions.append(kwargs)

    monkeypatch.setattr(deep_research, "transition_research_question", record_transition)

    reused = await _reuse_terminal_duplicate_question(
        cast(Any, _RecordingConnection()),
        tenant_id="tenant",
        research_run_id="run",
        question={"id": "duplicate", "question": "Who owns RB-17?", "execution_state": "pending"},
        existing_questions=[
            {
                "id": "source",
                "question": "Who owns RB-17?",
                "execution_state": "done",
                "outcome": "partial",
            }
        ],
    )

    assert reused is True
    assert transitions[0]["question_id"] == "duplicate"
    assert transitions[0]["outcome"] == "partial"
    assert transitions[0]["reason"] == "duplicate_question_reuses_terminal_evidence"


def test_deadline_report_has_non_null_partial_synthesis_and_limitations() -> None:
    run = {"topic": "Night Harbor", "status": "completed", "stop_reason": "run_deadline_exhausted"}
    questions = [
        {
            "id": "q1",
            "question": "What is Night Harbor?",
            "execution_state": "done",
            "outcome": "exhausted",
        }
    ]
    coverage = [{"question_id": "q1", "status": "missing", "reason": "deadline"}]
    synthesis = _deterministic_synthesis(run, questions=questions, coverage=coverage, evidence=[], claims=[])
    report = build_public_research_report(
        {**run, "final_report": {"synthesis": synthesis}},
        questions=questions,
        coverage=coverage,
        evidence=[],
        claims=[],
        reflections=[],
    )

    assert report["synthesis"] is not None
    assert "Confirmed findings" in report["synthesis"]["markdown"]
    assert "Unresolved questions" in report["synthesis"]["markdown"]
    assert "Limitations" in report["synthesis"]["markdown"]


def test_failed_tool_branch_is_classified_without_marking_tool_result_as_run_failure() -> None:
    branch = ToolResult(
        retrieval=None,
        error_class="transient",
        error_code="provider_timeout",
        error_message="research tool branch failed",
    )

    assert classify_tool_error(TimeoutError()) == "transient"
    assert branch.succeeded is False
    assert branch.error_class == "transient"


def test_deadline_remaining_is_bounded_and_expires_before_controller_tick() -> None:
    run = {"created_at": datetime.now(UTC) - timedelta(seconds=10)}

    remaining = _run_deadline_remaining(run, 60)

    assert remaining is not None
    assert 45 < remaining <= 50
    assert _run_deadline_expired({"created_at": datetime.now(UTC) - timedelta(seconds=61)}, 60)


def test_retrieval_rewrite_count_is_read_from_safe_event() -> None:
    retrieval = RetrievalResult(
        query="question",
        trace_id="trace",
        evidence=[],
        events=[{"stage": "research_rewrite", "rewrite_count": 2}],
    )

    assert _retrieval_rewrite_count(retrieval) == 2


@pytest.mark.asyncio
async def test_create_research_episode_marks_question_running() -> None:
    conn = _RecordingConnection()

    await create_research_episode(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        episode_index=1,
        question_id="question",
        query_run_id="query",
        context_summary={"token_estimate": 10},
    )

    assert any("UPDATE research_questions" in sql and "status = 'running'" in sql for sql in conn.sql)
    assert any("attempt_count = attempt_count + 1" in sql for sql in conn.sql)


@pytest.mark.asyncio
async def test_research_persistence_conflict_targets_are_explicit_and_stable() -> None:
    conn = _RecordingConnection()

    await create_query_run(
        cast(Any, conn),
        tenant_id="tenant",
        knowledge_base_id="kb",
        user_id="user",
        request_id=str(uuid.uuid4()),
        client_request_id=None,
        mode="deep_research",
        input_text="question",
        trace_id="trace",
    )
    await create_research_episode(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        episode_index=1,
        question_id="question",
        query_run_id="query",
        context_summary={},
    )
    await create_research_tool_call(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        episode_id="episode",
        question_id="question",
        query_run_id="query",
        tool_name="extended_search",
        tool_query_hash="query-hash",
        safe_metadata={"tool_name": "extended_search"},
    )

    assert any("ON CONFLICT (request_id) DO NOTHING" in sql for sql in conn.sql)
    episode_sql = next(sql for sql in conn.sql if "INSERT INTO research_episodes" in sql)
    assert "ON CONFLICT (research_run_id, episode_index) DO NOTHING" in episode_sql
    assert "xmax" not in episode_sql


@pytest.mark.asyncio
async def test_episode_stage_transition_uses_compare_and_set() -> None:
    conn = _RecordingConnection()

    await update_research_episode(
        cast(Any, conn),
        episode_id="episode",
        status="running",
        stage="tool_registered",
        expected_stage="claimed",
    )

    sql = next(sql for sql in conn.sql if "UPDATE research_episodes" in sql)
    assert ":expected_stage" in sql
    assert "stage = CAST(:expected_stage AS text)" in sql


@pytest.mark.asyncio
async def test_evidence_upsert_checks_identity_before_mutating_conflict() -> None:
    conn = _RecordingConnection()

    await upsert_research_evidence_record(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        question_id="question",
        chunk_id="chunk",
        document_id="document",
        document_version_id=None,
        knowledge_base_id="kb",
        evidence_ref="E1",
        title="title",
        source_url="source",
        section_path=[],
        content_abstract="abstract",
        support_status="supports",
        score=1.0,
        metadata={},
    )

    assert any("ON CONFLICT (research_run_id, chunk_id) DO NOTHING" in sql for sql in conn.sql)
    assert any("SELECT id, question_id, document_id" in sql for sql in conn.sql)


@pytest.mark.asyncio
async def test_completed_tool_call_cannot_be_downgraded_by_stale_worker() -> None:
    conn = _RecordingConnection()

    await update_research_tool_call(
        cast(Any, conn),
        tool_call_id="tool-call",
        status="failed",
        error_code="stale_worker",
    )

    sql = next(sql for sql in conn.sql if "UPDATE research_tool_calls" in sql)
    assert "status <> 'completed' OR :status = 'completed'" in sql


@pytest.mark.asyncio
async def test_resumable_episode_query_is_scoped_and_non_terminal() -> None:
    conn = _RecordingConnection()

    result = await load_resumable_research_episode(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
    )

    assert result is None
    sql = next(sql for sql in conn.sql if "FROM research_episodes" in sql)
    assert "status NOT IN ('completed','failed','cancelled')" in sql
    assert "stage NOT IN ('completed','failed')" in sql


@pytest.mark.asyncio
async def test_query_run_id_is_stable_for_replay_and_episode_attempt_is_not_a_key() -> None:
    conn = _RecordingConnection()
    kwargs: dict[str, Any] = {
        "tenant_id": "tenant",
        "knowledge_base_id": "kb",
        "user_id": "user",
        "request_id": str(uuid.uuid4()),
        "client_request_id": None,
        "mode": "deep_research",
        "input_text": "question",
        "trace_id": "trace",
    }

    first = await create_query_run(cast(Any, conn), **kwargs)
    second = await create_query_run(cast(Any, conn), **kwargs)

    assert first == second


@pytest.mark.asyncio
async def test_update_research_run_does_not_assign_error_columns_twice() -> None:
    conn = _RecordingConnection()

    await update_research_run(
        cast(Any, conn),
        research_run_id="run",
        error_code="controller_bug",
        clear_error=True,
    )

    sql = next(sql for sql in conn.sql if "UPDATE research_runs" in sql)
    assert sql.count("error_code =") == 1
    assert "error_code = :error_code" in sql
    assert "error_code = NULL" not in sql


@pytest.mark.asyncio
async def test_research_heartbeat_casts_nullable_lease_uuid() -> None:
    conn = _RecordingConnection()

    await touch_research_heartbeat(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        lease_id=None,
    )

    sql = next(sql for sql in conn.sql if "UPDATE research_runs" in sql)
    assert sql.count("CAST(:lease_id AS text)") >= 3


@pytest.mark.asyncio
async def test_update_job_completed_with_safe_error_uses_each_error_column_once() -> None:
    conn = _RecordingConnection()

    await update_job(
        cast(Any, conn),
        "job",
        status=JobStatus.completed,
        progress={"stage": "completed_partial", "safe_error_code": "run_deadline_exhausted"},
        error_code="run_deadline_exhausted",
        error_message="deep research completed with partial findings",
    )

    sql = conn.sql[-1]
    assert sql.count("error_code =") == 1
    assert sql.count("error_message =") == 1
    assert "error_code = :error_code" in sql
    assert "error_message = :error_message" in sql


@pytest.mark.asyncio
async def test_create_research_run_inserts_job_before_run_fk_reference() -> None:
    conn = _RecordingConnection()

    await create_research_run(
        cast(Any, conn),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        user_id="22222222-2222-4222-8222-222222222222",
        topic="topic",
        retrieval_profile="upload_mock",
        tool_mode=DEFAULT_RESEARCH_TOOL_MODE,
        retrieval_overrides={},
        context_policy={},
        questions=["Question?"],
        research_plan_id="77777777-7777-4777-8777-777777777777",
    )

    job_insert_index = next(index for index, sql in enumerate(conn.sql) if "INSERT INTO ingestion_jobs" in sql)
    run_insert_index = next(index for index, sql in enumerate(conn.sql) if "INSERT INTO research_runs" in sql)
    assert job_insert_index < run_insert_index
    serialized_params = json.dumps(conn.params, ensure_ascii=False, sort_keys=True)
    assert "research_plan_id" in serialized_params


@pytest.mark.asyncio
async def test_create_research_run_persists_scope_and_resume_job_reuses_scope() -> None:
    conn = _RecordingConnection()

    await create_research_run(
        cast(Any, conn),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        knowledge_base_ids=[
            "33333333-3333-4333-8333-333333333333",
            "44444444-4444-4444-8444-444444444444",
        ],
        user_id="22222222-2222-4222-8222-222222222222",
        topic="topic",
        retrieval_profile="upload_mock",
        tool_mode=DEFAULT_RESEARCH_TOOL_MODE,
        retrieval_overrides={},
        context_policy={},
        questions=["Question?"],
    )
    await create_research_resume_job(
        cast(Any, conn),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        research_run_id="55555555-5555-4555-8555-555555555555",
        knowledge_base_ids=[
            "33333333-3333-4333-8333-333333333333",
            "44444444-4444-4444-8444-444444444444",
        ],
        tool_mode=DEFAULT_RESEARCH_TOOL_MODE,
    )

    scope_inserts = [
        params for sql, params in zip(conn.sql, conn.params, strict=True) if "INSERT INTO research_run_scopes" in sql
    ]
    resume_job_insert = next(
        params
        for sql, params in zip(conn.sql, conn.params, strict=True)
        if "INSERT INTO ingestion_jobs" in sql and "resume_received" in str(params)
    )

    assert len(scope_inserts) == 2
    resume_config = json.loads(cast(dict[str, Any], resume_job_insert)["config"])
    assert resume_config["knowledge_base_ids"] == [
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    ]
    assert resume_config["tool_mode"] == DEFAULT_RESEARCH_TOOL_MODE


def test_research_run_create_schema_rejects_duplicate_scope_ids() -> None:
    with pytest.raises(ValueError, match="unique non-empty ids"):
        ResearchRunCreate.model_validate(
            {
                "topic": "topic",
                "knowledge_base_ids": ["kb-a", "kb-a"],
            }
        )


def test_research_run_create_schema_defaults_and_validates_tool_mode() -> None:
    payload = ResearchRunCreate.model_validate({"topic": "topic"})

    assert payload.tool_mode == DEFAULT_RESEARCH_TOOL_MODE
    with pytest.raises(ValueError):
        ResearchRunCreate.model_validate({"topic": "topic", "tool_mode": "web_search"})


def test_research_plan_create_schema_rejects_duplicate_questions() -> None:
    with pytest.raises(ValueError, match="questions must contain unique non-empty items"):
        ResearchPlanCreate.model_validate(
            {
                "topic": "topic",
                "questions": [
                    {"question": "Что известно про LTN-42?", "ordinal": 1, "kind": "primary"},
                    {"question": "Что известно про LTN-42", "ordinal": 2, "kind": "derived"},
                ],
            }
        )


def test_research_plan_patch_schema_accepts_ordered_question_edits() -> None:
    payload = ResearchPlanPatch.model_validate(
        {
            "topic": "updated topic",
            "questions": [
                {"question": "Primary question?", "ordinal": 1, "kind": "primary"},
                {"question": "Follow-up question?", "ordinal": 2, "kind": "derived"},
            ],
        }
    )

    assert payload.questions == [
        ResearchPlanQuestion(question="Primary question?", ordinal=1, kind="primary"),
        ResearchPlanQuestion(question="Follow-up question?", ordinal=2, kind="derived"),
    ]


def test_bounded_rewrite_queries_preserve_original_and_add_bounded_variants() -> None:
    rewrites = research_tools._bounded_rewrite_queries(
        "Какой сервис и владелец связаны с runbook RB-17 для Project Lantern?",
        max_rewrites=2,
    )

    assert 1 <= len(rewrites) <= 2
    assert "rb-17" in rewrites[0].casefold()
    assert all("project lantern" != item.casefold() for item in rewrites)


@pytest.mark.asyncio
async def test_append_research_questions_persists_derived_lineage_and_suppresses_duplicates() -> None:
    conn = _ResearchQuestionConnection(
        [
            {
                "id": "existing",
                "question": "Что известно про LTN-42?",
                "ordinal": 1,
                "kind": "primary",
                "status": "open",
                "acceptance": {},
                "metadata": {},
            }
        ]
    )

    inserted = await append_research_questions(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        questions=[
            ResearchDerivedQuestion(question="Что известно про LTN-42?", rationale="duplicate"),
            ResearchDerivedQuestion(
                question="Какой сервис и владелец связаны с runbook RB-17?",
                rationale="runbook bridge",
                needed_evidence=["service owner"],
            ),
        ],
        source_episode_id="episode",
        source_evidence_ids=["evidence-1"],
    )

    insert_params = [
        params for sql, params in zip(conn.sql, conn.params, strict=True) if "INSERT INTO research_questions" in sql
    ]
    assert len(inserted) == 1
    assert len(insert_params) == 1
    serialized_params = json.dumps(insert_params[0], ensure_ascii=False, sort_keys=True)
    assert any("'derived', 'open'" in sql for sql in conn.sql)
    assert "runbook RB-17" in serialized_params
    assert "source_episode_id" in serialized_params
    assert "evidence-1" in serialized_params


@pytest.mark.asyncio
async def test_research_tool_call_ledger_stores_safe_hash_metadata_only() -> None:
    conn = _RecordingConnection()
    metadata = research_tool_call_metadata(
        tool_name="extended_search",
        tool_query="Project Lantern RB-17",
        planner={"next_action": "call_tool", "derived_questions": [], "needed_evidence": []},
        context={"token_estimate": 1200, "over_soft_limit": False, "over_hard_input_limit": False, "trimming": []},
    )

    call_id = await create_research_tool_call(
        cast(Any, conn),
        tenant_id="tenant",
        research_run_id="run",
        episode_id="episode",
        question_id="question",
        query_run_id="query",
        tool_name="extended_search",
        tool_query_hash=str(metadata["tool_query_hash"]),
        safe_metadata=metadata,
    )
    await update_research_tool_call(
        cast(Any, conn),
        tool_call_id=str(call_id),
        status="completed",
        result_summary={"evidence_count": 2, "evidence_record_ids": ["e1", "e2"], "stop_reason": "evidence_sufficient"},
    )

    serialized_params = json.dumps(conn.params, ensure_ascii=False, sort_keys=True)
    assert "Project Lantern RB-17" not in serialized_params
    assert str(metadata["tool_query_hash"]) in serialized_params
    assert "evidence_sufficient" in serialized_params
    with pytest.raises(ValueError):
        assert_safe_tool_metadata({"provider": {"raw_provider_payload": "SECRET"}})


@pytest.mark.asyncio
async def test_research_tool_dispatches_multi_kb_extended_search_to_retrieve_multi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_retrieve_multi(*_args: Any, **kwargs: Any) -> RetrievalResult:
        captured.update(kwargs)
        return RetrievalResult(
            query="Project Lantern",
            trace_id="trace",
            evidence=[],
            events=[],
            answerability=AnswerabilityDecision(
                status=AnswerabilityStatus.unanswerable,
                confidence=0.1,
                reason="empty",
            ),
        )

    monkeypatch.setattr(research_tools, "retrieve_multi", fake_retrieve_multi)

    await research_tools.execute_research_tool(
        cast(Any, _RecordingConnection()),
        request=ToolRequest(tool_name="extended_search", query="Project Lantern"),
        tenant_id="tenant",
        knowledge_base_id="kb-a",
        knowledge_base_ids=["kb-a", "kb-b"],
        query_run_id="query",
        trace_id="trace",
        settings=cast(Any, object()),
        profile=get_retrieval_profile("test_mock"),
        profile_overrides={},
        search_filters={"document_access_scopes": {"kb-a": object(), "kb-b": object()}},
    )

    assert captured["knowledge_base_ids"] == ["kb-a", "kb-b"]


@pytest.mark.asyncio
async def test_pause_research_run_marks_preclaim_run_paused_and_job_cancelled() -> None:
    conn = _RecordingConnection()

    await request_research_pause(cast(Any, conn), tenant_id="tenant", research_run_id="run")

    assert any("UPDATE research_runs" in sql and "THEN 'paused'" in sql for sql in conn.sql)
    assert any(
        "UPDATE ingestion_jobs" in sql
        and "THEN 'cancelled'" in sql
        and "paused_before_start" in sql
        and "completed_at = CASE WHEN status = 'received'" in sql
        for sql in conn.sql
    )


@pytest.mark.asyncio
async def test_cancel_research_run_marks_preclaim_or_paused_run_cancelled() -> None:
    conn = _RecordingConnection()

    await request_research_cancel(cast(Any, conn), tenant_id="tenant", research_run_id="run")

    assert any("UPDATE research_runs" in sql and "THEN 'cancelled'" in sql for sql in conn.sql)
    assert any(
        "WHERE tenant_id = :tenant_id AND id = :id AND status IN ('received','running','paused')" in sql
        for sql in conn.sql
    )
    assert any(
        "UPDATE ingestion_jobs" in sql
        and "cancelled_before_start" in sql
        and "completed_at = CASE WHEN status = 'received'" in sql
        for sql in conn.sql
    )
