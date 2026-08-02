from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from wikipediarag.eval.schemas import EvalTask, EvalTaskResult, RetrievalTaskResult, RetrievalTaskScores, TaskScores

type RootCause = Literal[
    "passed",
    "retrieval_error",
    "hallucination_or_unsupported",
    "reasoning_error",
    "hard_negative_attribution",
    "unanswerable_false_positive",
    "execution_error",
    "not_evaluated",
]

DIAGNOSIS_SCHEMA_VERSION = "eval_root_cause_v1"
REASONING_TOKEN_F1_THRESHOLD = 0.5
ROOT_CAUSES: tuple[RootCause, ...] = (
    "passed",
    "retrieval_error",
    "hallucination_or_unsupported",
    "reasoning_error",
    "hard_negative_attribution",
    "unanswerable_false_positive",
    "execution_error",
    "not_evaluated",
)
_ROOT_CAUSE_SET = set(ROOT_CAUSES)


def diagnose_answer_task(task: EvalTask | None, *, status: str, scores: TaskScores | None) -> dict[str, Any]:
    signals = _answer_signals(task, status=status, scores=scores)
    if task is None:
        return _diagnosis("not_evaluated", ["missing_task_metadata"], signals)
    if status == "failed":
        return _diagnosis("execution_error", ["task_status_failed"], signals)
    if scores is None:
        return _diagnosis("not_evaluated", ["missing_scores"], signals)

    if task.unanswerable:
        if (
            scores.cited_hard_negative_rate > 0.0
            or scores.unsupported_claim_rate > 0.0
            or scores.citation_precision < 1.0
        ):
            return _diagnosis(
                "hallucination_or_unsupported",
                ["unanswerable_answer_uses_unsupported_evidence"],
                signals,
            )
        if scores.unanswerable_accuracy < 1.0:
            return _diagnosis("unanswerable_false_positive", ["unanswerable_answered_as_fact"], signals)
        return _diagnosis("passed", [], signals)

    if _gold_missing(scores):
        return _diagnosis("retrieval_error", ["gold_evidence_missing_in_required_window"], signals)
    if scores.cited_hard_negative_rate > 0.0:
        return _diagnosis("hard_negative_attribution", ["cited_hard_negative_evidence"], signals)
    if scores.citation_precision < 1.0 or scores.unsupported_claim_rate > 0.0:
        return _diagnosis("hallucination_or_unsupported", ["answer_not_fully_supported_by_citations"], signals)
    if scores.exact_match < 1.0 and scores.token_f1 < REASONING_TOKEN_F1_THRESHOLD:
        return _diagnosis("reasoning_error", ["gold_found_but_correctness_proxy_low"], signals)
    return _diagnosis("passed", [], signals)


def diagnose_retrieval_task(
    task: EvalTask | None, *, status: str, scores: RetrievalTaskScores | None
) -> dict[str, Any]:
    signals = _retrieval_signals(task, status=status, scores=scores)
    if task is None:
        return _diagnosis("not_evaluated", ["missing_task_metadata"], signals)
    if status == "failed":
        return _diagnosis("execution_error", ["task_status_failed"], signals)
    if scores is None:
        return _diagnosis("not_evaluated", ["missing_scores"], signals)

    if task.unanswerable:
        if scores.dangerous_false_positive_evidence_rate > 0.0:
            return _diagnosis(
                "hallucination_or_unsupported",
                ["unanswerable_retrieved_dangerous_false_positive_evidence"],
                signals,
            )
        if scores.retrieved_gold_leak_rate > 0.0 or scores.false_positive_evidence_rate > 0.0:
            return _diagnosis("retrieval_error", ["unanswerable_retrieved_false_positive_evidence"], signals)
        return _diagnosis("passed", [], signals)

    if _retrieval_hard_negative_hit(scores):
        return _diagnosis("hard_negative_attribution", ["hard_negative_ranked_above_gold_or_dangerous"], signals)
    if _retrieval_gold_missing(scores):
        return _diagnosis("retrieval_error", ["gold_evidence_missing_in_required_window"], signals)
    return _diagnosis("passed", [], signals)


def answer_result_diagnosis(task: EvalTask | None, result: EvalTaskResult | None) -> dict[str, Any]:
    if result is None:
        return diagnose_answer_task(task, status="missing_result", scores=None)
    if result.diagnosis:
        return result.diagnosis
    return diagnose_answer_task(task, status=result.status, scores=result.scores)


def retrieval_result_diagnosis(task: EvalTask | None, result: RetrievalTaskResult | None) -> dict[str, Any]:
    if result is None:
        return diagnose_retrieval_task(task, status="missing_result", scores=None)
    if result.diagnosis:
        return result.diagnosis
    return diagnose_retrieval_task(task, status=result.status, scores=result.scores)


def root_cause_count_metrics(diagnoses: Iterable[dict[str, Any]]) -> dict[str, float]:
    metrics = {f"root_cause_{cause}_count": 0.0 for cause in ROOT_CAUSES}
    for diagnosis in diagnoses:
        cause = _root_cause_from_payload(diagnosis)
        metrics[f"root_cause_{cause}_count"] += 1.0
    return metrics


def _diagnosis(root_cause: RootCause, reasons: list[str], signals: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "root_cause": root_cause,
        "passed": root_cause == "passed",
        "reasons": reasons,
        "signals": signals,
        "policy": {
            "reasoning_token_f1_threshold": REASONING_TOKEN_F1_THRESHOLD,
        },
    }


def _answer_signals(task: EvalTask | None, *, status: str, scores: TaskScores | None) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "status": status,
        "task_family": str(task.task_family) if task else "",
        "unanswerable": bool(task.unanswerable) if task else False,
        "gold_chunk_count": len(task.gold_chunk_ids) if task else 0,
        "hard_negative_page_count": len(task.hard_negative_page_ids) if task else 0,
    }
    if scores is None:
        return signals
    signals.update(
        {
            "page_recall_at_10": scores.page_recall.get("10", 0.0),
            "chunk_recall_at_20": scores.chunk_recall.get("20", 0.0),
            "citation_precision": scores.citation_precision,
            "unsupported_claim_rate": scores.unsupported_claim_rate,
            "cited_hard_negative_rate": scores.cited_hard_negative_rate,
            "unanswerable_accuracy": scores.unanswerable_accuracy,
            "exact_match": scores.exact_match,
            "token_f1": scores.token_f1,
        }
    )
    return signals


def _retrieval_signals(task: EvalTask | None, *, status: str, scores: RetrievalTaskScores | None) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "status": status,
        "task_family": str(task.task_family) if task else "",
        "unanswerable": bool(task.unanswerable) if task else False,
        "gold_chunk_count": len(task.gold_chunk_ids) if task else 0,
        "hard_negative_page_count": len(task.hard_negative_page_ids) if task else 0,
    }
    if scores is None:
        return signals
    signals.update(
        {
            "page_recall_at_10": scores.page_recall.get("10", 0.0),
            "chunk_recall_at_20": scores.chunk_recall.get("20", 0.0),
            "retrieved_gold_leak_rate": scores.retrieved_gold_leak_rate,
            "false_positive_evidence_rate": scores.false_positive_evidence_rate,
            "dangerous_false_positive_evidence_rate": scores.dangerous_false_positive_evidence_rate,
            "hard_negative_page_hit_at_10": scores.hard_negative_page_hit_at_10,
            "hard_negative_page_hit_at_20": scores.hard_negative_page_hit_at_20,
            "gold_vs_hard_negative_rank_margin": scores.gold_vs_hard_negative_rank_margin,
        }
    )
    return signals


def _gold_missing(scores: TaskScores) -> bool:
    return scores.page_recall.get("10", 0.0) < 1.0 or scores.chunk_recall.get("20", 0.0) < 1.0


def _retrieval_gold_missing(scores: RetrievalTaskScores) -> bool:
    return scores.page_recall.get("10", 0.0) < 1.0 or scores.chunk_recall.get("20", 0.0) < 1.0


def _retrieval_hard_negative_hit(scores: RetrievalTaskScores) -> bool:
    if scores.dangerous_false_positive_evidence_rate > 0.0:
        return True
    if scores.hard_negative_page_hit_at_10 <= 0.0:
        return False
    return scores.gold_vs_hard_negative_rank_margin is None or scores.gold_vs_hard_negative_rank_margin < 0.0


def _root_cause_from_payload(diagnosis: dict[str, Any]) -> RootCause:
    cause = str(diagnosis.get("root_cause") or "not_evaluated")
    if cause in _ROOT_CAUSE_SET:
        return cause
    return "not_evaluated"
