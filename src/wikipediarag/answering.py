from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from wikipediarag.claim_verifier import claim_verification_blocks, verify_claims
from wikipediarag.config import Settings, get_settings
from wikipediarag.model_client import chat_completion
from wikipediarag.reliability import OperationDeadline
from wikipediarag.retrieval_profile import CitationValidationMode, RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import AnswerabilityStatus, Evidence, RetrievalResult
from wikipediarag.zim_dump import build_kiwix_source_url

CITATION_RE = re.compile(r"\[(S\d+)\]")


class ModelOutputError(ValueError):
    """Safe, non-retryable failure for a provider response contract violation."""

    def __init__(self, message: str, *, code: str = "MODEL_OUTPUT_INVALID") -> None:
        super().__init__(message)
        self.safe_code = code
        self.retryable = False
        self.metadata: dict[str, Any] = {}


class ClaimDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    type: str = Field(pattern="^(fact|inference)$")


InsufficientEvidenceReason = Literal[
    "no_evidence",
    "insufficient_context",
    "missing_attribute",
    "ambiguous_entity",
    "conflicting_sources",
    "unsupported_claim",
]


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_markdown: str = Field(min_length=1, max_length=20000)
    claims: list[ClaimDraft] = Field(default_factory=list, max_length=32)
    insufficient_evidence: bool
    insufficient_evidence_reason: InsufficientEvidenceReason | None = None

    @field_validator("claims")
    @classmethod
    def unique_claim_ids(cls, value: list[ClaimDraft]) -> list[ClaimDraft]:
        identifiers = [item.claim_id for item in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("claim_id values must be unique")
        return value


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
                "insufficient_evidence_reason": {
                    "type": ["string", "null"],
                    "enum": [
                        "no_evidence",
                        "insufficient_context",
                        "missing_attribute",
                        "ambiguous_entity",
                        "conflicting_sources",
                        "unsupported_claim",
                        None,
                    ],
                },
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
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
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
        max_output_tokens=active_profile.answer.max_output_tokens,
        deadline=deadline,
        correlation_id=correlation_id,
    )
    timings_ms["model_chat"] = _elapsed_ms(model_started)
    choice = dict(payload.get("choices", [{}])[0] or {})
    message = dict(choice.get("message") or {})
    content = str(message.get("content") or "")
    usage = dict(payload.get("usage") or {})
    validation_metadata = {
        "model_alias": active_profile.model_aliases.generator_main,
        "provider": payload.get("provider"),
        "provider_request_id": payload.get("id"),
        "model_gateway": dict(payload.get("_gateway_metadata") or {}),
        "finish_reason": choice.get("finish_reason"),
        "max_output_tokens": active_profile.answer.max_output_tokens,
    }
    parse_started = time.perf_counter()
    try:
        draft = _parse_answer_draft(
            content,
            retrieval.evidence,
            strict=active_profile.requires_real_provider,
            truncated=choice.get("finish_reason") == "length",
        )
    except ModelOutputError as exc:
        exc.metadata = validation_metadata
        raise
    timings_ms["answer_parse"] = _elapsed_ms(parse_started)
    answer = str(draft["answer_markdown"])
    draft_insufficient_evidence = bool(draft.get("insufficient_evidence"))
    if active_profile.answer.insufficient_evidence_mode and draft_insufficient_evidence:
        citation = f" [{retrieval.evidence[0].evidence_id}]" if retrieval.evidence else ""
        answer = (
            "В предоставленных источниках нет достаточного подтверждения, чтобы надёжно ответить на вопрос. "
            "Я не буду делать неподтверждённый вывод."
            f"{citation}"
        )
        validation = validate_citations_with_policy(
            answer,
            retrieval.evidence,
            claims=[],
            mode=active_profile.answer.verification.citation_validation,
            settings=resolved,
        )
        validation["usage"] = usage
        validation.update(validation_metadata)
        validation["provider_cost"] = payload.get("cost") or usage.get("cost") or usage.get("total_cost")
        validation["answerability_status"] = answerability_status.value if answerability_status else None
        validation["insufficient_evidence"] = True
        validation["timings_ms"] = {**timings_ms, "generation_total": _elapsed_ms(started)}
        return answer, validation
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
    validation.update(validation_metadata)
    validation["provider_cost"] = payload.get("cost") or usage.get("cost") or usage.get("total_cost")
    validation["answerability_status"] = answerability_status.value if answerability_status else None
    validation["insufficient_evidence"] = bool(retrieval.insufficient_evidence or draft_insufficient_evidence)
    if not validation["valid"]:
        error = ModelOutputError("generated answer failed citation validation")
        error.metadata = validation_metadata
        raise error
    claim_payload = await verify_claims(
        list(draft.get("claims") or []),
        retrieval.evidence,
        settings=resolved,
        profile=active_profile,
        deadline=deadline,
        correlation_id=correlation_id,
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


def _parse_answer_draft(
    content: str,
    evidence: list[Evidence],
    *,
    strict: bool,
    truncated: bool = False,
) -> dict[str, Any]:
    normalized = _extract_structured_json(content, truncated=truncated)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ModelOutputError(
            "structured model output is not valid JSON",
            code="MODEL_OUTPUT_TRUNCATED" if truncated or _looks_truncated(normalized) else "MODEL_OUTPUT_INVALID",
        ) from exc
    if not isinstance(payload, dict):
        raise ModelOutputError("structured model output is not a JSON object")
    try:
        draft = AnswerDraft.model_validate(payload)
    except ValueError as exc:
        raise ModelOutputError("structured model output failed schema validation") from exc
    allowed = {item.evidence_id for item in evidence}
    unknown = {
        evidence_id for claim in draft.claims for evidence_id in claim.evidence_ids if evidence_id not in allowed
    }
    if unknown:
        raise ModelOutputError("structured model output referenced unknown evidence")
    declared_citations = {evidence_id for claim in draft.claims for evidence_id in claim.evidence_ids}
    answer_citations = set(CITATION_RE.findall(draft.answer_markdown))
    if not draft.insufficient_evidence and answer_citations - declared_citations:
        raise ModelOutputError("structured model output citation is not declared by a claim")
    if not draft.insufficient_evidence and declared_citations - answer_citations:
        raise ModelOutputError("structured model output claim evidence is not cited in the answer")
    if draft.insufficient_evidence and len(answer_citations) > 1:
        raise ModelOutputError("insufficient-evidence draft may contain at most one citation")
    if not draft.insufficient_evidence and not draft.claims:
        raise ModelOutputError("non-abstaining answer must contain claims")
    return draft.model_dump(mode="json")


def _extract_structured_json(content: str, *, truncated: bool = False) -> str:
    normalized = content.lstrip("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", normalized, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        normalized = fenced.group(1).strip()
    if not normalized:
        raise ModelOutputError("structured model output is empty")
    if normalized.startswith("{") and normalized.endswith("}"):
        return normalized
    if normalized.startswith("{"):
        try:
            _, end = json.JSONDecoder().raw_decode(normalized)
        except json.JSONDecodeError:
            end = -1
        if end > 0 and normalized[end:].strip():
            raise ModelOutputError("structured model output contains commentary outside JSON")
    raise ModelOutputError(
        "structured model output is truncated"
        if truncated or _looks_truncated(normalized)
        else "structured model output contains commentary outside JSON",
        code="MODEL_OUTPUT_TRUNCATED" if truncated or _looks_truncated(normalized) else "MODEL_OUTPUT_INVALID",
    )


def _looks_truncated(content: str) -> bool:
    return content.startswith("{") and not content.endswith("}")


def _zim_source_url_valid(evidence: Evidence, settings: Settings) -> bool:
    book_name = str(evidence.metadata.get("zim_book_name") or settings.kiwix_book_name)
    entry_path = str(evidence.metadata.get("zim_entry_path") or "")
    if not book_name or not entry_path:
        return False
    expected = build_kiwix_source_url(settings.kiwix_public_base_url, book_name, entry_path)
    return evidence.source_url == expected


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
