from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from wikipediarag.config import Settings, get_settings
from wikipediarag.embedding import normalize_for_embedding
from wikipediarag.model_client import chat_completion
from wikipediarag.reliability import OperationDeadline
from wikipediarag.retrieval_profile import RetrievalProfile, VerificationPolicy
from wikipediarag.schemas import Evidence

CLAIM_VERIFICATION_SCHEMA_VERSION = "claim_verifier_v1"

ClaimSupportStatus = Literal["supported", "partially_supported", "unsupported", "contradicted"]


class ClaimSupportVerdict(BaseModel):
    claim_id: str
    status: ClaimSupportStatus
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


VERIFIER_JSON_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "claim_support_verdicts",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdicts"],
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["claim_id", "status", "evidence_ids", "reason", "confidence"],
                        "properties": {
                            "claim_id": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["supported", "partially_supported", "unsupported", "contradicted"],
                            },
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "reason": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        },
                    },
                }
            },
        },
    },
}


async def verify_claims(
    claims: list[dict[str, Any]],
    evidence: list[Evidence],
    *,
    settings: Settings | None = None,
    profile: RetrievalProfile,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    started = time.perf_counter()
    policy = profile.answer.verification
    if policy.claim_verification == "off":
        return _disabled(policy, started)

    factual_claims = [claim for claim in claims if claim.get("type") == "fact"]
    if not factual_claims:
        return _result(policy, [], started, model_alias="")

    if policy.claim_verification_uses_llm:
        try:
            verdicts, provider_payload = await _verify_claims_llm(
                factual_claims,
                evidence,
                settings=settings,
                profile=profile,
                deadline=deadline,
                correlation_id=correlation_id,
            )
            result = _result(policy, verdicts, started, model_alias=profile.model_aliases.verifier)
            result["provider"] = provider_payload.get("provider")
            result["provider_request_id"] = provider_payload.get("id")
            result["usage"] = dict(provider_payload.get("usage") or {})
            result["model_gateway"] = dict(provider_payload.get("_gateway_metadata") or {})
            return result
        except Exception:
            if profile.requires_real_provider and policy.claim_verification_strict:
                raise
            verdicts = _verify_claims_deterministic(factual_claims, evidence)
            result = _result(policy, verdicts, started, model_alias="")
            result["fallback_reason"] = "llm_verifier_unavailable"
            return result

    verdicts = _verify_claims_deterministic(factual_claims, evidence)
    return _result(policy, verdicts, started, model_alias="")


def claim_verification_blocks(payload: dict[str, Any]) -> bool:
    if not payload.get("strict"):
        return False
    return bool(payload.get("unsupported_claims") or payload.get("contradicted_claims"))


def _disabled(policy: VerificationPolicy, started: float) -> dict[str, Any]:
    return {
        "version": CLAIM_VERIFICATION_SCHEMA_VERSION,
        "policy": policy.model_dump(mode="json"),
        "enabled": False,
        "strict": False,
        "status": "disabled_by_policy",
        "verdicts": [],
        "unsupported_claims": [],
        "contradicted_claims": [],
        "model_alias": "",
        "timings_ms": {"claim_verification": _elapsed_ms(started)},
    }


def _result(
    policy: VerificationPolicy,
    verdicts: list[ClaimSupportVerdict],
    started: float,
    *,
    model_alias: str,
) -> dict[str, Any]:
    unsupported = [item.claim_id for item in verdicts if item.status == "unsupported"]
    contradicted = [item.claim_id for item in verdicts if item.status == "contradicted"]
    return {
        "version": CLAIM_VERIFICATION_SCHEMA_VERSION,
        "policy": policy.model_dump(mode="json"),
        "enabled": True,
        "strict": policy.claim_verification_strict,
        "status": "blocked" if policy.claim_verification_strict and (unsupported or contradicted) else "completed",
        "verdicts": [item.model_dump(mode="json") for item in verdicts],
        "unsupported_claims": unsupported,
        "contradicted_claims": contradicted,
        "model_alias": model_alias,
        "timings_ms": {"claim_verification": _elapsed_ms(started)},
    }


def _verify_claims_deterministic(claims: list[dict[str, Any]], evidence: list[Evidence]) -> list[ClaimSupportVerdict]:
    allowed = {item.evidence_id: item for item in evidence}
    verdicts: list[ClaimSupportVerdict] = []
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        text = str(claim.get("text") or "")
        evidence_ids = [str(item) for item in claim.get("evidence_ids", []) if str(item) in allowed]
        if not evidence_ids:
            verdicts.append(
                ClaimSupportVerdict(
                    claim_id=claim_id,
                    status="unsupported",
                    evidence_ids=[],
                    reason="claim_has_no_supplied_evidence",
                    confidence=0.9,
                )
            )
            continue
        claim_terms = _claim_terms(text)
        evidence_text = " ".join(
            normalize_for_embedding(f"{allowed[evidence_id].title} {allowed[evidence_id].content}")
            for evidence_id in evidence_ids
        )
        overlap = {term for term in claim_terms if term in evidence_text}
        if not claim_terms or len(overlap) >= max(1, min(3, len(claim_terms))):
            status: ClaimSupportStatus = "supported"
            reason = "claim_terms_overlap_supplied_evidence"
            confidence = 0.75
        elif overlap:
            status = "partially_supported"
            reason = "claim_terms_partially_overlap_supplied_evidence"
            confidence = 0.45
        else:
            status = "unsupported"
            reason = "claim_terms_absent_from_supplied_evidence"
            confidence = 0.8
        verdicts.append(
            ClaimSupportVerdict(
                claim_id=claim_id,
                status=status,
                evidence_ids=evidence_ids,
                reason=reason,
                confidence=confidence,
            )
        )
    return verdicts


async def _verify_claims_llm(
    claims: list[dict[str, Any]],
    evidence: list[Evidence],
    *,
    settings: Settings | None,
    profile: RetrievalProfile,
    deadline: OperationDeadline | None,
    correlation_id: str,
) -> tuple[list[ClaimSupportVerdict], dict[str, Any]]:
    resolved = settings or get_settings()
    allowed = {item.evidence_id for item in evidence}
    prompt = {
        "claims": [
            {
                "claim_id": str(claim.get("claim_id") or ""),
                "text": str(claim.get("text") or ""),
                "evidence_ids": [str(item) for item in claim.get("evidence_ids", []) if str(item) in allowed],
            }
            for claim in claims
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "title": item.title,
                "section_path": item.section_path,
                "text": item.content[:1200],
            }
            for item in evidence
        ],
    }
    payload = await chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "Ты проверяешь, поддерживают ли supplied evidence заявленные claims. "
                    "Не добавляй новые источники и используй только переданные evidence_id."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        resolved,
        alias=profile.model_aliases.verifier,
        response_format=VERIFIER_JSON_SCHEMA,
        max_output_tokens=profile.deep_research.verifier.max_output_tokens,
        deadline=deadline,
        correlation_id=correlation_id,
    )
    content = str(payload["choices"][0]["message"]["content"])
    parsed = json.loads(content)
    verdicts = [
        ClaimSupportVerdict.model_validate(item)
        for item in parsed.get("verdicts", [])
        if set(str(evidence_id) for evidence_id in item.get("evidence_ids", [])) <= allowed
    ]
    return verdicts, payload


def _claim_terms(text: str) -> set[str]:
    return {token for token in normalize_for_embedding(text).split() if len(token) >= 4 and not token.isdigit()}


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
