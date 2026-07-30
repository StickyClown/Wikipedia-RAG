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
    assert decision.version == "answerability_gate_v4"


def test_answerability_gate_marks_missing_requested_fact_partial_not_hard_refusal() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability(
        "Какой официальный серийный номер указан в локальном snapshot для «Россия»?",
        [
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="Россия",
                section_path=["Россия"],
                content="Россия - государство в Восточной Европе и Северной Азии.",
                source_url="http://localhost/source",
                scores={"rerank": 0.94},
            )
        ],
        profile,
    )

    assert decision.status == AnswerabilityStatus.partial
    assert decision.reason == "answer_bearing_terms_missing_partial"
    assert "серийный" in decision.signals["missing_answer_bearing_terms"]


def test_answerability_gate_does_not_split_date_question_on_and_or_stop_generation() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability(
        "Кто является первооткрывателем астероида (358) Аполлония и в каком году это произошло?",
        [
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="(358) Аполлония",
                section_path=["(358) Аполлония"],
                content="Первооткрыватель Огюст Шарлуа. Дата обнаружения 8 марта 1893.",
                source_url="http://localhost/source",
                scores={"rerank": 0.94},
            )
        ],
        profile,
    )

    assert decision.status != AnswerabilityStatus.unanswerable
    assert decision.reason != "answer_bearing_terms_missing"
    assert decision.signals["required_part_count"] == 1


def test_answerability_gate_does_not_split_comparison_attributes_on_and() -> None:
    profile = get_retrieval_profile("test_mock", Settings())
    decision = decide_answerability(
        (
            "Сравните релизы «15» группы «Урфин Джюс» и «10» группы «Звери» "
            "по типу формата записи и году выпуска. Укажите конкретные значения для каждого альбома."
        ),
        [
            Evidence(
                evidence_id="S1",
                chunk_id="c1",
                title="15 (альбом «Урфина Джюса»)",
                section_path=["15 (альбом «Урфина Джюса»)"],
                content="15 — второй студийный альбом группы «Урфин Джюс». Дата выпуска 1982.",
                source_url="http://localhost/source/15",
                scores={"rerank": 0.72},
            ),
            Evidence(
                evidence_id="S2",
                chunk_id="c2",
                title="10 (альбом группы «Звери»)",
                section_path=["10 (альбом группы «Звери»)"],
                content="10 — третий мини-альбом группы «Звери». Дата выпуска 26 октября 2018.",
                source_url="http://localhost/source/10",
                scores={"rerank": 0.68},
            ),
        ],
        profile,
    )

    assert decision.status == AnswerabilityStatus.answerable
    assert decision.signals["required_part_count"] == 1


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
