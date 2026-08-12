from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from wikipediarag.claim_verifier import claim_verification_blocks, verify_claims
from wikipediarag.config import Settings, get_settings
from wikipediarag.model_client import chat_completion
from wikipediarag.reliability import OperationDeadline
from wikipediarag.retrieval_profile import CitationValidationMode, RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import AnswerabilityStatus, Evidence, RetrievalResult
from wikipediarag.zim_dump import build_kiwix_source_url

logger = logging.getLogger(__name__)
CITATION_RE = re.compile(r"\[(S\d+)\]")

_RECOVERABLE_MODEL_CONTRACT_REASONS = frozenset(
    {
        "unknown_evidence",
        "undeclared_citation",
        "claim_evidence_not_cited",
        "missing_claims",
        "ambiguity_contract",
        "answer_mode_contract",
        "interpretation_citation_contract",
        "interpretation_contract",
        "citation_validation",
    }
)
_MODEL_CONTRACT_ABSTENTION = (
    "Не удалось сформировать проверяемый ответ по найденным источникам. Уточните вопрос или попробуйте позже."
)


class ModelOutputError(ValueError):
    """Safe, non-retryable failure for a provider response contract violation."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "MODEL_OUTPUT_INVALID",
        reason: str = "structured_contract_violation",
    ) -> None:
        super().__init__(message)
        self.safe_code = code
        self.reason = reason
        self.retryable = False
        self.metadata: dict[str, Any] = {}


class ClaimDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    type: str = Field(pattern="^(fact|inference)$")


class InterpretationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=240)
    answer_markdown: str = Field(min_length=1, max_length=12000)
    claims: list[ClaimDraft] = Field(default_factory=list, max_length=16)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


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
    answer_mode: Literal["single", "multiple"] = "single"
    interpretations: list[InterpretationDraft] = Field(default_factory=list, max_length=8)
    clarification_question: str | None = Field(default=None, max_length=1000)
    claims: list[ClaimDraft] = Field(default_factory=list, max_length=32)
    insufficient_evidence: bool
    insufficient_evidence_reason: InsufficientEvidenceReason | None = None


ANSWER_JSON_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "grounded_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "answer_markdown",
                "answer_mode",
                "interpretations",
                "clarification_question",
                "claims",
                "insufficient_evidence",
            ],
            "properties": {
                "answer_markdown": {"type": "string", "minLength": 1, "maxLength": 20000},
                "answer_mode": {"type": "string", "enum": ["single", "multiple"]},
                "clarification_question": {"type": ["string", "null"], "maxLength": 1000},
                "interpretations": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["interpretation_id", "label", "answer_markdown", "claims", "evidence_ids"],
                        "properties": {
                            "interpretation_id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "label": {"type": "string", "minLength": 1, "maxLength": 240},
                            "answer_markdown": {"type": "string", "minLength": 1, "maxLength": 12000},
                            "evidence_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 12,
                                "items": {"type": "string"},
                            },
                            "claims": {
                                "type": "array",
                                "maxItems": 16,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["claim_id", "text", "evidence_ids", "type"],
                                    "properties": {
                                        "claim_id": {"type": "string", "minLength": 1, "maxLength": 128},
                                        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                                        "evidence_ids": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 32,
                                            "items": {"type": "string"},
                                        },
                                        "type": {"type": "string", "enum": ["fact", "inference"]},
                                    },
                                },
                            },
                        },
                    },
                },
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
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["claim_id", "text", "evidence_ids", "type"],
                        "properties": {
                            "claim_id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                            "evidence_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 32,
                                "items": {"type": "string"},
                            },
                            "type": {"type": "string", "enum": ["fact", "inference"]},
                        },
                    },
                },
            },
        },
    },
}


def _answer_json_schema_for_mode(
    *,
    ambiguity_expected: bool,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return the generator schema with an explicit always/ambiguous contract."""
    schema = deepcopy(ANSWER_JSON_SCHEMA)
    if ambiguity_expected:
        answer_schema = schema["json_schema"]["schema"]
        answer_schema["properties"]["answer_mode"]["enum"] = ["multiple"]
        answer_schema["properties"]["interpretations"]["minItems"] = 2
        answer_schema["properties"]["interpretations"]["maxItems"] = 8
        answer_schema["properties"]["clarification_question"] = {"type": "string", "minLength": 1}
    if evidence_ids is not None:
        allowed_ids = sorted({str(evidence_id) for evidence_id in evidence_ids if str(evidence_id)})
        item_schema = {"type": "string", "enum": allowed_ids}
        answer_schema = schema["json_schema"]["schema"]
        answer_schema["properties"]["claims"]["items"]["properties"]["evidence_ids"]["items"] = item_schema
        interpretation_schema = answer_schema["properties"]["interpretations"]["items"]
        interpretation_schema["properties"]["evidence_ids"]["items"] = item_schema
        interpretation_schema["properties"]["claims"]["items"]["properties"]["evidence_ids"]["items"] = item_schema
    return schema


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
    ambiguity_expected = bool(
        active_profile.answer.ambiguity_mode == "always"
        and retrieval.answerability
        and retrieval.answerability.signals.get("ambiguous_entity") is True
    )
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
            "answer_mode": "single",
            "interpretations": [],
            "clarification_question": None,
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
            "answer_mode": "single",
            "interpretations": [],
            "clarification_question": None,
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
    if ambiguity_expected:
        ambiguity_contract_instruction = (
            "Текущий answerability сигнал ambiguous_entity=true. Это обязательный structured-контракт. "
            "НЕМЕДЛЕННО верни answer_mode=multiple. Верни минимум две interpretations, по одной на каждый "
            "подтверждённый смысл, и непустой clarification_question. Нельзя возвращать answer_mode=single, "
            "нельзя возвращать interpretations=[], нельзя перечислять смыслы только в answer_markdown. "
            "Каждая interpretation обязана иметь собственные label, answer_markdown, claims и evidence_ids. "
        )
    elif active_profile.answer.ambiguity_mode == "always":
        ambiguity_contract_instruction = (
            "В режиме always явно проверь наличие нескольких подтверждённых значений. Если найден один смысл, "
            "верни answer_mode=single с interpretations=[]; если найдено несколько — answer_mode=multiple, "
            "interpretations и clarification_question. "
        )
    elif active_profile.answer.ambiguity_mode == "auto":
        ambiguity_contract_instruction = (
            "В режиме auto самостоятельно выдели все разные подтверждённые значения вопроса. Если смыслов "
            "несколько, верни answer_mode=multiple, interpretations и clarification_question; если один — "
            "answer_mode=single с interpretations=[]. "
        )
    else:
        ambiguity_contract_instruction = (
            "В режиме off верни только answer_mode=single, interpretations=[] и clarification_question=null. "
        )
    prompt = (
        "Ответь на русском языке только по источникам ниже. "
        f"{partial_instruction}"
        f"Режим неоднозначности: {active_profile.answer.ambiguity_mode}. Изучи все evidence. "
        f"{ambiguity_contract_instruction}"
        "Не выбирай главное значение при общем неоднозначном вопросе. Не считай противоречия об одном объекте "
        "разными значениями. Не создавай интерпретацию без citations. "
        "Верни JSON по схеме. Каждое фактическое утверждение должно иметь evidence_ids, "
        "а в answer_markdown citation ID вида [S1] должен стоять после утверждения. Каждый [S#] в основном "
        "answer_markdown должен быть указан в evidence_ids какого-либо claims; каждый [S#] внутри interpretation "
        "— в claims этой interpretation. "
        "Если insufficient_evidence=true, используй claims=[] и не более одной citation (лучше без citation).\n\n"
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
        response_format=_answer_json_schema_for_mode(
            ambiguity_expected=ambiguity_expected,
            evidence_ids=[item.evidence_id for item in retrieval.evidence],
        ),
        max_output_tokens=active_profile.answer.max_output_tokens,
        retry_max_output_tokens=active_profile.answer.max_output_tokens * 2,
        stage_output_cap=active_profile.answer.max_output_tokens,
        stage_safety_reserve_tokens=32,
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
        "ambiguity_expected": ambiguity_expected,
        "ambiguity_contract_status": "required" if ambiguity_expected else "not_required",
    }
    parse_started = time.perf_counter()
    try:
        draft = _parse_answer_draft(
            content,
            retrieval.evidence,
            strict=active_profile.requires_real_provider,
            truncated=choice.get("finish_reason") == "length",
            ambiguity_expected=ambiguity_expected,
        )
    except ModelOutputError as exc:
        timings_ms["answer_parse"] = _elapsed_ms(parse_started)
        if _is_recoverable_model_contract_error(exc):
            logger.warning("structured answer contract abstained reason=%s", exc.reason)
            return _model_contract_abstention(
                reason=exc.reason,
                validation_metadata=validation_metadata,
                usage=usage,
                provider_cost=payload.get("cost") or usage.get("cost") or usage.get("total_cost"),
                answerability_status=answerability_status,
                timings_ms=timings_ms,
                started=started,
            )
        exc.metadata = validation_metadata
        exc.metadata["structured_validation_reason"] = exc.reason
        if ambiguity_expected:
            exc.metadata["ambiguity_contract_status"] = "invalid"
            exc.metadata["ambiguity_contract_reason"] = "ambiguity_contract_violation"
        logger.warning("structured answer rejected code=%s reason=%s", exc.safe_code, str(exc))
        raise
    timings_ms["answer_parse"] = _elapsed_ms(parse_started)
    if active_profile.answer.ambiguity_mode == "off" and draft.get("answer_mode", "single") != "single":
        return _model_contract_abstention(
            reason="answer_mode_contract",
            validation_metadata=validation_metadata,
            usage=usage,
            provider_cost=payload.get("cost") or usage.get("cost") or usage.get("total_cost"),
            answerability_status=answerability_status,
            timings_ms=timings_ms,
            started=started,
        )
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
        validation["answer_mode"] = "single"
        validation["interpretations"] = []
        validation["clarification_question"] = None
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
    validation["answer_mode"] = draft.get("answer_mode", "single")
    validation["interpretations"] = list(draft.get("interpretations") or [])
    validation["clarification_question"] = draft.get("clarification_question")
    validation["model_output_normalizations"] = list(draft.get("model_output_normalizations") or [])
    validation["ambiguity_contract_status"] = "satisfied" if ambiguity_expected else "not_required"
    if not validation["valid"]:
        return _model_contract_abstention(
            reason="citation_validation",
            validation_metadata=validation_metadata,
            usage=usage,
            provider_cost=validation["provider_cost"],
            answerability_status=answerability_status,
            timings_ms=timings_ms,
            started=started,
        )
    claims_for_verification = list(draft.get("claims") or [])
    for interpretation in draft.get("interpretations") or []:
        claims_for_verification.extend(list(interpretation.get("claims") or []))
    claim_payload = await verify_claims(
        claims_for_verification,
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


def _is_recoverable_model_contract_error(exc: ModelOutputError) -> bool:
    """Return whether a parsed answer can safely become a deterministic abstention."""

    return exc.safe_code == "MODEL_OUTPUT_INVALID" and exc.reason in _RECOVERABLE_MODEL_CONTRACT_REASONS


def _model_contract_abstention(
    *,
    reason: str,
    validation_metadata: dict[str, Any],
    usage: dict[str, Any],
    provider_cost: Any,
    answerability_status: AnswerabilityStatus | None,
    timings_ms: dict[str, int],
    started: float,
) -> tuple[str, dict[str, object]]:
    """Return a safe terminal answer without retaining an invalid model draft."""

    timings = {**timings_ms, "generation_total": _elapsed_ms(started)}
    return _MODEL_CONTRACT_ABSTENTION, {
        "valid": True,
        "status": "abstained",
        "insufficient_evidence": True,
        "answerability_status": answerability_status.value if answerability_status else None,
        "citations": [],
        "claims": [],
        "answer_mode": "single",
        "interpretations": [],
        "clarification_question": None,
        "usage": usage,
        "provider_cost": provider_cost,
        "model_output_contract_abstained": True,
        "model_output_contract_reason": reason,
        "structured_validation_reason": reason,
        "model_output_normalizations": ["model_output_contract_abstention"],
        **validation_metadata,
        "ambiguity_contract_status": "abstained" if validation_metadata.get("ambiguity_expected") else "not_required",
        "timings_ms": timings,
    }


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
    ambiguity_expected: bool = False,
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
        raw_errors: list[Any] = getattr(exc, "errors", lambda: [])()
        safe_errors: list[dict[str, Any]] = [item for item in raw_errors if isinstance(item, dict)]
        logger.warning(
            "structured answer schema fields rejected fields=%s",
            [{"loc": list(item.get("loc") or []), "type": str(item.get("type") or "")} for item in safe_errors],
        )
        raise ModelOutputError("structured model output failed schema validation", reason="schema_validation") from exc
    normalizations = _canonicalize_answer_draft(draft, ambiguity_expected=ambiguity_expected)
    if draft.insufficient_evidence:
        return _parsed_answer_draft(draft, normalizations=[*normalizations, "insufficient_evidence_abstention"])
    if not draft.claims and not draft.interpretations:
        derived_claims = _derive_claims_from_citations(draft.answer_markdown)
        if derived_claims:
            draft.claims = derived_claims
            normalizations.append("claims_derived_from_citations")
    allowed = {item.evidence_id for item in evidence}
    top_level_claims = list(draft.claims)
    interpretation_claims = [claim for item in draft.interpretations for claim in item.claims]
    all_claims = [*top_level_claims, *interpretation_claims]
    unknown = {evidence_id for claim in all_claims for evidence_id in claim.evidence_ids if evidence_id not in allowed}
    if unknown:
        raise ModelOutputError("structured model output referenced unknown evidence", reason="unknown_evidence")
    declared_citations = {evidence_id for claim in all_claims for evidence_id in claim.evidence_ids}
    top_level_citations = {evidence_id for claim in top_level_claims for evidence_id in claim.evidence_ids}
    answer_citations = set(CITATION_RE.findall(draft.answer_markdown))
    if not draft.insufficient_evidence and answer_citations - declared_citations:
        raise ModelOutputError(
            "structured model output citation is not declared by a claim", reason="undeclared_citation"
        )
    if not draft.insufficient_evidence and top_level_citations - answer_citations:
        raise ModelOutputError(
            "structured model output claim evidence is not cited in the answer", reason="claim_evidence_not_cited"
        )
    if not draft.insufficient_evidence and not draft.claims:
        if not draft.interpretations:
            raise ModelOutputError("non-abstaining answer must contain claims", reason="missing_claims")
    if ambiguity_expected:
        if draft.answer_mode != "multiple":
            raise ModelOutputError("ambiguous always answer must use answer_mode=multiple", reason="ambiguity_contract")
        if len(draft.interpretations) < 2:
            raise ModelOutputError(
                "ambiguous always answer must contain at least two interpretations", reason="ambiguity_contract"
            )
        if not (draft.clarification_question or "").strip():
            raise ModelOutputError(
                "ambiguous always answer must contain clarification_question", reason="ambiguity_contract"
            )
    if draft.answer_mode == "multiple" and len(draft.interpretations) < 2:
        raise ModelOutputError(
            "multiple answer must contain at least two interpretations", reason="answer_mode_contract"
        )
    if draft.answer_mode == "single" and draft.interpretations:
        raise ModelOutputError("single answer must not contain interpretations", reason="answer_mode_contract")
    for interpretation in draft.interpretations:
        interpretation_allowed = set(interpretation.evidence_ids)
        if not interpretation_allowed <= allowed:
            raise ModelOutputError("interpretation referenced unknown evidence", reason="unknown_evidence")
        citations = set(CITATION_RE.findall(interpretation.answer_markdown))
        if not citations <= interpretation_allowed:
            raise ModelOutputError(
                "interpretation citation is outside its evidence_ids", reason="interpretation_citation_contract"
            )
        interpretation_claim_citations = {
            evidence_id for claim in interpretation.claims for evidence_id in claim.evidence_ids
        }
        if interpretation_claim_citations - interpretation_allowed:
            raise ModelOutputError(
                "interpretation claim referenced evidence outside its evidence_ids",
                reason="interpretation_citation_contract",
            )
        if interpretation.claims and not interpretation_claim_citations <= citations:
            raise ModelOutputError(
                "interpretation claim evidence is not cited in its answer",
                reason="interpretation_citation_contract",
            )
        if not citations or not interpretation.claims:
            raise ModelOutputError("interpretation must contain cited claims", reason="interpretation_contract")
    return _parsed_answer_draft(draft, normalizations=normalizations)


def _parsed_answer_draft(draft: AnswerDraft, *, normalizations: list[str]) -> dict[str, Any]:
    parsed = draft.model_dump(mode="json")
    if normalizations:
        parsed["model_output_normalizations"] = normalizations
    return parsed


def _derive_claims_from_citations(answer_markdown: str) -> list[ClaimDraft]:
    """Recover provider-omitted bookkeeping claims from already-cited sentences.

    The source text and citation IDs are preserved verbatim for subsequent
    citation and claim verification; an answer without a cited sentence still
    fails the normal grounded-answer contract.
    """

    claims: list[ClaimDraft] = []
    for segment in re.split(r"(?<=[.!?])\s+|\n+", answer_markdown):
        evidence_ids = list(dict.fromkeys(CITATION_RE.findall(segment)))
        text = CITATION_RE.sub("", segment).strip(" \t-–—")
        if not evidence_ids or not text:
            continue
        claims.append(
            ClaimDraft(
                claim_id=f"derived-{len(claims) + 1}",
                text=text[:4000],
                evidence_ids=evidence_ids,
                type="fact",
            )
        )
    return claims


def _canonicalize_answer_draft(draft: AnswerDraft, *, ambiguity_expected: bool) -> list[str]:
    """Apply lossless presentation normalizations before groundedness validation.

    Provider-local claim and interpretation keys, plus a multiple-answer marker
    without two interpretations, do not change the assertions or their
    evidence. Normalize only those bookkeeping fields; all citations, claims,
    and ambiguity cases required by retrieval still pass the strict checks
    below.
    """

    normalizations: list[str] = []
    interpretation_ids: set[str] = set()
    interpretation_index = 1
    for interpretation in draft.interpretations:
        if interpretation.interpretation_id not in interpretation_ids:
            interpretation_ids.add(interpretation.interpretation_id)
            continue
        while (candidate := f"interpretation-{interpretation_index}") in interpretation_ids:
            interpretation_index += 1
        interpretation.interpretation_id = candidate
        interpretation_ids.add(candidate)
        interpretation_index += 1
        normalizations.append("duplicate_interpretation_id")
    claims = [*draft.claims, *(claim for item in draft.interpretations for claim in item.claims)]
    seen: set[str] = set()
    generated_index = 1
    for claim in claims:
        if claim.claim_id not in seen:
            seen.add(claim.claim_id)
            continue
        while (candidate := f"claim-{generated_index}") in seen:
            generated_index += 1
        claim.claim_id = candidate
        seen.add(candidate)
        generated_index += 1
        normalizations.append("duplicate_claim_id")
    if not ambiguity_expected and draft.answer_mode == "multiple" and len(draft.interpretations) < 2:
        draft.answer_mode = "single"
        draft.interpretations = []
        draft.clarification_question = None
        normalizations.append("multiple_without_two_interpretations")
    return normalizations


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
