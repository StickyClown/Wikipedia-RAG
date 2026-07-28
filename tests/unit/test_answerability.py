from __future__ import annotations

from wikipediarag.answerability import decide_answerability
from wikipediarag.config import Settings
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import AnswerabilityStatus, Evidence


def test_answerability_gate_marks_exact_title_answerable() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability(
        "Что такое Россия?",
        [
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Россия",
                section_path=["Россия"],
                content="Россия - государство в Восточной Европе и Северной Азии.",
                source_url="http://localhost/source",
                scores={"rerank": 0.9},
            )
        ],
        profile,
    )

    assert decision.status == AnswerabilityStatus.answerable
    assert decision.signals["exact_title_match"] is True


def test_answerability_gate_marks_comparison_partial_when_one_part_missing() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability(
        "Сравни Россию и Канаду по площади",
        [
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Россия",
                section_path=["Россия"],
                content="Россия имеет большую площадь.",
                source_url="http://localhost/source",
                scores={"rerank": 0.8},
            )
        ],
        profile,
    )

    assert decision.status == AnswerabilityStatus.partial
    assert any("Россию" in part for part in decision.covered_parts)
    assert any("Канаду" in part for part in decision.missing_parts)


def test_answerability_gate_marks_empty_context_unanswerable() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability("Кто написал несуществующую статью?", [], profile)

    assert decision.status == AnswerabilityStatus.unanswerable
    assert decision.reason == "no_evidence"


def test_answerability_gate_marks_explicit_conflict_with_divergent_values() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability(
        "Есть конфликт в данных о населении города?",
        [
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Город",
                section_path=["Город"],
                content="Население города составляет 1000 человек.",
                source_url="http://localhost/source/1",
                scores={"rerank": 0.9},
            ),
            Evidence(
                evidence_id="S2",
                chunk_id="c2",
                title="Город",
                section_path=["Город"],
                content="Население города составляет 2000 человек.",
                source_url="http://localhost/source/2",
                scores={"rerank": 0.8},
            ),
        ],
        profile,
    )

    assert decision.status == AnswerabilityStatus.conflicting
    assert decision.signals["conflict_marker"] is True
