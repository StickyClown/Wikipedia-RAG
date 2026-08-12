from __future__ import annotations

import json
from typing import Any

import pytest

from wikipediarag.answering import (
    ModelOutputError,
    _answer_json_schema_for_mode,
    _parse_answer_draft,
    generate_answer,
    validate_citations,
    validate_citations_with_policy,
)
from wikipediarag.config import Settings
from wikipediarag.observability import safe_telemetry_payload
from wikipediarag.retrieval import (
    _order_ambiguity_candidates,
    _snapshot_candidates,
    apply_page_quota,
    build_stage_events,
    page_scope_key,
    postprocess_candidates,
    rerank,
    rrf_fuse,
)
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import AnswerabilityDecision, AnswerabilityStatus, Evidence, RetrievalResult


def test_rrf_fuses_stage_ranks() -> None:
    bm25 = [{"chunk_id": "a", "scores": {"bm25": 10.0}, "ranks": {"bm25": 1}}]
    dense = [{"chunk_id": "a", "scores": {"dense": 0.8}, "ranks": {"dense": 1}}]

    fused = rrf_fuse({"bm25": bm25, "dense": dense}, top_k=10)

    assert fused[0]["chunk_id"] == "a"
    assert fused[0]["scores"]["rrf_total"] > 0
    assert fused[0]["scores"]["fusion"] == fused[0]["scores"]["rrf_total"]
    assert fused[0]["ranks"]["bm25"] == 1
    assert fused[0]["ranks"]["dense"] == 1
    assert fused[0]["ranks"]["fusion"] == 1


def test_citation_validator_rejects_unknown_ids() -> None:
    evidence = [
        Evidence(
            evidence_id="S1",
            chunk_id="c1",
            title="Россия",
            section_path=["Россия"],
            content="Россия — государство.",
            source_url="https://ru.wikipedia.org/wiki/Россия",
        )
    ]

    assert validate_citations("Россия — государство [S1]", evidence)["valid"] is True
    result = validate_citations("Россия — государство [S2]", evidence)
    assert result["valid"] is False
    assert result["unknown"] == ["S2"]


def test_citation_validation_can_be_disabled_with_policy() -> None:
    evidence = [
        Evidence(
            evidence_id="S1",
            chunk_id="c1",
            title="Россия",
            section_path=["Россия"],
            content="Россия - государство.",
            source_url="https://ru.wikipedia.org/wiki/Россия",
        )
    ]

    result = validate_citations_with_policy("Факт [S2]", evidence, mode="off")

    assert result["valid"] is True
    assert result["status"] == "disabled_by_policy"
    assert result["citations"] == ["S2"]


def test_citation_validation_warn_records_underlying_failure() -> None:
    evidence = [
        Evidence(
            evidence_id="S1",
            chunk_id="c1",
            title="Россия",
            section_path=["Россия"],
            content="Россия - государство.",
            source_url="https://ru.wikipedia.org/wiki/Россия",
        )
    ]

    result = validate_citations_with_policy("Факт [S2]", evidence, mode="warn")

    assert result["valid"] is True
    assert result["citation_validation_valid"] is False


def test_retrieval_stage_events_include_additive_timings() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    candidate: dict[str, Any] = {
        "chunk_id": "c1",
        "title": "Россия",
        "section_path": ["Россия"],
        "content": "Россия - государство.",
        "source_url": "http://localhost/source",
        "scores": {"bm25": 1.0},
        "ranks": {"bm25": 1},
        "metadata": {},
    }

    events = build_stage_events(
        query="Россия",
        normalized_query="Россия",
        profile=profile,
        read_alias="wiki",
        result_sets={"bm25": [candidate]},
        fused=[candidate],
        reranked=[candidate],
        selected=[candidate],
        policy_events=[],
        latency_ms=15,
        timings_ms={"bm25": 3, "fusion": 1, "rerank": 4, "context": 2, "retrieval_total": 15},
        contract={"index_contract_id": "sha256:index", "run_contract_id": "sha256:run", "index_version": "index"},
    )

    profile_event = next(event for event in events if event["stage"] == "profile")
    assert profile_event["index_contract_id"] == "sha256:index"
    assert profile_event["run_contract_id"] == "sha256:run"
    assert next(event for event in events if event["stage"] == "bm25")["latency_ms"] == 3
    assert next(event for event in events if event["stage"] == "bm25")["query_context"]["subquery_id"] == "sq.primary.1"
    assert next(event for event in events if event["stage"] == "bm25")["candidates"][0]["subquery_id"] == "sq.primary.1"
    query_event = next(event for event in events if event["stage"] == "query_transform")
    assert query_event["transforms"][0]["transform_id"] == "tr.original.1"
    assert query_event["transforms"][1]["transform_id"] == "tr.normalization.1"
    assert query_event["query_refs"][0]["subquery_id"] == "sq.primary.1"
    assert [item["type"] for item in query_event["transforms"]] == [
        "original",
        "normalization",
        "rewrite",
        "decomposition",
    ]
    assert next(event for event in events if event["stage"] == "context")["latency_ms"] == 15
    assert next(event for event in events if event["stage"] == "context")["stage_latency_ms"] == 2
    assert next(event for event in events if event["stage"] == "context")["stable_stage"] == "context_selection"
    assert next(event for event in events if event["stage"] == "timings")["timings_ms"]["retrieval_total"] == 15


def test_retrieval_stage_snapshots_preserve_rank_movement() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    candidate: dict[str, Any] = {
        "chunk_id": "c1",
        "title": "Россия",
        "section_path": ["Россия"],
        "content": "Россия - государство.",
        "source_url": "http://localhost/source",
        "scores": {"bm25": 1.0, "fusion": 0.1},
        "ranks": {"bm25": 1, "fusion": 1},
        "metadata": {},
    }
    fused_snapshot = _snapshot_candidates([candidate])
    candidate["scores"]["rerank"] = 0.9
    candidate["ranks"]["rerank"] = 1

    events = build_stage_events(
        query="Россия",
        normalized_query="Россия",
        profile=profile,
        read_alias="wiki",
        result_sets={"bm25": _snapshot_candidates([candidate])},
        fused=fused_snapshot,
        reranked=[candidate],
        selected=[candidate],
        policy_events=[],
        latency_ms=15,
        timings_ms={"bm25": 3, "fusion": 1, "rerank": 4, "context": 2, "retrieval_total": 15},
    )

    rrf_candidate = next(event for event in events if event["stage"] == "rrf")["candidates"][0]
    rerank_candidate = next(event for event in events if event["stage"] == "rerank")["candidates"][0]
    assert "rerank" not in rrf_candidate["scores"]
    assert "rerank" not in rrf_candidate["ranks"]
    assert rerank_candidate["scores"]["rerank"] == 0.9
    assert rerank_candidate["ranks"]["rerank"] == 1


def test_postprocess_drops_explicit_negative_title_from_final_context() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    selected, events = postprocess_candidates(
        [
            _candidate("c1", "Россия", page_id=1),
            _candidate("c2", "Канада", page_id=2),
        ],
        profile,
        requested_top_k=10,
        query="Ответь по «Россия»; «Канада» дана только как отвлекающий контекст, не используй её.",
    )

    assert [item["title"] for item in selected] == ["Россия"]
    assert selected[0]["ranks"]["final"] == 1
    dropped = [event for event in events if event.get("reason") == "EXPLICIT_NEGATIVE_TITLE"]
    assert dropped
    assert dropped[0]["chunk_id"] == "c2"
    assert dropped[0]["negative_evidence_policy_version"] == "explicit_negative_title_v1"


def test_postprocess_keeps_quoted_title_without_negative_marker() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    selected, events = postprocess_candidates(
        [
            _candidate("c1", "Россия", page_id=1),
            _candidate("c2", "Канада", page_id=2),
        ],
        profile,
        requested_top_k=10,
        query="Сравни «Россия» и «Канада» по площади.",
    )

    assert [item["title"] for item in selected] == ["Россия", "Канада"]
    assert not [event for event in events if event.get("reason") == "EXPLICIT_NEGATIVE_TITLE"]


def test_ambiguity_context_exposes_distinct_documents_and_caps_each_document() -> None:
    profile = get_retrieval_profile("test_mock")
    candidates = [
        {**_candidate(f"country-{index}", "Россия", page_id=index), "document_id": "country"} for index in range(1, 5)
    ] + [
        {**_candidate(f"asteroid-{index}", "Россия (астероид)", page_id=index), "document_id": "asteroid"}
        for index in range(1, 5)
    ]
    selected, _events = postprocess_candidates(
        _order_ambiguity_candidates(candidates, 12, "auto"),
        profile,
        requested_top_k=12,
        query="Что такое Россия?",
        ambiguity_mode="auto",
    )

    assert len(selected) <= 12
    assert {item["document_id"] for item in selected} == {"country", "asteroid"}
    assert max(sum(item["document_id"] == document for item in selected) for document in {"country", "asteroid"}) <= 2


def test_sota_profile_limits_rerank_input_without_changing_legacy_top_k() -> None:
    profile = get_retrieval_profile("sota_mvp")
    assert profile.retrieval.rerank_input_k == 24
    assert profile.retrieval.rerank_top_k == 50
    assert profile.postprocess.final_evidence_max == 12


@pytest.mark.asyncio
async def test_rerank_gateway_input_is_capped_at_24(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[int] = []

    async def fake_gateway(_query: str, documents: list[str], *_args: object, **_kwargs: object) -> dict[str, object]:
        observed.append(len(documents))
        return {"results": [{"index": index, "relevance_score": 1.0} for index in range(len(documents))]}

    monkeypatch.setattr("wikipediarag.retrieval.gateway_rerank", fake_gateway)
    candidates = [_candidate(f"c{index}", "Россия", page_id=index) for index in range(30)]
    profile = get_retrieval_profile("test_mock")
    await rerank("Россия", candidates, Settings(), profile, top_k=50, score_all=True)

    assert observed == [24]


def test_multiple_interpretations_validate_citations_independently() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
        Evidence(
            evidence_id="S2",
            chunk_id="c2",
            title="Россия (астероид)",
            section_path=[],
            content="астероид",
            source_url="u2",
        ),
    ]
    draft = _parse_answer_draft(
        json.dumps(
            {
                "answer_markdown": "Найдено два подтверждённых значения.",
                "answer_mode": "multiple",
                "clarification_question": "О каком значении речь?",
                "interpretations": [
                    {
                        "interpretation_id": "country",
                        "label": "Страна",
                        "answer_markdown": "Россия — страна [S1]",
                        "evidence_ids": ["S1"],
                        "claims": [{"claim_id": "c-country", "text": "Страна", "evidence_ids": ["S1"], "type": "fact"}],
                    },
                    {
                        "interpretation_id": "asteroid",
                        "label": "Астероид",
                        "answer_markdown": "Россия — астероид [S2]",
                        "evidence_ids": ["S2"],
                        "claims": [
                            {"claim_id": "c-asteroid", "text": "Астероид", "evidence_ids": ["S2"], "type": "fact"}
                        ],
                    },
                ],
                "claims": [],
                "insufficient_evidence": False,
            }
        ),
        evidence,
        strict=True,
    )
    assert draft["answer_mode"] == "multiple"
    assert {item["interpretation_id"] for item in draft["interpretations"]} == {"country", "asteroid"}


def test_always_ambiguous_schema_requires_multiple_interpretations() -> None:
    schema = _answer_json_schema_for_mode(ambiguity_expected=True)["json_schema"]["schema"]
    assert schema["properties"]["answer_mode"]["enum"] == ["multiple"]
    assert schema["properties"]["interpretations"]["minItems"] == 2
    assert schema["properties"]["clarification_question"]["type"] == "string"
    assert schema["properties"]["clarification_question"]["minLength"] == 1


def test_answer_schema_binds_claims_to_current_evidence_ids() -> None:
    schema = _answer_json_schema_for_mode(ambiguity_expected=False, evidence_ids=["S2", "S1", "S1"])["json_schema"][
        "schema"
    ]
    claim_evidence = schema["properties"]["claims"]["items"]["properties"]["evidence_ids"]
    interpretation_evidence = schema["properties"]["interpretations"]["items"]["properties"]["evidence_ids"]

    assert schema["properties"]["answer_markdown"]["minLength"] == 1
    assert claim_evidence["minItems"] == 1
    assert claim_evidence["items"]["enum"] == ["S1", "S2"]
    assert interpretation_evidence["items"]["enum"] == ["S1", "S2"]


def test_answer_draft_reports_safe_contract_reason() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1")
    ]
    payload: dict[str, object] = {
        "answer_markdown": "Россия — страна [S2]",
        "answer_mode": "single",
        "interpretations": [],
        "clarification_question": None,
        "claims": [{"claim_id": "country", "text": "Страна", "evidence_ids": ["S2"], "type": "fact"}],
        "insufficient_evidence": False,
    }

    with pytest.raises(ModelOutputError) as error:
        _parse_answer_draft(json.dumps(payload), evidence, strict=True)

    assert error.value.reason == "unknown_evidence"


def test_insufficient_evidence_draft_discards_provider_citations_before_grounded_answer_validation() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
        Evidence(evidence_id="S2", chunk_id="c2", title="Москва", section_path=[], content="город", source_url="u2"),
    ]
    payload: dict[str, object] = {
        "answer_markdown": "Недостаточно данных [S1] [S2]",
        "answer_mode": "single",
        "interpretations": [],
        "clarification_question": None,
        "claims": [],
        "insufficient_evidence": True,
        "insufficient_evidence_reason": "insufficient_context",
    }

    parsed = _parse_answer_draft(json.dumps(payload), evidence, strict=True)

    assert parsed["insufficient_evidence"] is True
    assert parsed["model_output_normalizations"] == ["insufficient_evidence_abstention"]


def test_answer_draft_derives_claims_only_from_existing_cited_sentences() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
        Evidence(evidence_id="S2", chunk_id="c2", title="Москва", section_path=[], content="город", source_url="u2"),
    ]
    payload: dict[str, object] = {
        "answer_markdown": "Россия — страна [S1]. Москва — город [S2].",
        "answer_mode": "single",
        "interpretations": [],
        "clarification_question": None,
        "claims": [],
        "insufficient_evidence": False,
    }

    parsed = _parse_answer_draft(json.dumps(payload), evidence, strict=True)

    assert [claim["evidence_ids"] for claim in parsed["claims"]] == [["S1"], ["S2"]]
    assert parsed["model_output_normalizations"] == ["claims_derived_from_citations"]


def test_answer_draft_canonicalizes_duplicate_provider_claim_ids() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
        Evidence(evidence_id="S2", chunk_id="c2", title="Столица", section_path=[], content="Москва", source_url="u2"),
    ]
    payload = {
        "answer_markdown": "Россия — страна [S1]. Её столица — Москва [S2]",
        "answer_mode": "single",
        "interpretations": [],
        "clarification_question": None,
        "claims": [
            {"claim_id": "claim", "text": "Россия — страна", "evidence_ids": ["S1"], "type": "fact"},
            {"claim_id": "claim", "text": "Столица — Москва", "evidence_ids": ["S2"], "type": "fact"},
        ],
        "insufficient_evidence": False,
    }

    parsed = _parse_answer_draft(json.dumps(payload), evidence, strict=True)

    assert [claim["claim_id"] for claim in parsed["claims"]] == ["claim", "claim-1"]
    assert parsed["model_output_normalizations"] == ["duplicate_claim_id"]


def test_answer_draft_canonicalizes_incomplete_multiple_mode_without_ambiguity_requirement() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
    ]
    payload = {
        "answer_markdown": "Россия — страна [S1]",
        "answer_mode": "multiple",
        "interpretations": [],
        "clarification_question": "О каком значении речь?",
        "claims": [{"claim_id": "country", "text": "Россия — страна", "evidence_ids": ["S1"], "type": "fact"}],
        "insufficient_evidence": False,
    }

    parsed = _parse_answer_draft(json.dumps(payload), evidence, strict=True)

    assert parsed["answer_mode"] == "single"
    assert parsed["interpretations"] == []
    assert parsed["clarification_question"] is None
    assert parsed["model_output_normalizations"] == ["multiple_without_two_interpretations"]


def test_answer_draft_canonicalizes_duplicate_provider_interpretation_ids() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
        Evidence(
            evidence_id="S2",
            chunk_id="c2",
            title="Россия (астероид)",
            section_path=[],
            content="астероид",
            source_url="u2",
        ),
    ]
    payload = {
        "answer_markdown": "Есть два значения: страна [S1] и астероид [S2]",
        "answer_mode": "multiple",
        "clarification_question": "О каком значении речь?",
        "interpretations": [
            {
                "interpretation_id": "meaning",
                "label": "Страна",
                "answer_markdown": "Россия — страна [S1]",
                "evidence_ids": ["S1"],
                "claims": [{"claim_id": "country", "text": "страна", "evidence_ids": ["S1"], "type": "fact"}],
            },
            {
                "interpretation_id": "meaning",
                "label": "Астероид",
                "answer_markdown": "Россия — астероид [S2]",
                "evidence_ids": ["S2"],
                "claims": [{"claim_id": "asteroid", "text": "астероид", "evidence_ids": ["S2"], "type": "fact"}],
            },
        ],
        "claims": [],
        "insufficient_evidence": False,
    }

    parsed = _parse_answer_draft(json.dumps(payload), evidence, strict=True)

    assert [item["interpretation_id"] for item in parsed["interpretations"]] == ["meaning", "interpretation-1"]
    assert parsed["model_output_normalizations"] == ["duplicate_interpretation_id"]


def test_answer_draft_rejects_incomplete_multiple_mode_when_ambiguity_is_required() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
    ]
    payload = {
        "answer_markdown": "Россия — страна [S1]",
        "answer_mode": "multiple",
        "interpretations": [],
        "clarification_question": "О каком значении речь?",
        "claims": [{"claim_id": "country", "text": "Россия — страна", "evidence_ids": ["S1"], "type": "fact"}],
        "insufficient_evidence": False,
    }

    with pytest.raises(ModelOutputError, match="at least two interpretations"):
        _parse_answer_draft(json.dumps(payload), evidence, strict=True, ambiguity_expected=True)


def test_always_ambiguous_rejects_single_structured_answer() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
    ]
    payload = {
        "answer_markdown": "Россия — страна [S1]",
        "answer_mode": "single",
        "interpretations": [],
        "clarification_question": None,
        "claims": [{"claim_id": "country", "text": "Страна", "evidence_ids": ["S1"], "type": "fact"}],
        "insufficient_evidence": False,
    }
    with pytest.raises(ModelOutputError, match="answer_mode=multiple"):
        _parse_answer_draft(json.dumps(payload), evidence, strict=True, ambiguity_expected=True)


def test_always_ambiguous_rejects_empty_structured_interpretations_or_question() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
        Evidence(
            evidence_id="S2",
            chunk_id="c2",
            title="Россия (астероид)",
            section_path=[],
            content="астероид",
            source_url="u2",
        ),
    ]
    payload = {
        "answer_markdown": "Есть два значения [S1] [S2]",
        "answer_mode": "multiple",
        "interpretations": [],
        "clarification_question": "",
        "claims": [
            {"claim_id": "country", "text": "Страна", "evidence_ids": ["S1"], "type": "fact"},
            {"claim_id": "asteroid", "text": "Астероид", "evidence_ids": ["S2"], "type": "fact"},
        ],
        "insufficient_evidence": False,
    }
    with pytest.raises(ModelOutputError, match="at least two interpretations"):
        _parse_answer_draft(json.dumps(payload), evidence, strict=True, ambiguity_expected=True)

    payload["interpretations"] = [
        {
            "interpretation_id": "country",
            "label": "Страна",
            "answer_markdown": "Страна [S1]",
            "evidence_ids": ["S1"],
            "claims": [{"claim_id": "country-i", "text": "Страна", "evidence_ids": ["S1"], "type": "fact"}],
        },
        {
            "interpretation_id": "asteroid",
            "label": "Астероид",
            "answer_markdown": "Астероид [S2]",
            "evidence_ids": ["S2"],
            "claims": [{"claim_id": "asteroid-i", "text": "Астероид", "evidence_ids": ["S2"], "type": "fact"}],
        },
    ]
    with pytest.raises(ModelOutputError, match="clarification_question"):
        _parse_answer_draft(json.dumps(payload), evidence, strict=True, ambiguity_expected=True)


def test_always_without_ambiguity_keeps_single_answer_contract() -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
    ]
    payload = {
        "answer_markdown": "Россия — страна [S1]",
        "answer_mode": "single",
        "interpretations": [],
        "clarification_question": None,
        "claims": [{"claim_id": "country", "text": "Страна", "evidence_ids": ["S1"], "type": "fact"}],
        "insufficient_evidence": False,
    }
    parsed = _parse_answer_draft(json.dumps(payload), evidence, strict=True, ambiguity_expected=False)
    assert parsed["answer_mode"] == "single"


def test_page_quota_is_scoped_by_knowledge_base_and_document() -> None:
    candidates = [
        {**_candidate("a", "A", page_id=1), "knowledge_base_id": "kb-1", "document_id": "doc-1"},
        {**_candidate("b", "B", page_id=1), "knowledge_base_id": "kb-1", "document_id": "doc-2"},
        {**_candidate("c", "C", page_id=1), "knowledge_base_id": "kb-2", "document_id": "doc-1"},
    ]

    selected = apply_page_quota(candidates, max_per_page=1)

    assert [item["chunk_id"] for item in selected] == ["a", "b", "c"]


def test_page_quota_applies_within_one_document_page() -> None:
    candidates = [
        {**_candidate("a", "A", page_id=1), "knowledge_base_id": "kb", "document_id": "doc"},
        {**_candidate("b", "B", page_id=1), "knowledge_base_id": "kb", "document_id": "doc"},
    ]

    assert [item["chunk_id"] for item in apply_page_quota(candidates, max_per_page=1)] == ["a"]


def test_missing_page_locator_falls_back_to_chunk_identity() -> None:
    first = {"chunk_id": "c1", "knowledge_base_id": "kb", "document_id": "doc", "metadata": {}}
    second = {"chunk_id": "c2", "knowledge_base_id": "kb", "document_id": "doc", "metadata": {}}

    assert page_scope_key(first) != page_scope_key(second)


def test_token_budget_drop_does_not_consume_page_quota() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    constrained_postprocess = profile.postprocess.model_copy(update={"max_context_tokens": 256, "page_quota": 1})
    constrained = profile.model_copy(update={"postprocess": constrained_postprocess})
    first = {**_candidate("a", "A", page_id=1), "content": "word " * 300}
    second = {**_candidate("b", "B", page_id=1), "content": "short"}

    selected, events = postprocess_candidates([first, second], constrained, requested_top_k=2)

    assert [item["chunk_id"] for item in selected] == ["b"]
    assert any(event.get("reason") == "TOKEN_BUDGET" for event in events)


def test_safe_telemetry_projection_masks_content_by_default() -> None:
    settings = Settings(telemetry_content_capture="off")
    payload = {
        "query": "raw user question",
        "candidate": {
            "chunk_id": "c1",
            "document_id": "d1",
            "scores": {"bm25": 1.0},
            "content": "full document text",
            "object_key": "tenant/secret/object",
        },
    }

    safe = safe_telemetry_payload(payload, settings=settings)

    assert safe["query"]["hash"]
    assert "raw user question" not in json.dumps(safe)
    assert "full document text" not in json.dumps(safe)
    assert "tenant/secret/object" not in json.dumps(safe)
    assert safe["candidate"]["chunk_id"] == "c1"
    assert safe["candidate"]["scores"]["bm25"] == 1.0


def test_safe_telemetry_projection_masked_mode_truncates() -> None:
    settings = Settings(telemetry_content_capture="masked", telemetry_max_text_chars=8)

    safe = safe_telemetry_payload({"comment": "email user@example.test 123456789"}, settings=settings)

    assert safe["comment"]["masked_text"] == "email [R"
    assert safe["comment"]["truncated"] is True


@pytest.mark.asyncio
async def test_generate_answer_returns_generation_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = [
        Evidence(
            evidence_id="S1",
            chunk_id="c1",
            title="Россия",
            section_path=["Россия"],
            content="Россия - государство.",
            source_url="http://localhost/source",
        )
    ]
    retrieval = RetrievalResult(query="Россия", trace_id="trace", evidence=evidence, events=[])
    profile = get_retrieval_profile("test_mock", Settings())

    async def fake_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer_markdown": "Россия - государство [S1]",
                                "claims": [
                                    {
                                        "claim_id": "c1",
                                        "text": "Россия - государство",
                                        "evidence_ids": ["S1"],
                                        "type": "fact",
                                    }
                                ],
                                "insufficient_evidence": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 5},
            "provider": "mock",
        }

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fake_chat_completion)

    answer, validation = await generate_answer("Что такое Россия?", retrieval, Settings(), profile)

    assert answer == "Россия - государство [S1]"
    timings = validation["timings_ms"]
    assert isinstance(timings, dict)
    assert set(timings) >= {"generation_total", "model_chat", "answer_parse", "citation_validation"}


@pytest.mark.asyncio
async def test_generate_answer_abstains_for_undeclared_citation_without_claim_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Первый", section_path=[], content="факт", source_url="u1"),
        Evidence(evidence_id="S2", chunk_id="c2", title="Второй", section_path=[], content="факт", source_url="u2"),
    ]
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=evidence,
        events=[],
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.partial,
            confidence=0.8,
            reason="partial",
        ),
    )
    profile = get_retrieval_profile("test_mock", Settings())
    verified = False

    async def fake_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer_markdown": "Проверочный факт [S1]",
                                "answer_mode": "single",
                                "interpretations": [],
                                "clarification_question": None,
                                "claims": [
                                    {
                                        "claim_id": "wrong-link",
                                        "text": "Проверочный факт",
                                        "evidence_ids": ["S2"],
                                        "type": "fact",
                                    }
                                ],
                                "insufficient_evidence": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 5},
            "provider": "SiliconFlow",
            "id": "provider-request",
            "_gateway_metadata": {"provider": "SiliconFlow", "attempts": 1},
        }

    async def fail_claim_verification(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal verified
        verified = True
        raise AssertionError("claim verification must not run after a contract abstention")

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fake_chat_completion)
    monkeypatch.setattr("wikipediarag.answering.verify_claims", fail_claim_verification)

    answer, validation = await generate_answer("q", retrieval, Settings(), profile)

    assert "проверяемый ответ" in answer
    assert validation["status"] == "abstained"
    assert validation["model_output_contract_abstained"] is True
    assert validation["model_output_contract_reason"] == "undeclared_citation"
    assert validation["citations"] == []
    assert validation["claims"] == []
    assert validation["interpretations"] == []
    assert validation["provider"] == "SiliconFlow"
    assert validation["provider_request_id"] == "provider-request"
    assert validation["usage"] == {"total_tokens": 5}
    assert validation["answerability_status"] == AnswerabilityStatus.partial.value
    timings = validation["timings_ms"]
    assert isinstance(timings, dict)
    assert set(timings) >= {"model_chat", "answer_parse", "claim_verification", "generation_total"}
    assert timings["claim_verification"] == 0
    assert verified is False


@pytest.mark.asyncio
async def test_generate_answer_abstains_for_single_answer_with_interpretations(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
    ]
    retrieval = RetrievalResult(query="q", trace_id="trace", evidence=evidence, events=[])
    profile = get_retrieval_profile("test_mock", Settings())

    async def fake_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer_markdown": "Россия — страна [S1]",
                                "answer_mode": "single",
                                "interpretations": [
                                    {
                                        "interpretation_id": "country",
                                        "label": "Страна",
                                        "answer_markdown": "Россия — страна [S1]",
                                        "evidence_ids": ["S1"],
                                        "claims": [
                                            {
                                                "claim_id": "country",
                                                "text": "Россия — страна",
                                                "evidence_ids": ["S1"],
                                                "type": "fact",
                                            }
                                        ],
                                    }
                                ],
                                "clarification_question": None,
                                "claims": [],
                                "insufficient_evidence": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 5},
            "provider": "Venice",
        }

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fake_chat_completion)

    _answer, validation = await generate_answer("q", retrieval, Settings(), profile)

    assert validation["status"] == "abstained"
    assert validation["model_output_contract_reason"] == "answer_mode_contract"
    assert validation["answer_mode"] == "single"
    assert validation["interpretations"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "finish_reason", "error_code"),
    [
        ("not json", None, "MODEL_OUTPUT_INVALID"),
        (json.dumps({"answer_markdown": "Неполная схема"}), None, "MODEL_OUTPUT_INVALID"),
        ("{", "length", "MODEL_OUTPUT_TRUNCATED"),
    ],
)
async def test_generate_answer_keeps_malformed_and_truncated_output_terminal(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    finish_reason: str | None,
    error_code: str,
) -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
    ]
    retrieval = RetrievalResult(query="q", trace_id="trace", evidence=evidence, events=[])
    profile = get_retrieval_profile("test_mock", Settings())

    async def fake_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fake_chat_completion)

    with pytest.raises(ModelOutputError) as error:
        await generate_answer("q", retrieval, Settings(), profile)

    assert error.value.safe_code == error_code


@pytest.mark.asyncio
async def test_generate_answer_always_ambiguous_uses_one_generator_call(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = [
        Evidence(evidence_id="S1", chunk_id="c1", title="Россия", section_path=[], content="страна", source_url="u1"),
        Evidence(
            evidence_id="S2",
            chunk_id="c2",
            title="Россия (астероид)",
            section_path=[],
            content="астероид",
            source_url="u2",
        ),
    ]
    answerability = AnswerabilityDecision(
        status=AnswerabilityStatus.partial,
        confidence=0.8,
        reason="ambiguous_entity",
        signals={"ambiguous_entity": True},
    )
    retrieval = RetrievalResult(
        query="Что такое Россия?",
        trace_id="trace",
        evidence=evidence,
        events=[],
        answerability=answerability,
    )
    profile = get_retrieval_profile("test_mock", Settings(), overrides={"answer": {"ambiguity_mode": "always"}})
    calls = 0

    async def fake_chat_completion(*_args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        response_format = kwargs["response_format"]
        assert isinstance(response_format, dict)
        json_schema = response_format["json_schema"]
        assert isinstance(json_schema, dict)
        schema = json_schema["schema"]
        assert isinstance(schema, dict)
        assert schema["properties"]["interpretations"]["minItems"] == 2
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer_markdown": "Есть два значения: страна [S1] и астероид [S2]",
                                "answer_mode": "multiple",
                                "clarification_question": "О каком значении речь?",
                                "interpretations": [
                                    {
                                        "interpretation_id": "country",
                                        "label": "Страна",
                                        "answer_markdown": "Россия — страна [S1]",
                                        "evidence_ids": ["S1"],
                                        "claims": [
                                            {
                                                "claim_id": "country-claim",
                                                "text": "Страна",
                                                "evidence_ids": ["S1"],
                                                "type": "fact",
                                            }
                                        ],
                                    },
                                    {
                                        "interpretation_id": "asteroid",
                                        "label": "Астероид",
                                        "answer_markdown": "Россия — астероид [S2]",
                                        "evidence_ids": ["S2"],
                                        "claims": [
                                            {
                                                "claim_id": "asteroid-claim",
                                                "text": "Астероид",
                                                "evidence_ids": ["S2"],
                                                "type": "fact",
                                            }
                                        ],
                                    },
                                ],
                                "claims": [],
                                "insufficient_evidence": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 5},
            "provider": "mock",
        }

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fake_chat_completion)
    _answer, validation = await generate_answer("Что такое Россия?", retrieval, Settings(), profile)

    assert calls == 1
    assert validation["answer_mode"] == "multiple"
    assert validation["ambiguity_contract_status"] == "satisfied"


@pytest.mark.asyncio
async def test_generate_answer_strict_claim_verifier_blocks_unsupported_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = [
        Evidence(
            evidence_id="S1",
            chunk_id="c1",
            title="Россия",
            section_path=["Россия"],
            content="Россия - государство.",
            source_url="http://localhost/source",
        )
    ]
    retrieval = RetrievalResult(query="Россия", trace_id="trace", evidence=evidence, events=[])
    profile = get_retrieval_profile(
        "test_mock",
        Settings(),
        overrides={"answer": {"verification": {"claim_verification": "deterministic_strict"}}},
    )

    async def fake_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer_markdown": "Марс является столицей Венеры [S1]",
                                "claims": [
                                    {
                                        "claim_id": "c1",
                                        "text": "Марс является столицей Венеры",
                                        "evidence_ids": ["S1"],
                                        "type": "fact",
                                    }
                                ],
                                "insufficient_evidence": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 5},
            "provider": "mock",
        }

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fake_chat_completion)

    answer, validation = await generate_answer("Где находится Россия?", retrieval, Settings(), profile)

    assert "не прошёл claim-level проверку" in answer
    claim_verification = validation["claim_verification"]
    assert isinstance(claim_verification, dict)
    assert claim_verification["status"] == "blocked"
    assert validation["insufficient_evidence"] is True


@pytest.mark.asyncio
async def test_generate_answer_insufficient_evidence_has_no_provider_timing() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    retrieval = RetrievalResult(query="q", trace_id="trace", evidence=[], events=[], insufficient_evidence=True)

    _answer, validation = await generate_answer("q", retrieval, Settings(), profile)

    timings = validation["timings_ms"]
    assert isinstance(timings, dict)
    assert timings["model_chat"] == 0
    assert timings["generation_total"] >= 0


@pytest.mark.asyncio
async def test_generate_answer_unanswerable_gate_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=[],
        events=[],
        insufficient_evidence=True,
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.unanswerable,
            confidence=0.95,
            reason="no_evidence",
        ),
    )

    async def fail_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("provider should not be called")

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fail_chat_completion)

    _answer, validation = await generate_answer("q", retrieval, Settings(), profile)

    assert validation["answerability_status"] == "UNANSWERABLE"
    assert validation["usage"] == {}


@pytest.mark.asyncio
async def test_generate_answer_missing_fact_gate_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    evidence = [
        Evidence(
            evidence_id="S1",
            chunk_id="c1",
            title="Россия",
            section_path=["Россия"],
            content="Россия - государство в Восточной Европе и Северной Азии.",
            source_url="http://localhost/source",
            scores={"rerank": 0.94},
        )
    ]
    retrieval = RetrievalResult(
        query="Какой официальный серийный номер указан в локальном snapshot для «Россия»?",
        trace_id="trace",
        evidence=evidence,
        events=[],
        insufficient_evidence=True,
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.unanswerable,
            confidence=0.82,
            reason="answer_bearing_terms_missing",
        ),
    )

    async def fail_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("provider should not be called")

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fail_chat_completion)

    answer, validation = await generate_answer(retrieval.query, retrieval, Settings(), profile)

    assert "Недостаточно доказательств" in answer
    assert validation["answerability_status"] == "UNANSWERABLE"
    assert validation["usage"] == {}


@pytest.mark.asyncio
async def test_generate_answer_conflicting_gate_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    retrieval = RetrievalResult(
        query="q",
        trace_id="trace",
        evidence=[
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Город",
                section_path=["Город"],
                content="Население 1000.",
                source_url="http://localhost/source",
            )
        ],
        events=[],
        insufficient_evidence=True,
        answerability=AnswerabilityDecision(
            status=AnswerabilityStatus.conflicting,
            confidence=0.7,
            reason="explicit_conflict_with_divergent_values",
        ),
    )

    async def fail_chat_completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("provider should not be called")

    monkeypatch.setattr("wikipediarag.answering.chat_completion", fail_chat_completion)

    _answer, validation = await generate_answer("q", retrieval, Settings(), profile)

    assert validation["answerability_status"] == "CONFLICTING"
    assert validation["usage"] == {}


def _candidate(chunk_id: str, title: str, *, page_id: int) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": f"doc-{page_id}",
        "page_id": page_id,
        "title": title,
        "section_path": [title],
        "content": f"{title} - тестовый фрагмент.",
        "source_url": f"http://localhost/source/{page_id}",
        "scores": {"rerank": 0.9},
        "ranks": {"rerank": page_id},
        "metadata": {},
    }
