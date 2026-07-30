from __future__ import annotations

from wikipediarag.eval.commands import _diagnostic_task_payload
from wikipediarag.eval.diagnostics import (
    diagnose_answer_task,
    diagnose_retrieval_task,
    root_cause_count_metrics,
)
from wikipediarag.eval.schemas import (
    EvalConfig,
    EvalTask,
    EvalTaskResult,
    GoldEvidence,
    RetrievalTaskResult,
    RetrievalTaskScores,
    TaskFamily,
    TaskScores,
)


def _task(*, unanswerable: bool = False, hard_negative_page_ids: list[str] | None = None) -> EvalTask:
    task_family: TaskFamily = (
        "unanswerable" if unanswerable else ("hard_negative" if hard_negative_page_ids else "single_hop_factual")
    )
    return EvalTask(
        task_id="t1",
        question="Какой тестовый факт?",
        task_family=task_family,
        reference_answer="" if unanswerable else "Тестовый ответ",
        accepted_answers=[] if unanswerable else ["Тестовый ответ"],
        unanswerable=unanswerable,
        expected_mode="unanswerable" if unanswerable else "normal_sufficient",
        gold_page_ids=[] if unanswerable else ["p1"],
        gold_section_ids=[] if unanswerable else ["s1"],
        gold_chunk_ids=[] if unanswerable else ["c1"],
        gold_evidence=[]
        if unanswerable
        else [
            GoldEvidence(
                evidence_id="e1",
                document_id="p1",
                section_id="s1",
                chunk_id="c1",
                quote="Тестовый ответ",
            )
        ],
        reasoning_path=[] if unanswerable else ["p1"],
        generator_alias="generator_main",
        verifier_alias="verifier",
        zim_checksum="sha",
        snapshot_id="snapshot",
        index_version="index",
        retrieval_profile_hash="profile",
        hard_negative_page_ids=hard_negative_page_ids or [],
    )


def _answer_scores(
    *,
    page_recall_at_10: float = 1.0,
    chunk_recall_at_20: float = 1.0,
    exact_match: float = 1.0,
    token_f1: float = 1.0,
    unanswerable_accuracy: float = 1.0,
    citation_precision: float = 1.0,
    unsupported_claim_rate: float = 0.0,
    cited_hard_negative_rate: float = 0.0,
) -> TaskScores:
    return TaskScores(
        page_recall={"1": page_recall_at_10, "5": page_recall_at_10, "10": page_recall_at_10, "20": 1.0},
        section_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        chunk_recall={"5": chunk_recall_at_20, "10": chunk_recall_at_20, "20": chunk_recall_at_20},
        mrr_at_10=page_recall_at_10,
        ndcg_at_10=page_recall_at_10,
        full_hop_recall=chunk_recall_at_20,
        path_completion=chunk_recall_at_20,
        reranker_gold_delta=0.0,
        exact_match=exact_match,
        token_f1=token_f1,
        unanswerable_accuracy=unanswerable_accuracy,
        citation_precision=citation_precision,
        citation_recall=citation_precision,
        unsupported_claim_rate=unsupported_claim_rate,
        cited_hard_negative_rate=cited_hard_negative_rate,
        kiwix_url_ok=1.0,
    )


def _retrieval_scores(
    *,
    page_recall_at_10: float = 1.0,
    chunk_recall_at_20: float = 1.0,
    dangerous_false_positive_evidence_rate: float = 0.0,
    hard_negative_page_hit_at_10: float = 0.0,
    gold_vs_hard_negative_rank_margin: float | None = 1.0,
) -> RetrievalTaskScores:
    return RetrievalTaskScores(
        page_recall={"1": page_recall_at_10, "5": page_recall_at_10, "10": page_recall_at_10, "20": 1.0},
        section_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        chunk_recall={"5": chunk_recall_at_20, "10": chunk_recall_at_20, "20": chunk_recall_at_20},
        mrr_at_10=page_recall_at_10,
        ndcg_at_10=page_recall_at_10,
        full_hop_recall=chunk_recall_at_20,
        path_completion=chunk_recall_at_20,
        reranker_gold_delta=0.0,
        dangerous_false_positive_evidence_rate=dangerous_false_positive_evidence_rate,
        hard_negative_page_hit_at_10=hard_negative_page_hit_at_10,
        hard_negative_page_hit_at_20=hard_negative_page_hit_at_10,
        gold_vs_hard_negative_rank_margin=gold_vs_hard_negative_rank_margin,
    )


def test_missing_gold_evidence_is_retrieval_error() -> None:
    diagnosis = diagnose_answer_task(
        _task(),
        status="completed",
        scores=_answer_scores(page_recall_at_10=0.0, chunk_recall_at_20=0.0),
    )

    assert diagnosis["root_cause"] == "retrieval_error"
    assert "gold_evidence_missing_in_required_window" in diagnosis["reasons"]


def test_cited_hard_negative_is_hard_negative_attribution() -> None:
    diagnosis = diagnose_answer_task(
        _task(hard_negative_page_ids=["p2"]),
        status="completed",
        scores=_answer_scores(cited_hard_negative_rate=1.0),
    )

    assert diagnosis["root_cause"] == "hard_negative_attribution"


def test_unsupported_answer_is_hallucination_or_unsupported() -> None:
    diagnosis = diagnose_answer_task(
        _task(),
        status="completed",
        scores=_answer_scores(citation_precision=0.5, unsupported_claim_rate=0.5),
    )

    assert diagnosis["root_cause"] == "hallucination_or_unsupported"


def test_gold_found_grounded_answer_with_low_correctness_proxy_is_reasoning_error() -> None:
    diagnosis = diagnose_answer_task(
        _task(),
        status="completed",
        scores=_answer_scores(exact_match=0.0, token_f1=0.1),
    )

    assert diagnosis["root_cause"] == "reasoning_error"


def test_unanswerable_answered_as_fact_is_false_positive() -> None:
    diagnosis = diagnose_answer_task(
        _task(unanswerable=True),
        status="completed",
        scores=_answer_scores(unanswerable_accuracy=0.0),
    )

    assert diagnosis["root_cause"] == "unanswerable_false_positive"


def test_failed_result_is_execution_error() -> None:
    diagnosis = diagnose_answer_task(_task(), status="failed", scores=None)

    assert diagnosis["root_cause"] == "execution_error"
    assert diagnosis["passed"] is False


def test_retrieval_hard_negative_hit_is_diagnosed() -> None:
    diagnosis = diagnose_retrieval_task(
        _task(hard_negative_page_ids=["p2"]),
        status="completed",
        scores=_retrieval_scores(hard_negative_page_hit_at_10=1.0, gold_vs_hard_negative_rank_margin=-1.0),
    )

    assert diagnosis["root_cause"] == "hard_negative_attribution"


def test_root_cause_count_metrics_are_stable() -> None:
    metrics = root_cause_count_metrics(
        [
            {"root_cause": "passed"},
            {"root_cause": "retrieval_error"},
            {"root_cause": "unknown"},
        ]
    )

    assert metrics["root_cause_passed_count"] == 1.0
    assert metrics["root_cause_retrieval_error_count"] == 1.0
    assert metrics["root_cause_not_evaluated_count"] == 1.0
    assert metrics["root_cause_execution_error_count"] == 0.0


def test_diagnostic_task_payload_includes_answer_and_retrieval_diagnosis() -> None:
    task = _task()
    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={},
        config_hash="h",
    )
    answer = EvalTaskResult(
        task_id=task.task_id,
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="completed",
        question=task.question,
        answer="Другой ответ",
        scores=_answer_scores(exact_match=0.0, token_f1=0.1),
    )
    retrieval = RetrievalTaskResult(
        task_id=task.task_id,
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="completed",
        question=task.question,
        task_family=task.task_family,
        unanswerable=task.unanswerable,
        batch_index=1,
        task_index=1,
        scores=_retrieval_scores(),
    )

    payload = _diagnostic_task_payload(task, answer=answer, retrieval=retrieval)

    assert payload["diagnosis"]["root_cause"] == "reasoning_error"
    assert payload["answer"]["diagnosis"]["root_cause"] == "reasoning_error"
    assert payload["retrieval"]["diagnosis"]["root_cause"] == "passed"
