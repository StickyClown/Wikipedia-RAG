from __future__ import annotations

from wikipediarag.eval.metrics import citation_scores, exact_match, ndcg_at, score_task, token_f1
from wikipediarag.eval.schemas import CandidateRef, EvalTask, GoldEvidence


def _task(unanswerable: bool = False) -> EvalTask:
    return EvalTask(
        task_id="t1",
        question="Что такое тест?",
        task_family="unanswerable" if unanswerable else "single_hop_factual",
        reference_answer="Тестовый ответ",
        accepted_answers=["Тестовый ответ"],
        unanswerable=unanswerable,
        expected_mode="unanswerable" if unanswerable else "normal_sufficient",
        gold_page_ids=[] if unanswerable else ["p1"],
        gold_section_ids=[] if unanswerable else ["s1"],
        gold_chunk_ids=[] if unanswerable else ["c1"],
        gold_evidence=[]
        if unanswerable
        else [
            GoldEvidence(
                evidence_id="e1",
                document_id="p1",
                section_id="s1",
                chunk_id="c1",
                quote="Тестовый ответ.",
                source_url="http://localhost/source",
            )
        ],
        reasoning_path=[] if unanswerable else ["p1"],
        generator_alias="generator_main",
        verifier_alias="verifier",
        zim_checksum="sha",
        snapshot_id="snapshot",
        index_version="index",
        retrieval_profile_hash="profile",
    )


def test_retrieval_metrics_match_page_section_and_chunk_gold() -> None:
    candidates = [
        CandidateRef(chunk_id="c2", document_id="p2", section_id="s2", rank=1, stage="rerank"),
        CandidateRef(chunk_id="c1", document_id="p1", section_id="s1", rank=2, stage="rerank"),
    ]

    scores = score_task(
        _task(),
        answer="Тестовый ответ [S1]",
        reranked=candidates,
        prefusion=list(reversed(candidates)),
        cited_chunk_ids=["c1"],
        kiwix_url_ok=True,
    )

    assert scores.page_recall["1"] == 0.0
    assert scores.page_recall["5"] == 1.0
    assert scores.section_recall["5"] == 1.0
    assert scores.chunk_recall["5"] == 1.0
    assert scores.mrr_at_10 == 0.5
    assert scores.path_completion == 1.0
    assert scores.reranker_gold_delta == -1.0


def test_ndcg_em_f1_and_citation_scores() -> None:
    assert ndcg_at(["bad", "gold"], {"gold"}, 10) > 0
    assert exact_match("Тестовый ответ!", ["тестовый ответ"]) == 1.0
    assert token_f1("Тестовый подробный ответ", ["тестовый ответ"]) > 0.7

    precision, recall, unsupported = citation_scores(["c1", "bad"], {"c1"}, unanswerable=False)

    assert precision == 0.5
    assert recall == 1.0
    assert unsupported == 0.5


def test_unanswerable_accuracy_uses_refusal_markers() -> None:
    scores = score_task(
        _task(unanswerable=True),
        answer="Недостаточно доказательств в локальной базе.",
        reranked=[],
        prefusion=[],
        cited_chunk_ids=[],
        kiwix_url_ok=True,
    )

    assert scores.unanswerable_accuracy == 1.0
    assert scores.citation_precision == 1.0
