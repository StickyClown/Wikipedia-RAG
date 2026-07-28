from __future__ import annotations

import json

import pytest

from wikipediarag.answering import generate_answer, validate_citations
from wikipediarag.config import Settings
from wikipediarag.retrieval import build_stage_events, rrf_fuse
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import AnswerabilityDecision, AnswerabilityStatus, Evidence, RetrievalResult


def test_rrf_fuses_stage_ranks() -> None:
    bm25 = [{"chunk_id": "a", "scores": {"bm25": 10.0}, "ranks": {"bm25": 1}}]
    dense = [{"chunk_id": "a", "scores": {"dense": 0.8}, "ranks": {"dense": 1}}]

    fused = rrf_fuse({"bm25": bm25, "dense": dense}, top_k=10)

    assert fused[0]["chunk_id"] == "a"
    assert fused[0]["scores"]["rrf_total"] > 0
    assert fused[0]["ranks"]["bm25"] == 1
    assert fused[0]["ranks"]["dense"] == 1


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


def test_retrieval_stage_events_include_additive_timings() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    candidate = {
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
    assert next(event for event in events if event["stage"] == "context")["latency_ms"] == 15
    assert next(event for event in events if event["stage"] == "context")["stage_latency_ms"] == 2
    assert next(event for event in events if event["stage"] == "timings")["timings_ms"]["retrieval_total"] == 15


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
