from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from wikipediarag.eval.schemas import CandidateRef, EvalTask, RetrievalTaskScores, TaskScores

TOKEN_RE = re.compile(r"[\wА-Яа-яЁё]+", re.UNICODE)
CITATION_RE = re.compile(r"\[(S\d+)\]")
NO_ANSWER_MARKERS = ("недостаточно", "нет доказательств", "не найден", "insufficient", "cannot answer")


def normalize_answer(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.casefold()))


def exact_match(answer: str, accepted_answers: list[str]) -> float:
    normalized = normalize_answer(answer)
    return float(any(normalized == normalize_answer(item) for item in accepted_answers if item))


def token_f1(answer: str, accepted_answers: list[str]) -> float:
    answer_tokens = normalize_answer(answer).split()
    if not answer_tokens:
        return 0.0
    best = 0.0
    for accepted in accepted_answers:
        gold_tokens = normalize_answer(accepted).split()
        if not gold_tokens:
            continue
        common = Counter(answer_tokens) & Counter(gold_tokens)
        overlap = sum(common.values())
        if overlap == 0:
            continue
        precision = overlap / len(answer_tokens)
        recall = overlap / len(gold_tokens)
        best = max(best, 2 * precision * recall / (precision + recall))
    return best


def is_no_answer(answer: str) -> bool:
    normalized = answer.casefold()
    return any(marker in normalized for marker in NO_ANSWER_MARKERS)


def recall_at(candidates: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return float(bool(set(candidates[:k]) & gold))


def mrr_at(candidates: list[str], gold: set[str], k: int = 10) -> float:
    if not gold:
        return 0.0
    for index, item in enumerate(candidates[:k], start=1):
        if item in gold:
            return 1.0 / index
    return 0.0


def ndcg_at(candidates: list[str], gold: set[str], k: int = 10) -> float:
    if not gold:
        return 0.0
    dcg = 0.0
    for index, item in enumerate(candidates[:k], start=1):
        if item in gold:
            dcg += 1.0 / math.log2(index + 1)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def full_hop_recall(candidates: list[str], task: EvalTask, k: int = 20) -> float:
    by_hop: dict[int, set[str]] = {}
    for evidence in task.gold_evidence:
        by_hop.setdefault(evidence.hop, set()).add(evidence.chunk_id)
    if not by_hop:
        return 0.0
    found = 0
    top = set(candidates[:k])
    for chunks in by_hop.values():
        if top & chunks:
            found += 1
    return found / len(by_hop)


def path_completion(candidates: list[str], task: EvalTask, k: int = 20) -> float:
    if not task.gold_evidence:
        return 0.0
    return float(full_hop_recall(candidates, task, k) == 1.0)


def rank_delta(before: list[str], after: list[str], gold: set[str], k: int = 20) -> float | None:
    before_rank = _first_rank(before, gold, k)
    after_rank = _first_rank(after, gold, k)
    if before_rank is None and after_rank is None:
        return None
    missing = k + 1
    return float((before_rank or missing) - (after_rank or missing))


def citation_scores(
    cited_chunk_ids: list[str],
    gold_chunk_ids: set[str],
    *,
    unanswerable: bool,
) -> tuple[float, float, float]:
    if unanswerable:
        return (1.0 if not cited_chunk_ids else 0.0, 1.0, float(bool(cited_chunk_ids)))
    if not cited_chunk_ids:
        return 0.0, 0.0, 1.0
    cited = set(cited_chunk_ids)
    supported = cited & gold_chunk_ids
    precision = len(supported) / len(cited)
    recall = len(supported) / len(gold_chunk_ids) if gold_chunk_ids else 0.0
    unsupported = 1.0 - precision
    return precision, recall, unsupported


def score_task(
    task: EvalTask,
    *,
    answer: str,
    reranked: list[CandidateRef],
    prefusion: list[CandidateRef],
    cited_chunk_ids: list[str],
    kiwix_url_ok: bool,
) -> TaskScores:
    pages = [item.document_id for item in reranked]
    sections = [item.section_id for item in reranked]
    chunks = [item.chunk_id for item in reranked]
    prefusion_chunks = [item.chunk_id for item in prefusion]
    gold_pages = set(task.gold_page_ids)
    gold_sections = set(task.gold_section_ids)
    gold_chunks = set(task.gold_chunk_ids)
    citation_precision, citation_recall, unsupported = citation_scores(
        cited_chunk_ids,
        gold_chunks,
        unanswerable=task.unanswerable,
    )
    return TaskScores(
        page_recall={str(k): recall_at(pages, gold_pages, k) for k in (1, 5, 10, 20)},
        section_recall={str(k): recall_at(sections, gold_sections, k) for k in (5, 10, 20)},
        chunk_recall={str(k): recall_at(chunks, gold_chunks, k) for k in (5, 10, 20)},
        mrr_at_10=mrr_at(chunks, gold_chunks, 10),
        ndcg_at_10=ndcg_at(chunks, gold_chunks, 10),
        full_hop_recall=full_hop_recall(chunks, task, 20),
        path_completion=path_completion(chunks, task, 20),
        reranker_gold_delta=rank_delta(prefusion_chunks, chunks, gold_chunks, 20),
        exact_match=0.0 if task.unanswerable else exact_match(answer, [task.reference_answer, *task.accepted_answers]),
        token_f1=0.0 if task.unanswerable else token_f1(answer, [task.reference_answer, *task.accepted_answers]),
        unanswerable_accuracy=float(is_no_answer(answer)) if task.unanswerable else 0.0,
        citation_precision=citation_precision,
        citation_recall=citation_recall,
        unsupported_claim_rate=unsupported,
        kiwix_url_ok=float(kiwix_url_ok),
    )


def score_retrieval_task(
    task: EvalTask,
    *,
    final: list[CandidateRef],
    reranked: list[CandidateRef],
    prefusion: list[CandidateRef],
) -> RetrievalTaskScores:
    pages = [item.document_id for item in final]
    sections = [item.section_id for item in final]
    chunks = [item.chunk_id for item in final]
    reranked_chunks = [item.chunk_id for item in reranked]
    prefusion_chunks = [item.chunk_id for item in prefusion]
    gold_pages = set(task.gold_page_ids)
    gold_sections = set(task.gold_section_ids)
    gold_chunks = set(task.gold_chunk_ids)
    hard_negative_pages = set(task.hard_negative_page_ids)
    return RetrievalTaskScores(
        page_recall={str(k): recall_at(pages, gold_pages, k) for k in (1, 5, 10, 20)},
        section_recall={str(k): recall_at(sections, gold_sections, k) for k in (5, 10, 20)},
        chunk_recall={str(k): recall_at(chunks, gold_chunks, k) for k in (5, 10, 20)},
        mrr_at_10=mrr_at(chunks, gold_chunks, 10),
        ndcg_at_10=ndcg_at(chunks, gold_chunks, 10),
        full_hop_recall=full_hop_recall(chunks, task, 20),
        path_completion=path_completion(chunks, task, 20),
        reranker_gold_delta=rank_delta(prefusion_chunks, reranked_chunks, gold_chunks, 20),
        retrieved_gold_leak_rate=_gold_leak_rate(chunks, gold_chunks, task.unanswerable),
        false_positive_evidence_rate=_false_positive_rate(pages, hard_negative_pages, 20),
        hard_negative_page_hit_at_10=recall_at(pages, hard_negative_pages, 10),
        hard_negative_page_hit_at_20=recall_at(pages, hard_negative_pages, 20),
        gold_vs_hard_negative_rank_margin=_rank_margin(pages, gold_pages, hard_negative_pages, 20),
    )


def aggregate(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((pct / 100) * len(ordered)) - 1))
    return ordered[index]


def _first_rank(candidates: list[str], gold: set[str], k: int) -> int | None:
    for index, item in enumerate(candidates[:k], start=1):
        if item in gold:
            return index
    return None


def _gold_leak_rate(candidates: list[str], gold: set[str], unanswerable: bool) -> float:
    if not unanswerable:
        return 0.0
    if not gold:
        return 0.0
    return float(bool(set(candidates[:20]) & gold))


def _false_positive_rate(candidates: list[str], negative_pages: set[str], k: int) -> float:
    if not negative_pages:
        return 0.0
    top = candidates[:k]
    if not top:
        return 0.0
    return sum(1 for item in top if item in negative_pages) / len(top)


def _rank_margin(
    candidates: list[str],
    gold_pages: set[str],
    hard_negative_pages: set[str],
    k: int,
) -> float | None:
    gold_rank = _first_rank(candidates, gold_pages, k)
    hard_negative_rank = _first_rank(candidates, hard_negative_pages, k)
    if gold_rank is None or hard_negative_rank is None:
        return None
    return float(hard_negative_rank - gold_rank)
