from __future__ import annotations

from wikipediarag.answering import validate_citations
from wikipediarag.retrieval import rrf_fuse
from wikipediarag.schemas import Evidence


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
