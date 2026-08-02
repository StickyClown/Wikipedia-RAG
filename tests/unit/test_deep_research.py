from __future__ import annotations

import json
from typing import Any, cast

import pytest

from wikipediarag.auth import KnowledgeBaseRole
from wikipediarag.deep_research import (
    ResearchContextBudget,
    build_public_research_report,
    build_research_questions,
    context_budget_for_profile,
    context_policy_for_profile,
    pack_research_context,
    visible_research_evidence,
)
from wikipediarag.document_access import DocumentAccessScope
from wikipediarag.repository import (
    create_research_episode,
    create_research_run,
    load_next_research_question,
    request_research_cancel,
    request_research_pause,
)
from wikipediarag.retrieval_profile import get_retrieval_profile


class _EmptyResult:
    def mappings(self) -> _EmptyResult:
        return self

    def first(self) -> None:
        return None


class _RecordingConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def execute(self, statement: object, _params: object | None = None) -> _EmptyResult:
        self.sql.append(str(statement))
        return _EmptyResult()


def test_context_budget_uses_declared_model_context_percentages() -> None:
    profile = get_retrieval_profile(
        "test_mock",
        overrides={"postprocess": {"max_context_tokens": 80_000}},
    )

    budget = context_budget_for_profile(profile)
    policy = context_policy_for_profile(profile)

    assert budget.max_context_tokens == 80_000
    assert budget.productive_target_tokens == 36_000
    assert budget.soft_limit_tokens == 44_000
    assert budget.hard_input_limit_tokens == 56_000
    assert budget.output_reserve_tokens == 12_000
    assert budget.safety_reserve_tokens == 12_000
    assert policy["ratios"] == {
        "productive_target": 0.45,
        "soft_limit": 0.55,
        "hard_input_limit": 0.70,
        "output_reserve": 0.15,
        "safety_reserve": 0.15,
    }


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


def test_visible_research_evidence_applies_current_document_acl() -> None:
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

    visible = visible_research_evidence(
        records,
        DocumentAccessScope(user_id="u1", kb_role=KnowledgeBaseRole.viewer),
    )

    assert [row["id"] for row in visible] == ["visible", "kb-visible"]


def test_public_report_uses_only_passed_visible_evidence_and_reports_gaps() -> None:
    report = build_public_research_report(
        {"topic": "topic", "status": "completed", "stop_reason": "all_questions_processed"},
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
    assert "Visible abstract." in report["markdown"]
    assert "Question?: missing (no evidence)" in report["markdown"]
    assert report["latest_reflection"] == "Operational note only."


@pytest.mark.asyncio
async def test_research_question_selection_does_not_reopen_terminal_coverage() -> None:
    conn = _RecordingConnection()

    await load_next_research_question(cast(Any, conn), tenant_id="tenant", research_run_id="run")

    assert "status IN ('open','running')" in conn.sql[0]
    assert "partial" not in conn.sql[0]
    assert "missing" not in conn.sql[0]
    assert "conflicting" not in conn.sql[0]


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
        retrieval_overrides={},
        context_policy={},
        questions=["Question?"],
    )

    job_insert_index = next(index for index, sql in enumerate(conn.sql) if "INSERT INTO ingestion_jobs" in sql)
    run_insert_index = next(index for index, sql in enumerate(conn.sql) if "INSERT INTO research_runs" in sql)
    assert job_insert_index < run_insert_index


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
