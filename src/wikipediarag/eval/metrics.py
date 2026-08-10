from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from wikipediarag.eval.schemas import CandidateRef, EvalTask, RetrievalTaskScores, TaskScores

TOKEN_RE = re.compile(r"[\wА-Яа-яЁё]+", re.UNICODE)
CITATION_RE = re.compile(r"\[(S\d+)\]")
NO_ANSWER_MARKERS = (
    "недостаточно",
    "нет доказательств",
    "не найден",
    "не найдена",
    "не найдено",
    "отсутствует информация",
    "информация отсутствует",
    "отсутствуют сведения",
    "нет сведений",
    "нет информации",
    "не указано",
    "не содержит информации",
    "не удалось определить",
    "невозможно определить",
    "cannot answer",
    "cannot determine",
    "insufficient",
    "not specified",
    "no information",
    "does not contain information",
)


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


def rouge_l(answer: str, accepted_answers: list[str]) -> float:
    """Token-level ROUGE-L F1, intentionally dependency-free for eval runs."""
    answer_tokens = normalize_answer(answer).split()
    if not answer_tokens:
        return 0.0
    best = 0.0
    for accepted in accepted_answers:
        gold_tokens = normalize_answer(accepted).split()
        if not gold_tokens:
            continue
        previous = [0] * (len(gold_tokens) + 1)
        for answer_token in answer_tokens:
            current = [0]
            for index, gold_token in enumerate(gold_tokens, start=1):
                if answer_token == gold_token:
                    current.append(previous[index - 1] + 1)
                else:
                    current.append(max(previous[index], current[-1]))
            previous = current
        lcs = previous[-1]
        precision = lcs / len(answer_tokens)
        recall = lcs / len(gold_tokens)
        if precision + recall:
            best = max(best, 2 * precision * recall / (precision + recall))
    return best


def is_no_answer(answer: str) -> bool:
    normalized = answer.casefold()
    if any(marker in normalized for marker in NO_ANSWER_MARKERS):
        return True
    absence_patterns = (
        r"информац\w*.{0,160}отсутств",
        r"сведен\w*.{0,160}отсутств",
        r"источник\w*.{0,160}не\s+содерж",
        r"source\w*.{0,160}(?:does not contain|do not contain)",
    )
    return any(re.search(pattern, normalized, flags=re.S) for pattern in absence_patterns)


def recall_at(candidates: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return len(set(candidates[:k]) & gold) / len(gold)


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
    no_answer: bool = False,
    allow_context_citations: bool = False,
) -> tuple[float, float, float]:
    if unanswerable:
        if no_answer:
            return 1.0, 1.0, 0.0
        return (1.0 if not cited_chunk_ids else 0.0, 1.0, float(bool(cited_chunk_ids)))
    if not cited_chunk_ids:
        return 0.0, 0.0, 1.0
    cited = set(cited_chunk_ids)
    supported = cited & gold_chunk_ids
    if allow_context_citations and gold_chunk_ids and supported >= gold_chunk_ids:
        return 1.0, 1.0, 0.0
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
    cited_document_ids: list[str] | None = None,
    kiwix_url_ok: bool,
) -> TaskScores:
    pages = [item.document_id for item in reranked]
    sections = [item.section_id for item in reranked]
    chunks = [item.chunk_id for item in reranked]
    prefusion_chunks = [item.chunk_id for item in prefusion]
    gold_documents = set(task.gold_document_ids or task.gold_page_ids)
    gold_pages = set(task.gold_page_ids or gold_documents)
    gold_sections = set(task.gold_section_ids)
    gold_chunks = set(task.gold_chunk_ids)
    documents = _unique_values(item.document_id for item in reranked if item.document_id)
    prefusion_documents = _unique_values(item.document_id for item in prefusion if item.document_id)
    document_mode = task.evaluation_granularity == "document" or bool(task.gold_document_ids)
    cited_documents = list(cited_document_ids or [])
    cited_document_set = set(cited_documents)
    no_answer = is_no_answer(answer)
    if task.unanswerable:
        document_citation_precision = float(not cited_document_set or cited_document_set <= gold_documents)
        document_citation_recall = float(no_answer)
        gold_document_citation_hit = float(no_answer and bool(cited_document_set & gold_documents))
    else:
        supported_documents = cited_document_set & gold_documents
        document_citation_precision = len(supported_documents) / len(cited_document_set) if cited_document_set else 0.0
        document_citation_recall = len(supported_documents) / len(gold_documents) if gold_documents else 0.0
        gold_document_citation_hit = float(bool(supported_documents))
    cited_hard_negative = (
        0.0
        if task.unanswerable and no_answer
        else _cited_hard_negative_rate(
            task,
            cited_chunk_ids=cited_chunk_ids,
            candidates=[*reranked, *prefusion],
        )
    )
    citation_precision, citation_recall, unsupported = citation_scores(
        cited_chunk_ids,
        gold_chunks,
        unanswerable=task.unanswerable,
        no_answer=no_answer,
        allow_context_citations=cited_hard_negative == 0.0,
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
        unanswerable_accuracy=float(no_answer) if task.unanswerable else 0.0,
        soft_unanswerable_context_rate=float(task.unanswerable and no_answer and bool(cited_chunk_ids)),
        citation_precision=citation_precision,
        citation_recall=citation_recall,
        unsupported_claim_rate=unsupported,
        cited_hard_negative_rate=cited_hard_negative,
        kiwix_url_ok=float(kiwix_url_ok),
        document_recall={str(k): recall_at(documents, gold_documents, k) for k in (1, 5, 10, 20)},
        document_mrr_at_10=mrr_at(documents, gold_documents, 10),
        document_ndcg_at_10=ndcg_at(documents, gold_documents, 10),
        document_reranker_gold_delta=rank_delta(prefusion_documents, documents, gold_documents, 10),
        document_citation_precision=document_citation_precision if document_mode else 0.0,
        document_citation_recall=document_citation_recall if document_mode else 0.0,
        gold_document_citation_hit=gold_document_citation_hit if document_mode else 0.0,
        rouge_l=0.0 if task.unanswerable else rouge_l(answer, [task.reference_answer, *task.accepted_answers]),
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
    gold_documents = set(task.gold_document_ids or gold_pages)
    gold_sections = set(task.gold_section_ids)
    gold_chunks = set(task.gold_chunk_ids)
    hard_negative_pages = set(task.hard_negative_page_ids)
    document_final = _unique_values(pages)
    document_reranked = _unique_values(item.document_id for item in reranked if item.document_id)
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
        dangerous_false_positive_evidence_rate=_dangerous_false_positive_rate(
            task,
            final=final,
            gold_pages=gold_pages,
            hard_negative_pages=hard_negative_pages,
            k=20,
        ),
        hard_negative_page_hit_at_10=recall_at(pages, hard_negative_pages, 10),
        hard_negative_page_hit_at_20=recall_at(pages, hard_negative_pages, 20),
        gold_vs_hard_negative_rank_margin=_rank_margin(pages, gold_pages, hard_negative_pages, 20),
        document_recall={str(k): recall_at(document_final, gold_documents, k) for k in (1, 5, 10, 20)},
        document_mrr_at_10=mrr_at(document_final, gold_documents, 10),
        document_ndcg_at_10=ndcg_at(document_final, gold_documents, 10),
        document_reranker_gold_delta=rank_delta(document_reranked, document_final, gold_documents, 10),
    )


def aggregate(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _unique_values(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


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


def _cited_hard_negative_rate(
    task: EvalTask,
    *,
    cited_chunk_ids: list[str],
    candidates: list[CandidateRef],
) -> float:
    hard_negative_pages = set(task.hard_negative_page_ids)
    if not hard_negative_pages or not cited_chunk_ids:
        return 0.0
    page_by_chunk = {candidate.chunk_id: candidate.document_id for candidate in candidates if candidate.chunk_id}
    return float(any(page_by_chunk.get(chunk_id) in hard_negative_pages for chunk_id in set(cited_chunk_ids)))


def _dangerous_false_positive_rate(
    task: EvalTask,
    *,
    final: list[CandidateRef],
    gold_pages: set[str],
    hard_negative_pages: set[str],
    k: int,
) -> float:
    if task.unanswerable or not hard_negative_pages:
        return 0.0
    top = final[:k]
    if not top:
        return 0.0
    pages = [item.document_id for item in top]
    gold_rank = _first_rank(pages, gold_pages, k)
    best_gold_score = _best_candidate_score(item for item in top if item.document_id in gold_pages)
    dangerous = 0
    for rank, candidate in enumerate(top, start=1):
        if candidate.document_id not in hard_negative_pages:
            continue
        score = _candidate_score(candidate)
        rank_risk = rank <= 3 or (gold_rank is not None and rank < gold_rank)
        score_risk = score is not None and (
            score >= 0.5 or (best_gold_score is not None and score >= best_gold_score * 0.6)
        )
        if rank_risk or score_risk:
            dangerous += 1
    return dangerous / len(top)


def _best_candidate_score(candidates: Iterable[CandidateRef]) -> float | None:
    scores = [score for candidate in candidates if (score := _candidate_score(candidate)) is not None]
    return max(scores) if scores else None


def _candidate_score(candidate: CandidateRef) -> float | None:
    for key in ("rerank", "relevance_score", "rrf_total", "dense", "bm25"):
        value = candidate.scores.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None


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
