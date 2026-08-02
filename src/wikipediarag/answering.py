from __future__ import annotations

import json
import re
import time
from typing import Any

from wikipediarag.claim_verifier import claim_verification_blocks, verify_claims
from wikipediarag.config import Settings, get_settings
from wikipediarag.model_client import chat_completion
from wikipediarag.retrieval_profile import CitationValidationMode, RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import AnswerabilityStatus, Evidence, RetrievalResult
from wikipediarag.zim_dump import build_kiwix_source_url

CITATION_RE = re.compile(r"\[(S\d+)\]")

ANSWER_JSON_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "grounded_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer_markdown", "claims", "insufficient_evidence"],
            "properties": {
                "answer_markdown": {"type": "string"},
                "insufficient_evidence": {"type": "boolean"},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["claim_id", "text", "evidence_ids", "type"],
                        "properties": {
                            "claim_id": {"type": "string"},
                            "text": {"type": "string"},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "type": {"type": "string", "enum": ["fact", "inference"]},
                        },
                    },
                },
            },
        },
    },
}


async def generate_answer(
    question: str,
    retrieval: RetrievalResult,
    settings: Settings | None = None,
    profile: RetrievalProfile | None = None,
) -> tuple[str, dict[str, object]]:
    started = time.perf_counter()
    timings_ms: dict[str, int] = {
        "model_chat": 0,
        "answer_parse": 0,
        "citation_validation": 0,
        "claim_verification": 0,
    }
    resolved = settings or get_settings()
    active_profile = profile or get_retrieval_profile(settings=resolved)
    answerability_status = retrieval.answerability.status if retrieval.answerability else None
    if active_profile.answer.insufficient_evidence_mode and (
        answerability_status == AnswerabilityStatus.unanswerable
        or (retrieval.insufficient_evidence and not retrieval.evidence)
    ):
        answer = (
            "Недостаточно доказательств в локальной базе, чтобы надёжно ответить на вопрос. "
            "Попробуйте расширить импорт Wikipedia или уточнить запрос."
        )
        timings_ms["generation_total"] = _elapsed_ms(started)
        return answer, {
            "valid": True,
            "insufficient_evidence": True,
            "answerability_status": (AnswerabilityStatus.unanswerable.value if answerability_status else None),
            "citations": [],
            "claims": [],
            "usage": {},
            "timings_ms": timings_ms,
        }
    if active_profile.answer.insufficient_evidence_mode and answerability_status == AnswerabilityStatus.conflicting:
        answer = (
            "Найденные источники выглядят противоречиво, поэтому я не буду выбирать одну версию без дополнительной "
            "проверки. Уточните запрос или расширьте набор источников."
        )
        timings_ms["generation_total"] = _elapsed_ms(started)
        return answer, {
            "valid": True,
            "insufficient_evidence": True,
            "answerability_status": AnswerabilityStatus.conflicting.value,
            "citations": [],
            "claims": [],
            "usage": {},
            "timings_ms": timings_ms,
        }

    manifest = "\n\n".join(format_evidence(item) for item in retrieval.evidence)
    partial_instruction = ""
    if answerability_status == AnswerabilityStatus.partial:
        covered = ", ".join(retrieval.answerability.covered_parts) if retrieval.answerability else ""
        missing = ", ".join(retrieval.answerability.missing_parts) if retrieval.answerability else ""
        partial_instruction = (
            "Контекст покрывает вопрос частично. Ответь только по покрытым частям, явно укажи, "
            f"что покрыто: {covered or 'часть запроса'}, и что не покрыто: {missing or 'оставшаяся часть'}.\n"
        )
    prompt = (
        "Ответь на русском языке только по источникам ниже. "
        f"{partial_instruction}"
        "Верни JSON по схеме. Каждое фактическое утверждение должно иметь evidence_ids, "
        "а в answer_markdown citation ID вида [S1] должен стоять после утверждения.\n\n"
        f"Вопрос: {question}\n\nИсточники:\n{manifest}"
    )
    model_started = time.perf_counter()
    payload = await chat_completion(
        [
            {
                "role": "system",
                "content": "Ты локальный RAG генератор. Не придумывай факты вне evidence и не придумывай URL.",
            },
            {"role": "user", "content": prompt},
        ],
        resolved,
        alias=active_profile.model_aliases.generator_main,
        response_format=ANSWER_JSON_SCHEMA,
    )
    timings_ms["model_chat"] = _elapsed_ms(model_started)
    content = str(payload["choices"][0]["message"]["content"])
    usage = dict(payload.get("usage") or {})
    parse_started = time.perf_counter()
    draft = _parse_answer_draft(content, retrieval.evidence, strict=active_profile.requires_real_provider)
    timings_ms["answer_parse"] = _elapsed_ms(parse_started)
    answer = str(draft["answer_markdown"])
    validation_started = time.perf_counter()
    validation = validate_citations_with_policy(
        answer,
        retrieval.evidence,
        claims=list(draft.get("claims") or []),
        mode=active_profile.answer.verification.citation_validation,
        settings=resolved,
    )
    timings_ms["citation_validation"] = _elapsed_ms(validation_started)
    validation["usage"] = usage
    validation["model_alias"] = active_profile.model_aliases.generator_main
    validation["provider"] = payload.get("provider")
    validation["provider_request_id"] = payload.get("id")
    validation["model_gateway"] = dict(payload.get("_gateway_metadata") or {})
    validation["provider_cost"] = payload.get("cost") or usage.get("cost") or usage.get("total_cost")
    validation["answerability_status"] = answerability_status.value if answerability_status else None
    validation["insufficient_evidence"] = retrieval.insufficient_evidence
    if not validation["valid"]:
        if active_profile.requires_real_provider:
            raise ValueError(f"generated answer failed citation validation: {validation}")
        repaired = (
            "Найденные источники релевантны, но сгенерированный ответ не прошёл проверку ссылок. "
            f"Краткий подтверждённый фрагмент: {retrieval.evidence[0].content[:500]} [S1]"
        )
        validation_started = time.perf_counter()
        validation = validate_citations_with_policy(
            repaired,
            retrieval.evidence,
            claims=[],
            mode=active_profile.answer.verification.citation_validation,
            settings=resolved,
        )
        timings_ms["citation_validation"] += _elapsed_ms(validation_started)
        validation["usage"] = usage
        validation["model_gateway"] = dict(payload.get("_gateway_metadata") or {})
        validation["timings_ms"] = {**timings_ms, "generation_total": _elapsed_ms(started)}
        return repaired, validation
    claim_payload = await verify_claims(
        list(draft.get("claims") or []),
        retrieval.evidence,
        settings=resolved,
        profile=active_profile,
    )
    timings_ms["claim_verification"] = int(dict(claim_payload.get("timings_ms") or {}).get("claim_verification", 0))
    validation["claim_verification"] = claim_payload
    if claim_verification_blocks(claim_payload):
        answer = (
            "Ответ не прошёл claim-level проверку по supplied evidence, поэтому я не буду выдавать его как "
            "подтверждённый. Уточните запрос или расширьте набор источников."
        )
        validation["valid"] = True
        validation["insufficient_evidence"] = True
        validation["timings_ms"] = {**timings_ms, "generation_total": _elapsed_ms(started)}
        return answer, validation
    validation["timings_ms"] = {**timings_ms, "generation_total": _elapsed_ms(started)}
    return answer, validation


def format_evidence(evidence: Evidence) -> str:
    section = " / ".join(evidence.section_path)
    source_url = evidence.source_url
    return f"[{evidence.evidence_id}] {evidence.title} / {section}\nURL: {source_url}\n{evidence.content}"


def validate_citations(
    answer: str,
    evidence: list[Evidence],
    *,
    claims: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    resolved = settings or get_settings()
    allowed = {item.evidence_id for item in evidence}
    cited = CITATION_RE.findall(answer)
    unknown = sorted(set(cited) - allowed)
    claims_payload = claims or []
    unsupported_claims = [
        claim.get("claim_id", "")
        for claim in claims_payload
        if claim.get("type") == "fact" and not set(str(item) for item in claim.get("evidence_ids", [])) & allowed
    ]
    phantom_claim_citations = sorted(
        {str(item) for claim in claims_payload for item in claim.get("evidence_ids", []) if str(item) not in allowed}
    )
    source_url_errors = [
        item.evidence_id
        for item in evidence
        if item.metadata.get("source_type") == "wikipedia_zim" and not _zim_source_url_valid(item, resolved)
    ]
    valid = (
        bool(cited) and not unknown and not unsupported_claims and not phantom_claim_citations and not source_url_errors
    )
    return {
        "valid": valid,
        "citations": cited,
        "unknown": unknown,
        "allowed": sorted(allowed),
        "unsupported_claims": unsupported_claims,
        "phantom_claim_citations": phantom_claim_citations,
        "source_url_errors": source_url_errors,
        "claims": claims_payload,
        "insufficient_evidence": False,
    }


def validate_citations_with_policy(
    answer: str,
    evidence: list[Evidence],
    *,
    claims: list[dict[str, Any]] | None = None,
    mode: CitationValidationMode = "strict",
    settings: Settings | None = None,
) -> dict[str, object]:
    if mode == "off":
        return {
            "valid": True,
            "status": "disabled_by_policy",
            "policy": {"citation_validation": mode},
            "citations": CITATION_RE.findall(answer),
            "unknown": [],
            "allowed": sorted(item.evidence_id for item in evidence),
            "unsupported_claims": [],
            "phantom_claim_citations": [],
            "source_url_errors": [],
            "claims": claims or [],
            "insufficient_evidence": False,
        }
    validation = validate_citations(answer, evidence, claims=claims, settings=settings)
    validation["status"] = "completed"
    validation["policy"] = {"citation_validation": mode}
    if mode == "warn":
        validation["citation_validation_valid"] = validation["valid"]
        validation["valid"] = True
    return validation


def _parse_answer_draft(content: str, evidence: list[Evidence], *, strict: bool) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        if strict:
            raise
        return {"answer_markdown": content, "claims": [], "insufficient_evidence": False}
    if not isinstance(payload, dict):
        if strict:
            raise ValueError("answer draft is not a JSON object")
        return {"answer_markdown": content, "claims": [], "insufficient_evidence": False}
    if "answer_markdown" not in payload:
        if strict:
            raise ValueError("answer draft missing answer_markdown")
        first = evidence[0].evidence_id if evidence else "S1"
        return {"answer_markdown": f"{content} [{first}]", "claims": [], "insufficient_evidence": False}
    return payload


def _zim_source_url_valid(evidence: Evidence, settings: Settings) -> bool:
    book_name = str(evidence.metadata.get("zim_book_name") or settings.kiwix_book_name)
    entry_path = str(evidence.metadata.get("zim_entry_path") or "")
    if not book_name or not entry_path:
        return False
    expected = build_kiwix_source_url(settings.kiwix_public_base_url, book_name, entry_path)
    return evidence.source_url == expected


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
