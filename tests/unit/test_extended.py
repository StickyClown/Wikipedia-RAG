from __future__ import annotations

import pytest

from wikipediarag.config import Settings
from wikipediarag.extended import (
    HarnessState,
    _build_subqueries,
    get_neighbors,
    run_extended_search,
    should_start_extended,
)
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import Evidence, RetrievalResult


def test_extended_classifier_keeps_simple_query_direct() -> None:
    assert should_start_extended("Что такое Россия?") is False


def test_extended_classifier_routes_comparison() -> None:
    assert should_start_extended("Сравни Россию и Канаду по площади") is True


def test_extended_classifier_routes_bridge_question_and_builds_subqueries() -> None:
    query = (
        "В каком городе можно встретить жилые здания серии, название которой начинается с тех же цифр, "
        "что и название документального фильма, вышедшего на экраны в марте 2010 года?"
    )

    assert should_start_extended(query) is True
    subqueries = _build_subqueries(query, 6)
    normalized = " | ".join(subqueries).casefold()
    assert "документальный фильм марте 2010" in normalized
    assert "серия жилых домов" in normalized


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

    async def fake_fetch_chunk_by_id(*_args: object, **_kwargs: object) -> dict[str, object] | None:
        return None

    monkeypatch.setattr("wikipediarag.extended.fetch_chunk_by_id", fake_fetch_chunk_by_id)
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

    tool_event = next(
        event for event in result.events if event["stage"] == "harness_tool" and event["tool"] == "search"
    )
    harness_event = next(event for event in result.events if event["stage"] == "harness")
    assert tool_event["latency_ms"] >= 0
    assert tool_event["retrieval_timings_ms"] == {"retrieval_total": 3, "bm25": 1}
    assert harness_event["timings_ms"]["extended_search_total"] >= 0


@pytest.mark.asyncio
async def test_extended_bridge_context_keeps_film_and_series_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            return None

    async def fake_retrieve(_conn: object, query: str, **_kwargs: object) -> RetrievalResult:
        if "серия жилых домов" in query:
            evidence = [
                Evidence(
                    evidence_id="S1",
                    chunk_id="series",
                    title="104 (серия жилых домов)",
                    section_path=["104 (серия жилых домов)"],
                    content="104 серия жилых домов встречается в Риге.",
                    source_url="http://localhost/series",
                    scores={"rerank": 0.9},
                )
            ]
        else:
            evidence = [
                Evidence(
                    evidence_id="S1",
                    chunk_id="film",
                    title="1040 (фильм)",
                    section_path=["1040 (фильм)"],
                    content="1040 - документальный фильм. Год 12 марта 2010.",
                    source_url="http://localhost/film",
                    scores={"rerank": 0.9},
                )
            ]
        return RetrievalResult(query=query, trace_id="trace", evidence=evidence, events=[])

    monkeypatch.setattr("wikipediarag.extended.retrieve", fake_retrieve)

    async def fake_fetch_chunk_by_id(*_args: object, **_kwargs: object) -> dict[str, object] | None:
        return None

    monkeypatch.setattr("wikipediarag.extended.fetch_chunk_by_id", fake_fetch_chunk_by_id)
    profile = get_retrieval_profile("test_mock", Settings(), overrides={"postprocess": {"extended_search": "always"}})

    result = await run_extended_search(
        FakeConnection(),  # type: ignore[arg-type]
        (
            "В каком городе можно встретить жилые здания серии, название которой начинается с тех же цифр, "
            "что и название документального фильма, вышедшего на экраны в марте 2010 года?"
        ),
        tenant_id="tenant",
        knowledge_base_id="kb",
        query_run_id="run",
        trace_id="trace",
        settings=Settings(),
        profile=profile,
    )

    assert [item.title for item in result.evidence[:2]] == ["1040 (фильм)", "104 (серия жилых домов)"]


@pytest.mark.asyncio
async def test_get_neighbors_uses_tenant_scoped_chunk_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    rows = {
        "c1": {
            "id": "c1",
            "title": "Center",
            "section_path": ["Doc"],
            "content": "center",
            "source_url": "http://source/center",
            "page_id": 1,
            "document_id": "doc",
            "parent_chunk_id": "p",
            "prev_chunk_id": "c0",
            "next_chunk_id": "c2",
            "metadata": {},
        },
        "c0": {
            "id": "c0",
            "title": "Prev",
            "section_path": ["Doc"],
            "content": "prev",
            "source_url": "http://source/prev",
            "page_id": 1,
            "document_id": "doc",
            "parent_chunk_id": "p",
            "prev_chunk_id": None,
            "next_chunk_id": "c1",
            "metadata": {},
        },
        "c2": {
            "id": "c2",
            "title": "Next",
            "section_path": ["Doc"],
            "content": "next",
            "source_url": "http://source/next",
            "page_id": 1,
            "document_id": "doc",
            "parent_chunk_id": "p",
            "prev_chunk_id": "c1",
            "next_chunk_id": None,
            "metadata": {},
        },
    }

    async def fake_fetch_chunk_by_id(_conn: object, **kwargs: object) -> dict[str, object] | None:
        calls.append(kwargs)
        return rows.get(str(kwargs["chunk_id"]))

    monkeypatch.setattr("wikipediarag.extended.fetch_chunk_by_id", fake_fetch_chunk_by_id)

    neighbors = await get_neighbors(
        object(),  # type: ignore[arg-type]
        "c1",
        tenant_id="tenant",
        knowledge_base_id="kb",
        window=1,
    )

    assert [item.chunk_id for item in neighbors] == ["c0", "c2"]
    assert all(call["tenant_id"] == "tenant" and call["knowledge_base_id"] == "kb" for call in calls)
