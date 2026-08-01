from __future__ import annotations

import json
from typing import Any

import pytest

from wikipediarag.answering import generate_answer, validate_citations, validate_citations_with_policy
from wikipediarag.config import Settings
from wikipediarag.observability import safe_telemetry_payload
from wikipediarag.retrieval import _snapshot_candidates, build_stage_events, postprocess_candidates, rrf_fuse
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
