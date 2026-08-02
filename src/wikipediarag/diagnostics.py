from __future__ import annotations

from typing import Any

from wikipediarag.ids import stable_hash
from wikipediarag.retrieval_profile import RetrievalProfile
from wikipediarag.schemas import AnswerabilityDecision, AnswerabilityStatus, Evidence, RetrievalResult

DIAGNOSTICS_VERSION = "experimental_diagnostics_v1"
ANSWER_ARTIFACT_VERSION = "experimental_answer_artifact_v1"
SAFE_ANSWERABILITY_SIGNAL_KEYS = {
    "exact_title_match",
    "top_relevance_score",
    "top_score_threshold",
    "page_diversity",
    "required_part_count",
    "covered_part_count",
    "coverage_ratio",
    "final_evidence_min",
    "evidence_count",
    "conflict_marker",
}


def initial_route_decision(
    *,
    mode: str,
    extended_policy: str,
    classifier_suggested_extended: bool,
) -> dict[str, str]:
    if mode == "extended":
        return {"route": "extended_first", "reason": "requested_extended_mode"}
    if extended_policy in {"always", "conditional"} and classifier_suggested_extended:
        return {"route": "extended_first", "reason": "query_classifier_suggested_extended_search"}
    return {"route": "direct_retrieval", "reason": "direct_path_selected"}


def repair_route_decision(answerability: AnswerabilityDecision | None) -> dict[str, str]:
    status = answerability.status.value if answerability else "UNKNOWN"
    reason = f"answerability_{status.lower()}_allows_extended_repair"
    return {"route": "extended_repair", "reason": reason}


def build_search_plan(
    *,
    query: str,
    mode: str,
    route: str,
    route_reason: str,
    knowledge_base_id: str,
    knowledge_base_ids: list[str] | None = None,
    trace_id: str,
    profile: RetrievalProfile,
    top_k: int | None = None,
    include_generation: bool = True,
) -> dict[str, Any]:
    normalized = " ".join(query.split())
    steps = [
        {"name": "path_selected", "status": "planned"},
        {"name": "query_normalization", "status": "planned"},
    ]
    if route in {"extended_first", "extended_repair"}:
        steps.append({"name": "bounded_extended_search", "status": "planned"})
    steps.extend(
        [
            {"name": "bm25", "status": "planned" if profile.retrieval.bm25 else "disabled"},
            {"name": "dense", "status": "planned" if profile.retrieval.dense else "disabled"},
            {"name": "fusion", "status": "planned", "mode": profile.retrieval.fusion},
            {"name": "rerank", "status": "planned" if profile.retrieval.rerank else "disabled"},
            {"name": "context_selection", "status": "planned"},
            {"name": "answerability", "status": "planned"},
        ]
    )
    if include_generation:
        steps.extend(
            [
                {"name": "answer_generation", "status": "planned"},
                {"name": "citation_validation", "status": "planned"},
                {
                    "name": "claim_verification",
                    "status": "planned" if profile.answer.verification.claim_verification_enabled else "disabled",
                },
            ]
        )
    return {
        "version": DIAGNOSTICS_VERSION,
        "experimental": True,
        "mode": mode,
        "route": route,
        "route_reason": route_reason,
        "trace_id": trace_id,
        "knowledge_base_id": knowledge_base_id,
        "knowledge_base_ids": knowledge_base_ids or [knowledge_base_id],
        "query": {
            "fingerprint": stable_hash(["diagnostics_query", normalized], 16),
            "length_chars": len(query),
            "terms_estimate": len(normalized.split()),
        },
        "profile": {
            "name": profile.name,
            "source": profile.source,
            "version": profile.version,
        },
        "retrieval": {
            "bm25": profile.retrieval.bm25,
            "dense": profile.retrieval.dense,
            "fusion": profile.retrieval.fusion,
            "top_k": top_k or profile.retrieval.top_k,
            "bm25_top_k": profile.retrieval.bm25_top_k,
            "dense_top_k": profile.retrieval.dense_top_k,
            "rerank_top_k": profile.retrieval.rerank_top_k,
        },
        "postprocess": {
            "dedup": profile.postprocess.dedup,
            "page_quota": profile.postprocess.page_quota,
            "parent_expansion": profile.postprocess.parent_expansion,
            "context_packing": profile.postprocess.context_packing,
            "final_evidence_max": profile.postprocess.final_evidence_max,
            "extended_search": profile.postprocess.extended_search,
        },
        "constraints": {
            "tenant_scope": "server_owned",
            "knowledge_base_scope": "multi_kb" if len(knowledge_base_ids or [knowledge_base_id]) > 1 else "single_kb",
            "client_tenant_filters": "ignored",
            "llm_security_fields": "not_allowed",
        },
        "steps": steps,
    }


def build_answer_artifact(
    *,
    query_run_id: str | None,
    knowledge_base_id: str,
    search_plan: dict[str, Any],
    retrieval: RetrievalResult,
    validation: dict[str, Any] | None,
    timings_ms: dict[str, int] | None,
    answer_present: bool,
) -> dict[str, Any]:
    validation_payload = dict(validation or {})
    artifact = {
        "version": ANSWER_ARTIFACT_VERSION,
        "experimental": True,
        "query_run_id": query_run_id,
        "trace_id": retrieval.trace_id,
        "knowledge_base_id": knowledge_base_id,
        "knowledge_base_ids": search_plan.get("knowledge_base_ids", [knowledge_base_id]),
        "answer_present": answer_present,
        "search_plan": search_plan,
        "root_cause": _root_cause(retrieval, validation_payload),
        "contracts": {
            "index_contract_id": retrieval.index_contract_id,
            "run_contract_id": retrieval.run_contract_id,
        },
        "answerability": _answerability_summary(retrieval.answerability),
        "retrieval": _retrieval_summary(retrieval),
        "validation": _validation_summary(validation_payload),
        "timings_ms": dict(timings_ms or {}),
    }
    return artifact


def build_failure_artifact(
    *,
    query_run_id: str | None,
    knowledge_base_id: str,
    search_plan: dict[str, Any],
    retrieval: RetrievalResult | None,
    stage: str,
    last_successful_stage: str,
    code: str,
    retryable: bool,
    trace_id: str,
) -> dict[str, Any]:
    return {
        "version": ANSWER_ARTIFACT_VERSION,
        "experimental": True,
        "query_run_id": query_run_id,
        "trace_id": trace_id,
        "knowledge_base_id": knowledge_base_id,
        "knowledge_base_ids": search_plan.get("knowledge_base_ids", [knowledge_base_id]),
        "answer_present": False,
        "search_plan": search_plan,
        "root_cause": {
            "version": DIAGNOSTICS_VERSION,
            "code": "runtime_failure",
            "category": _failure_category(stage),
            "severity": "error",
            "message": "chat run failed before a grounded answer artifact could be completed",
            "retryable": retryable,
            "signals": {
                "stage": stage,
                "last_successful_stage": last_successful_stage,
                "exception_code": code,
                "had_retrieval": retrieval is not None,
            },
        },
        "contracts": {
            "index_contract_id": retrieval.index_contract_id if retrieval else "",
            "run_contract_id": retrieval.run_contract_id if retrieval else "",
        },
        "answerability": _answerability_summary(retrieval.answerability if retrieval else None),
        "retrieval": _retrieval_summary(retrieval) if retrieval else {"evidence_count": 0, "events": []},
        "validation": {},
        "timings_ms": {},
    }


def _root_cause(retrieval: RetrievalResult, validation: dict[str, Any]) -> dict[str, Any]:
    claim = _claim_verification_summary(validation)
    if claim.get("blocked"):
        return _cause(
            code="claim_verification_blocked",
            category="verification",
            severity="error",
            message="generated answer was blocked by claim-level evidence verification",
            signals={"claim_verification": claim},
        )
    citation_valid = validation.get("citation_validation_valid", validation.get("valid"))
    if citation_valid is False:
        return _cause(
            code="citation_validation_failed",
            category="verification",
            severity="error",
            message="generated answer failed citation validation",
            signals=_validation_summary(validation),
        )
    status = retrieval.answerability.status if retrieval.answerability else None
    if not retrieval.evidence:
        return _cause(
            code="no_evidence_retrieved",
            category="retrieval",
            severity="warning",
            message="retrieval produced no evidence for the question",
            signals={"answerability_status": status.value if status else None},
        )
    if status == AnswerabilityStatus.conflicting:
        return _cause(
            code="conflicting_evidence",
            category="retrieval",
            severity="warning",
            message="retrieved evidence appears conflicting",
            signals=_answerability_summary(retrieval.answerability),
        )
    if status == AnswerabilityStatus.unanswerable:
        return _cause(
            code="answerability_unanswerable",
            category="retrieval",
            severity="warning",
            message="retrieved evidence does not cover required answer-bearing facts",
            signals=_answerability_summary(retrieval.answerability),
        )
    if status == AnswerabilityStatus.partial or bool(validation.get("insufficient_evidence")):
        return _cause(
            code="partial_evidence",
            category="retrieval",
            severity="warning",
            message="answer is based on partial evidence coverage",
            signals=_answerability_summary(retrieval.answerability),
        )
    if not validation:
        return _cause(
            code="retrieval_evidence_available",
            category="retrieval",
            severity="info",
            message="retrieval completed and evidence is available for inspection",
            signals={"evidence_count": len(retrieval.evidence)},
        )
    return _cause(
        code="answered_with_valid_citations",
        category="success",
        severity="info",
        message="answer completed with valid diagnostic signals",
        signals={"evidence_count": len(retrieval.evidence), "citations": list(validation.get("citations") or [])},
    )


def _cause(
    *,
    code: str,
    category: str,
    severity: str,
    message: str,
    signals: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": DIAGNOSTICS_VERSION,
        "code": code,
        "category": category,
        "severity": severity,
        "message": message,
        "signals": signals,
    }


def _answerability_summary(answerability: AnswerabilityDecision | None) -> dict[str, Any]:
    if answerability is None:
        return {}
    return {
        "version": answerability.version,
        "status": answerability.status.value,
        "confidence": answerability.confidence,
        "reason": answerability.reason,
        "required_parts_count": len(answerability.required_parts),
        "covered_parts_count": len(answerability.covered_parts),
        "missing_parts_count": len(answerability.missing_parts),
        "signals": _safe_answerability_signals(answerability.signals),
    }


def _retrieval_summary(retrieval: RetrievalResult) -> dict[str, Any]:
    return {
        "evidence_count": len(retrieval.evidence),
        "insufficient_evidence": retrieval.insufficient_evidence,
        "evidence": [_evidence_summary(item) for item in retrieval.evidence],
        "events": [_event_summary(event) for event in retrieval.events],
        "used_extended_search": any(event.get("stage") == "harness" for event in retrieval.events),
        "stop_reason": _extended_stop_reason(retrieval.events),
    }


def _evidence_summary(evidence: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "chunk_id": evidence.chunk_id,
        "title": evidence.title,
        "section_path": evidence.section_path,
        "source_url": evidence.source_url,
        "scores": evidence.scores,
        "ranks": evidence.ranks,
    }


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"stage": event.get("stage")}
    for key in ("count", "latency_ms", "stage_latency_ms", "tool", "new_evidence", "new_neighbors"):
        if key in event:
            payload[key] = event[key]
    if event.get("top"):
        payload["top"] = list(event.get("top") or [])[:10]
    if event.get("decision") and event.get("stage") == "answerability":
        decision = event.get("decision")
        if isinstance(decision, dict):
            payload["decision"] = _answerability_decision_summary(decision)
    if event.get("reason"):
        payload["reason"] = event.get("reason")
    if event.get("stop_reason"):
        payload["stop_reason"] = event.get("stop_reason")
    if isinstance(event.get("timings_ms"), dict):
        payload["timings_ms"] = {
            str(key): int(value) for key, value in dict(event["timings_ms"]).items() if isinstance(value, int | float)
        }
    return payload


def _validation_summary(validation: dict[str, Any]) -> dict[str, Any]:
    if not validation:
        return {}
    return {
        "valid": validation.get("valid"),
        "status": validation.get("status"),
        "insufficient_evidence": validation.get("insufficient_evidence"),
        "answerability_status": validation.get("answerability_status"),
        "citations": list(validation.get("citations") or []),
        "unknown_citation_count": len(list(validation.get("unknown") or [])),
        "unsupported_claim_count": len(list(validation.get("unsupported_claims") or [])),
        "phantom_claim_citation_count": len(list(validation.get("phantom_claim_citations") or [])),
        "source_url_error_count": len(list(validation.get("source_url_errors") or [])),
        "citation_validation_valid": validation.get("citation_validation_valid"),
        "claim_verification": _claim_verification_summary(validation),
        "model_alias": validation.get("model_alias"),
        "provider": validation.get("provider"),
    }


def _answerability_decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    signals = decision.get("signals")
    if not isinstance(signals, dict):
        signals = {}
    return {
        "version": decision.get("version"),
        "status": decision.get("status"),
        "confidence": decision.get("confidence"),
        "reason": decision.get("reason"),
        "required_parts_count": _list_count(decision.get("required_parts")),
        "covered_parts_count": _list_count(decision.get("covered_parts")),
        "missing_parts_count": _list_count(decision.get("missing_parts")),
        "signals": _safe_answerability_signals(signals),
    }


def _safe_answerability_signals(signals: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in SAFE_ANSWERABILITY_SIGNAL_KEYS:
        value = signals.get(key)
        if isinstance(value, bool | int | float):
            safe[key] = value
    return safe


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _claim_verification_summary(validation: dict[str, Any]) -> dict[str, Any]:
    payload = validation.get("claim_verification")
    if not isinstance(payload, dict):
        return {}
    unsupported = payload.get("unsupported_claim_ids")
    if not isinstance(unsupported, list):
        unsupported = []
    return {
        "status": payload.get("status"),
        "blocked": payload.get("status") == "blocked",
        "unsupported_claim_count": len(unsupported),
        "mode": payload.get("mode"),
    }


def _extended_stop_reason(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("stage") == "harness" and event.get("stop_reason"):
            return str(event["stop_reason"])
        state = event.get("state")
        if event.get("stage") == "harness" and isinstance(state, dict) and state.get("stop_reason"):
            return str(state["stop_reason"])
    return None


def _failure_category(stage: str) -> str:
    if stage in {"retrieval", "extended_search", "path_selected"}:
        return "retrieval"
    if stage in {"answer_generation", "query_run_complete"}:
        return "generation"
    return "runtime"
