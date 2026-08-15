from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from wikipediarag.eval.quality import (
    QUALITY_SCHEMA_VERSION,
    QualitySuiteError,
    build_quality_report,
    macro_f1,
    normalize_stage_records,
    validate_quality_suite,
)
from wikipediarag.eval.schemas import (
    EvalTask,
    EvalTaskResult,
    EvaluationSchemaVersion,
    ExpectedClaim,
    GoldEvidence,
    ScopeReview,
    TaskScores,
)


def _task(*, task_id: str = "q1", family: str = "exact_identifier", outcome: str = "answered") -> EvalTask:
    evidence = GoldEvidence(
        evidence_id=f"e-{task_id}",
        document_id="doc-ru",
        section_id="section",
        chunk_id="chunk-ru",
        source_id="source-ru",
        quote="Контрольный факт 42",
        supports_claim_ids=[f"claim-{task_id}"],
    )
    return EvalTask(
        task_id=task_id,
        question="Какой контрольный факт указан?",
        task_family=family,  # type: ignore[arg-type]
        reference_answer="Контрольный факт 42",
        accepted_answers=["Контрольный факт 42"],
        unanswerable=outcome == "not_found_in_scope",
        expected_mode="unanswerable" if outcome == "not_found_in_scope" else "normal_sufficient",
        gold_page_ids=["doc-ru"],
        gold_section_ids=["section"],
        gold_chunk_ids=["chunk-ru"],
        gold_evidence=[evidence] if outcome != "not_found_in_scope" else [],
        reasoning_path=[],
        generator_alias="generator_main",
        verifier_alias="verifier",
        zim_checksum="",
        snapshot_id="",
        index_version="",
        retrieval_profile_hash="",
        evaluation_schema_version=cast(EvaluationSchemaVersion, QUALITY_SCHEMA_VERSION),
        language_group="ru",
        expected_outcome=outcome,  # type: ignore[arg-type]
        source_ids=["source-ru"],
        required_source_ids=["source-ru"] if outcome != "not_found_in_scope" else [],
        expected_claims=[]
        if outcome == "not_found_in_scope"
        else [
            ExpectedClaim(
                claim_id=f"claim-{task_id}",
                statement="Контрольный факт 42",
                accepted_answers=["Контрольный факт 42"],
                supports_evidence_ids=[f"e-{task_id}"],
            )
        ],
        scope_review=ScopeReview(
            reviewed=outcome in {"partial", "conflicting", "not_found_in_scope"},
            reviewed_by="test",
            reviewed_at="2026-08-15T00:00:00Z",
            source_ids=["source-ru"],
            checked_source_count=1,
        ),
        reviewed_by="test",
        reviewed_at="2026-08-15T00:00:00Z",
        split="dev",
    )


def _write_suite(tmp_path: Path, tasks: list[EvalTask]) -> None:
    source_path = tmp_path / "source-ru.md"
    source_path.write_text("Контрольный факт 42", encoding="utf-8")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    (tmp_path / "sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "source-ru",
                        "filename": "source-ru.md",
                        "language_group": "ru",
                        "source_kind": "controlled",
                        "sha256": digest,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "tasks.jsonl").write_text(
        "\n".join(json.dumps(task.model_dump(mode="json"), ensure_ascii=False) for task in tasks) + "\n",
        encoding="utf-8",
    )


def test_quality_validation_rejects_duplicate_task_ids(tmp_path: Path) -> None:
    _write_suite(tmp_path, [_task(), _task()])

    with pytest.raises(QualitySuiteError, match="TASK_ID_DUPLICATE"):
        validate_quality_suite(tmp_path, strict_counts=False)


def test_quality_prepare_allows_pending_review_but_freeze_validation_does_not(tmp_path: Path) -> None:
    pending = _task().model_copy(update={"reviewed_by": "", "reviewed_at": ""})
    _write_suite(tmp_path, [pending])

    suite = validate_quality_suite(tmp_path, strict_counts=False, require_reviewed=False)

    assert suite.tasks[0].reviewed_by == ""
    with pytest.raises(QualitySuiteError, match="REVIEW_MISSING"):
        validate_quality_suite(tmp_path, strict_counts=False, require_reviewed=True)


def test_quality_validation_checks_quote_and_scope_review(tmp_path: Path) -> None:
    task = _task(family="partial", outcome="partial")
    _write_suite(tmp_path, [task])
    suite = validate_quality_suite(tmp_path, strict_counts=False)

    assert suite.tasks[0].task_id == "q1"
    assert suite.source_hash
    assert suite.dataset_hash

    broken = task.model_copy(update={"gold_evidence": [task.gold_evidence[0].model_copy(update={"quote": "нет"})]})
    _write_suite(tmp_path, [broken])
    with pytest.raises(QualitySuiteError, match="EVIDENCE_QUOTE_NOT_FOUND"):
        validate_quality_suite(tmp_path, strict_counts=False)


def test_normalize_stage_records_keeps_only_safe_fields() -> None:
    records = normalize_stage_records(
        [
            {
                "stage": "rerank",
                "latency_ms": 12,
                "candidates": [{"chunk_id": "c1", "rank": 1, "score": 0.9, "text": "secret"}],
                "reason": "ok",
            }
        ],
        model_aliases={"rerank": "reranker"},
    )

    assert records[0].stage == "rerank"
    assert records[0].candidate_ids == ["c1"]
    assert records[0].candidate_ranks == [1]
    assert records[0].candidate_scores == [0.9]
    assert "secret" not in records[0].model_dump_json()


def test_macro_f1_and_quality_report() -> None:
    assert macro_f1(["answered", "partial"], ["answered", "answered"], ["answered", "partial"]) == pytest.approx(1 / 3)
    scores = TaskScores(
        page_recall={"1": 1.0, "5": 1.0, "10": 1.0, "20": 1.0},
        section_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        chunk_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        mrr_at_10=1.0,
        ndcg_at_10=1.0,
        full_hop_recall=1.0,
        path_completion=1.0,
        exact_match=1.0,
        token_f1=1.0,
        unanswerable_accuracy=0.0,
        citation_precision=1.0,
        citation_recall=1.0,
        unsupported_claim_rate=0.0,
        kiwix_url_ok=1.0,
    )
    result = EvalTaskResult(
        task_id="q1",
        config_id="test",
        config_hash="config",
        status="completed",
        question="q",
        usage={"answerability_status": "answerable"},
        scores=scores,
        latency_ms={"total": 10, "retrieval": 5},
    )

    report = build_quality_report([_task()], [result])

    assert report["completion_rate"] == 1.0
    assert report["answerability_macro_f1"] == 1.0
    assert report["metrics"]["answer_groundedness"] == 1.0
    assert report["by_group"]["family:exact_identifier"]["completed"] == 1.0


def test_quality_report_does_not_mix_comparison_keys() -> None:
    scores = TaskScores(
        page_recall={"1": 1.0, "5": 1.0, "10": 1.0, "20": 1.0},
        section_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        chunk_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        mrr_at_10=1.0,
        ndcg_at_10=1.0,
        full_hop_recall=1.0,
        path_completion=1.0,
        exact_match=1.0,
        token_f1=1.0,
        unanswerable_accuracy=0.0,
        citation_precision=1.0,
        citation_recall=1.0,
        unsupported_claim_rate=0.0,
        kiwix_url_ok=1.0,
    )
    result = EvalTaskResult(
        task_id="q1",
        config_id="test",
        config_hash="config",
        status="completed",
        question="q",
        usage={"answerability_status": "answerable"},
        scores=scores,
        comparison_key="one",
        latency_ms={"total": 10},
    )
    other = result.model_copy(update={"comparison_key": "two"})

    report = build_quality_report([_task()], [result, other])

    assert report["comparison_status"] == "incompatible_results"
    assert report["metrics"] == {}
    assert len(report["comparison_keys"]) == 2
