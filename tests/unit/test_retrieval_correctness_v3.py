from __future__ import annotations

from wikipediarag.answerability import decide_answerability
from wikipediarag.config import Settings
from wikipediarag.eval.metrics import recall_at
from wikipediarag.ids import scoped_id
from wikipediarag.retrieval import _candidate_debug, postprocess_candidates, rrf_fuse
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import AnswerabilityStatus, Evidence


def _candidate(chunk_id: str, *, parent: str | None = None) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "knowledge_base_id": "kb",
        "document_id": "doc",
        "page_id": 1,
        "title": "Document",
        "section_path": ["Section"],
        "content": "answer text",
        "source_url": "https://example.invalid/doc",
        "scores": {"rerank": 0.9},
        "ranks": {"rerank": 1},
        "metadata": {"parent_text": "answer text"} | ({"parent_chunk_id": parent} if parent else {}),
    }


def test_scoped_ids_are_isolated_for_same_snapshot_and_native_id() -> None:
    left = scoped_id(
        "wiki-document", 42, tenant_id="tenant-a", knowledge_base_id="kb-a", source_type="zim", snapshot_id="s"
    )
    right = scoped_id(
        "wiki-document", 42, tenant_id="tenant-b", knowledge_base_id="kb-b", source_type="zim", snapshot_id="s"
    )
    assert left != right


def test_parent_expansion_deduplicates_content_unit_and_preserves_supporting_chunks() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    selected, _events = postprocess_candidates([_candidate("c1", parent="p"), _candidate("c2", parent="p")], profile, 4)
    assert len(selected) == 1
    assert selected[0]["metadata"]["supporting_chunk_ids"] == ["c1", "c2"]


def test_unprompted_divergent_values_are_partial_not_conflicting() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability(
        "Каков возраст звезды 12 Гидры?",
        [
            Evidence(
                evidence_id="S1",
                chunk_id="a",
                title="12 Гидры",
                section_path=[],
                content="Возраст 398 млн лет.",
                source_url="x",
            ),
            Evidence(
                evidence_id="S2",
                chunk_id="b",
                title="12 Гидры",
                section_path=[],
                content="Возраст 910 млн лет.",
                source_url="y",
            ),
        ],
        profile,
    )
    assert decision.status == AnswerabilityStatus.partial


def test_debug_projection_never_contains_parent_text() -> None:
    projected = _candidate_debug(_candidate("c1"))
    assert "parent_text" not in str(projected)
    assert "content" not in projected


def test_rrf_ties_are_stable_and_recall_is_fractional() -> None:
    left = [{"chunk_id": "b", "knowledge_base_id": "kb", "scores": {}, "ranks": {}}]
    right = [{"chunk_id": "a", "knowledge_base_id": "kb", "scores": {}, "ranks": {}}]
    assert [item["chunk_id"] for item in rrf_fuse({"bm25": left, "dense": right}, 2)] == ["a", "b"]
    assert recall_at(["gold-a"], {"gold-a", "gold-b"}, 10) == 0.5
