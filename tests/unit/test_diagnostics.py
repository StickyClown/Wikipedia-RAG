from __future__ import annotations

import json
from typing import Any

from wikipediarag.config import Settings
from wikipediarag.diagnostics import build_answer_artifact, build_search_plan
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import AnswerabilityDecision, AnswerabilityStatus, Evidence, RetrievalResult


def test_search_plan_is_experimental_and_redacts_raw_query() -> None:
    profile = get_retrieval_profile("test_mock", Settings())

    plan = build_search_plan(
        query="секретный пользовательский запрос про паспорт",
        mode="normal",
        route="direct_retrieval",
        route_reason="direct_path_selected",
        knowledge_base_id="kb",
        trace_id="trace",
        profile=profile,
    )

    payload = json.dumps(plan, ensure_ascii=False)
    assert plan["experimental"] is True
    assert plan["query"]["fingerprint"]
    assert "секретный пользовательский запрос" not in payload
    assert plan["constraints"]["tenant_scope"] == "server_owned"


def test_answer_artifact_summarizes_evidence_without_raw_content() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    plan = _plan(profile)
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Статья",
                section_path=["Статья"],
                content="raw document content must not be copied into diagnostics",
                source_url="http://localhost/source",
                scores={"rerank": 1.0},
            )
        ],
        events=[{"stage": "context", "count": 1, "latency_ms": 4, "top": ["c1"]}],
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.answerable,
            confidence=0.9,
            reason="exact_title_overlap",
            signals={"query_names": ["секретное имя"], "evidence_count": 1},
        ),
    )

    artifact = build_answer_artifact(
        query_run_id="run",
        knowledge_base_id="kb",
        search_plan=plan,
        retrieval=retrieval,
        validation=None,
        timings_ms={"retrieval_total": 4},
        answer_present=False,
    )

    payload = json.dumps(artifact, ensure_ascii=False)
    assert artifact["experimental"] is True
    assert artifact["root_cause"]["code"] == "retrieval_evidence_available"
    assert artifact["retrieval"]["evidence"][0]["chunk_id"] == "c1"
    assert "raw document content" not in payload
    assert "секретное имя" not in payload
    assert artifact["answerability"]["signals"] == {"evidence_count": 1}


def test_answer_artifact_marks_claim_verification_block_as_root_cause() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Статья",
                section_path=["Статья"],
                content="Россия - государство.",
                source_url="http://localhost/source",
            )
        ],
        events=[],
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.answerable,
            confidence=0.9,
            reason="exact_title_overlap",
        ),
    )

    artifact = build_answer_artifact(
        query_run_id="run",
        knowledge_base_id="kb",
        search_plan=_plan(profile),
        retrieval=retrieval,
        validation={
            "valid": True,
            "claim_verification": {
                "status": "blocked",
                "mode": "deterministic_strict",
                "unsupported_claim_ids": ["claim-1"],
            },
        },
        timings_ms={},
        answer_present=True,
    )

    assert artifact["root_cause"]["code"] == "claim_verification_blocked"
    assert artifact["root_cause"]["category"] == "verification"
    assert artifact["validation"]["claim_verification"]["unsupported_claim_count"] == 1


def test_answer_artifact_marks_model_contract_abstention_without_provider_content() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Россия",
                section_path=[],
                content="секрет",
                source_url="u1",
            )
        ],
        events=[],
        answerability=AnswerabilityDecision(status=AnswerabilityStatus.partial, confidence=0.8, reason="partial"),
    )

    artifact = build_answer_artifact(
        query_run_id="run",
        knowledge_base_id="kb",
        search_plan=_plan(profile),
        retrieval=retrieval,
        validation={
            "valid": True,
            "model_output_contract_abstained": True,
            "model_output_contract_reason": "undeclared_citation",
        },
        timings_ms={},
        answer_present=True,
    )

    assert artifact["root_cause"] == {
        "version": artifact["root_cause"]["version"],
        "code": "model_output_contract_abstained",
        "category": "generation",
        "severity": "warning",
        "message": "model output could not satisfy the grounded answer contract",
        "signals": {"reason": "undeclared_citation"},
    }


def _plan(profile: RetrievalProfile) -> dict[str, Any]:
    return build_search_plan(
        query="q",
        mode="normal",
        route="direct_retrieval",
        route_reason="direct_path_selected",
        knowledge_base_id="kb",
        trace_id="trace",
        profile=profile,
    )
