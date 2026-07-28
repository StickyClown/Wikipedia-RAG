from __future__ import annotations

import pytest

from wikipediarag.config import Settings
from wikipediarag.extended import HarnessState, run_extended_search, should_start_extended
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import Evidence, RetrievalResult


def test_extended_classifier_keeps_simple_query_direct() -> None:
    assert should_start_extended("Что такое Россия?") is False


def test_extended_classifier_routes_comparison() -> None:
    assert should_start_extended("Сравни Россию и Канаду по площади") is True


def test_harness_state_tracks_duplicate_tool_hashes() -> None:
    state = HarnessState(original_query="q", intent="direct", retrieval_profile="test_mock")

    state.tool_call_hashes.append("abc")

    assert state.tool_call_hashes == ["abc"]


@pytest.mark.asyncio
async def test_extended_search_records_tool_and_total_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            return None

    async def fake_retrieve(*_args: object, **_kwargs: object) -> RetrievalResult:
        return RetrievalResult(
            query="subquery",
            trace_id="trace",
            evidence=[
                Evidence(
                    evidence_id="S1",
                    chunk_id="c1",
                    title="Россия",
                    section_path=["Россия"],
                    content="Россия - государство.",
                    source_url="http://localhost/source",
                )
            ],
            events=[{"stage": "timings", "timings_ms": {"retrieval_total": 3, "bm25": 1}}],
        )

    monkeypatch.setattr("wikipediarag.extended.retrieve", fake_retrieve)
    profile = get_retrieval_profile("test_mock", Settings())

    result = await run_extended_search(
        FakeConnection(),  # type: ignore[arg-type]
        "Сравни Россию и Канаду?",
        tenant_id="tenant",
        knowledge_base_id="kb",
        query_run_id="run",
        trace_id="trace",
        settings=Settings(),
        profile=profile,
    )

    tool_event = next(event for event in result.events if event["stage"] == "harness_tool")
    harness_event = next(event for event in result.events if event["stage"] == "harness")
    assert tool_event["latency_ms"] >= 0
    assert tool_event["retrieval_timings_ms"] == {"retrieval_total": 3, "bm25": 1}
    assert harness_event["timings_ms"]["extended_search_total"] >= 0
