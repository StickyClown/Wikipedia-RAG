from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from wikipediarag.embedding import normalize_for_embedding
from wikipediarag.retrieval_profile import RetrievalProfile
from wikipediarag.schemas import AnswerabilityDecision, AnswerabilityStatus, Evidence

GATE_VERSION = "answerability_gate_v4"

_VALUE_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{3,4}\b|\b\d+(?:[,.]\d+)?\b")
_STOPWORDS = {
    "а",
    "в",
    "во",
    "где",
    "год",
    "для",
    "и",
    "или",
    "как",
    "какая",
    "каком",
    "какого",
    "какие",
    "какой",
    "какое",
    "какую",
    "когда",
    "которого",
    "которой",
    "которое",
    "который",
    "которые",
    "кто",
    "на",
    "о",
    "об",
    "от",
    "по",
    "почему",
    "при",
    "произошел",
    "произошла",
    "произошли",
    "произошло",
    "с",
    "со",
    "сравни",
    "сравнение",
    "такое",
    "у",
    "чем",
    "что",
    "это",
    "the",
    "what",
    "when",
    "where",
    "who",
    "why",
    "дай",
    "информация",
    "информацию",
    "кратко",
    "локальном",
    "локальный",
    "опиши",
    "подробно",
    "расскажи",
    "сведения",
    "году",
    "выпуска",
    "каждого",
    "конкретные",
    "релиз",
    "релиза",
    "релизы",
    "сравните",
    "тип",
    "типу",
    "укажите",
    "формат",
    "формата",
    "значение",
    "значения",
    "альбом",
    "альбома",
}
_FACT_REQUIREMENT_MARKERS = (
    "год",
    "дата",
    "дате",
    "когда",
    "номер",
    "серийн",
    "сколько",
    "официальн",
    "snapshot",
    "снапшот",
    "указан",
    "указано",
    "население",
    "площад",
    "столиц",
)
_PART_MARKERS = (" и ", " vs ", " versus ", ";")
_CONFLICT_MARKERS = (
    "конфликт",
    "противореч",
    "расход",
    "разные данные",
    "какой источник верен",
    "which source is correct",
    "conflict",
    "contradict",
)


def decide_answerability(
    query: str,
    evidence: Sequence[Evidence],
    profile: RetrievalProfile,
) -> AnswerabilityDecision:
    required_parts = _required_parts(query)
    covered_parts = [part for part in required_parts if _part_is_covered(part, evidence)]
    missing_parts = [part for part in required_parts if part not in covered_parts]
    key_values = _extract_values(query)
    key_names = _extract_names(query)
    all_text = _evidence_text(evidence)
    covered_values = [value for value in key_values if value.casefold() in all_text]
    covered_names = [name for name in key_names if normalize_for_embedding(name) in all_text]
    missing_names = [name for name in key_names if name not in covered_names]
    exact_title_match = _has_exact_or_redirect_title_match(query, evidence)
    answer_terms, covered_answer_terms, missing_answer_terms = _answer_bearing_terms(query, evidence)
    top_score = _top_relevance_score(evidence)
    page_diversity = _page_diversity(evidence)
    conflicting = _has_explicit_conflict(query, evidence)
    required_count = len(required_parts)
    coverage_ratio = len(covered_parts) / max(required_count, 1)
    score_threshold = 0.65
    values_ok = len(covered_values) == len(key_values)
    names_ok = len(covered_names) == len(key_names)
    strong_match = exact_title_match or top_score >= score_threshold
    enough_diversity = page_diversity >= min(required_count, 2)

    signals: dict[str, Any] = {
        "exact_title_match": exact_title_match,
        "top_relevance_score": top_score,
        "top_score_threshold": score_threshold,
        "page_diversity": page_diversity,
        "required_part_count": required_count,
        "covered_part_count": len(covered_parts),
        "coverage_ratio": round(coverage_ratio, 3),
        "query_values": key_values,
        "covered_values": covered_values,
        "query_names": key_names,
        "covered_names": covered_names,
        "missing_names": missing_names,
        "answer_bearing_terms": sorted(answer_terms),
        "covered_answer_bearing_terms": sorted(covered_answer_terms),
        "missing_answer_bearing_terms": sorted(missing_answer_terms),
        "final_evidence_min": profile.postprocess.final_evidence_min,
        "evidence_count": len(evidence),
    }

    if conflicting:
        signals["conflict_marker"] = True
        return AnswerabilityDecision(
            version=GATE_VERSION,
            status=AnswerabilityStatus.conflicting,
            confidence=0.7,
            reason="explicit_conflict_with_divergent_values",
            required_parts=required_parts,
            covered_parts=covered_parts,
            missing_parts=missing_parts,
            signals=signals,
        )
    if not evidence:
        return AnswerabilityDecision(
            version=GATE_VERSION,
            status=AnswerabilityStatus.unanswerable,
            confidence=0.95,
            reason="no_evidence",
            required_parts=required_parts,
            covered_parts=[],
            missing_parts=required_parts,
            signals=signals,
        )
    if _has_missing_answer_bearing_requirement(query, answer_terms, covered_answer_terms, missing_answer_terms):
        return AnswerabilityDecision(
            version=GATE_VERSION,
            status=AnswerabilityStatus.partial,
            confidence=0.55,
            reason="answer_bearing_terms_missing_partial",
            required_parts=required_parts,
            covered_parts=covered_parts,
            missing_parts=missing_parts or required_parts,
            signals=signals,
        )
    if strong_match and coverage_ratio >= 1.0 and values_ok and names_ok and enough_diversity:
        return AnswerabilityDecision(
            version=GATE_VERSION,
            status=AnswerabilityStatus.answerable,
            confidence=0.9 if exact_title_match else 0.8,
            reason="required_parts_covered",
            required_parts=required_parts,
            covered_parts=covered_parts,
            missing_parts=missing_parts,
            signals=signals,
        )
    if covered_parts or exact_title_match or top_score >= score_threshold:
        return AnswerabilityDecision(
            version=GATE_VERSION,
            status=AnswerabilityStatus.partial,
            confidence=max(0.35, min(0.75, coverage_ratio)),
            reason="partial_context_coverage",
            required_parts=required_parts,
            covered_parts=covered_parts,
            missing_parts=missing_parts or missing_names,
            signals=signals,
        )
    return AnswerabilityDecision(
        version=GATE_VERSION,
        status=AnswerabilityStatus.unanswerable,
        confidence=0.85,
        reason="no_required_parts_covered",
        required_parts=required_parts,
        covered_parts=covered_parts,
        missing_parts=missing_parts,
        signals=signals,
    )


def is_insufficient(decision: AnswerabilityDecision) -> bool:
    return decision.status in {
        AnswerabilityStatus.partial,
        AnswerabilityStatus.unanswerable,
        AnswerabilityStatus.conflicting,
    }


def should_try_extended_search(decision: AnswerabilityDecision) -> bool:
    return decision.status in {AnswerabilityStatus.partial, AnswerabilityStatus.unanswerable}


def _required_parts(query: str) -> list[str]:
    compact = " ".join(query.split())
    split_pattern = r"\?|;|\bvs\b|\bversus\b"
    parts = [part.strip(" ?!.") for part in re.split(split_pattern, compact, flags=re.I)]
    meaningful = [part for part in parts if _significant_terms(part)]
    if len(meaningful) <= 1:
        return [compact]
    if any(marker in compact.casefold() for marker in _PART_MARKERS) or should_decompose_query(compact):
        return meaningful[:6]
    return [compact]


def should_decompose_query(query: str) -> bool:
    normalized = query.casefold()
    return any(
        marker in normalized
        for marker in (
            "сравни",
            "сравнение",
            "отличается",
            "между",
            "что общего",
            "compare",
            "difference",
        )
    )


def _part_is_covered(part: str, evidence: Sequence[Evidence]) -> bool:
    terms = _significant_terms(part)
    if not terms:
        return False
    name_terms = {normalize_for_embedding(name) for name in _extract_names(part)}
    part_text = normalize_for_embedding(part)
    for item in evidence:
        title_text = normalize_for_embedding(_candidate_titles(item))
        body_text = normalize_for_embedding(f"{item.title} {' '.join(item.section_path)} {item.content}")
        if part_text and (part_text in title_text or title_text in part_text):
            return True
        body_terms = set(body_text.split())
        if name_terms and not _terms_overlap(name_terms, body_terms):
            continue
        if _terms_overlap(terms, body_terms):
            return True
    return False


def _has_exact_or_redirect_title_match(query: str, evidence: Sequence[Evidence]) -> bool:
    normalized_query = normalize_for_embedding(query)
    query_terms = set(normalized_query.split())
    for item in evidence:
        for title in _candidate_titles(item).split("\n"):
            normalized_title = normalize_for_embedding(title)
            title_terms = set(normalized_title.split())
            if not normalized_title or not title_terms:
                continue
            if (
                normalized_title in normalized_query
                or title_terms <= query_terms
                or _terms_overlap(title_terms, query_terms)
            ):
                return True
    return False


def _answer_bearing_terms(query: str, evidence: Sequence[Evidence]) -> tuple[set[str], set[str], set[str]]:
    query_terms = _significant_terms(query)
    title_terms = _matched_title_terms(query, evidence)
    answer_terms = {term for term in query_terms if not _terms_overlap({term}, title_terms)}
    evidence_terms = set(_evidence_text(evidence).split())
    covered = {term for term in answer_terms if _terms_overlap({term}, evidence_terms)}
    missing = answer_terms - covered
    return answer_terms, covered, missing


def _matched_title_terms(query: str, evidence: Sequence[Evidence]) -> set[str]:
    query_terms = set(normalize_for_embedding(query).split())
    matched: set[str] = set()
    for item in evidence:
        for title in _candidate_titles(item).split("\n"):
            title_terms = set(normalize_for_embedding(title).split())
            if title_terms and any(_terms_overlap({term}, query_terms) for term in title_terms):
                matched.update(title_terms)
    return matched


def _has_missing_answer_bearing_requirement(
    query: str,
    answer_terms: set[str],
    covered_terms: set[str],
    missing_terms: set[str],
) -> bool:
    if len(answer_terms) < 2 or len(missing_terms) < 2:
        return False
    normalized_query = normalize_for_embedding(query)
    if not any(marker in normalized_query for marker in _FACT_REQUIREMENT_MARKERS):
        return False
    coverage_ratio = len(covered_terms) / max(len(answer_terms), 1)
    return coverage_ratio < 0.5


def _candidate_titles(evidence: Evidence) -> str:
    metadata = evidence.metadata
    aliases: list[str] = [evidence.title, *evidence.section_path]
    for key in ("redirect_title", "redirect_source_title", "alias_title", "matched_title"):
        value = metadata.get(key)
        if isinstance(value, str):
            aliases.append(value)
    raw_aliases = metadata.get("aliases")
    if isinstance(raw_aliases, list):
        aliases.extend(str(value) for value in raw_aliases if isinstance(value, str))
    return "\n".join(aliases)


def _top_relevance_score(evidence: Sequence[Evidence]) -> float:
    if not evidence:
        return 0.0
    scores = evidence[0].scores
    for key in ("rerank", "rrf_total", "dense", "bm25"):
        value = scores.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def _page_diversity(evidence: Sequence[Evidence]) -> int:
    pages: set[str] = set()
    for item in evidence:
        page = item.metadata.get("zim_entry_path") or item.metadata.get("document_id") or item.title
        pages.add(str(page))
    return len(pages)


def _extract_values(text: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for match in _VALUE_RE.findall(text):
        normalized = match.casefold()
        if normalized not in seen:
            values.append(match)
            seen.add(normalized)
    return values[:8]


def _extract_names(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{2,}", text):
        token = raw.strip("-")
        normalized = normalize_for_embedding(token)
        if not normalized or normalized in _STOPWORDS or normalized in seen:
            continue
        if token[0].isupper():
            names.append(token)
            seen.add(normalized)
    return names[:10]


def _significant_terms(text: str) -> set[str]:
    return {
        token
        for token in normalize_for_embedding(text).split()
        if len(token) >= 3 and token not in _STOPWORDS and not token.isdigit()
    }


def _terms_overlap(left: set[str], right: set[str]) -> bool:
    if left & right:
        return True
    for left_term in left:
        for right_term in right:
            if len(left_term) >= 5 and len(right_term) >= 5 and left_term[:5] == right_term[:5]:
                return True
    return False


def _evidence_text(evidence: Sequence[Evidence]) -> str:
    return "\n".join(
        normalize_for_embedding(f"{item.title} {' '.join(item.section_path)} {item.content}") for item in evidence
    )


def _has_explicit_conflict(query: str, evidence: Sequence[Evidence]) -> bool:
    normalized_query = query.casefold()
    if not any(marker in normalized_query for marker in _CONFLICT_MARKERS):
        return False
    per_evidence_values = [
        set(_extract_values(f"{item.title} {' '.join(item.section_path)} {item.content}")) for item in evidence
    ]
    non_empty = [values for values in per_evidence_values if values]
    if len(non_empty) < 2:
        return False
    first = non_empty[0]
    return any(values.isdisjoint(first) for values in non_empty[1:])
