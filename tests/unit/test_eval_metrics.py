from __future__ import annotations

from wikipediarag.eval.metrics import citation_scores, exact_match, is_no_answer, ndcg_at, score_task, token_f1
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


def _hard_negative_task() -> EvalTask:
    task = _task()
    return task.model_copy(update={"task_family": "hard_negative", "hard_negative_page_ids": ["p2"]})


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


def test_soft_unanswerable_with_context_citations_is_supported() -> None:
    task = _task(unanswerable=True).model_copy(update={"hard_negative_page_ids": ["p2"]})
    candidates = [CandidateRef(chunk_id="c2", document_id="p2", section_id="s2", rank=1, stage="rerank")]
    scores = score_task(
        task,
        answer=(
            "В предоставленных источниках информация о конкретном командире отсутствует. "
            "Близко известно только, что U-1009 входила в список лодок флотилии [S1]."
        ),
        reranked=candidates,
        prefusion=candidates,
        cited_chunk_ids=["c2"],
        kiwix_url_ok=True,
    )

    assert is_no_answer("В источниках отсутствует информация о командире.")
    assert is_no_answer("В источниках информация о командире отсутствует.")
    assert scores.unanswerable_accuracy == 1.0
    assert scores.soft_unanswerable_context_rate == 1.0
    assert scores.citation_precision == 1.0
    assert scores.unsupported_claim_rate == 0.0
    assert scores.cited_hard_negative_rate == 0.0


def test_answer_citing_hard_negative_is_tracked() -> None:
    candidates = [
        CandidateRef(chunk_id="c1", document_id="p1", section_id="s1", rank=1, stage="rerank"),
        CandidateRef(chunk_id="c2", document_id="p2", section_id="s2", rank=2, stage="rerank"),
    ]

    scores = score_task(
        _hard_negative_task(),
        answer="Ответ [S2]",
        reranked=candidates,
        prefusion=candidates,
        cited_chunk_ids=["c2"],
        kiwix_url_ok=True,
    )

    assert scores.cited_hard_negative_rate == 1.0


def test_answer_with_gold_and_bridge_context_citations_stays_supported() -> None:
    task = _task().model_copy(
        update={
            "gold_page_ids": ["p1", "p2"],
            "gold_section_ids": ["s1", "s2"],
            "gold_chunk_ids": ["c1", "c2"],
        }
    )
    candidates = [
        CandidateRef(chunk_id="c1", document_id="p1", section_id="s1", title="1040 (фильм)", rank=1, stage="rerank"),
        CandidateRef(
            chunk_id="c3",
            document_id="p3",
            section_id="s3",
            title="104 (значения)",
            rank=2,
            stage="rerank",
        ),
        CandidateRef(
            chunk_id="c2",
            document_id="p2",
            section_id="s2",
            title="104 (серия жилых домов)",
            rank=3,
            stage="rerank",
        ),
    ]

    scores = score_task(
        task,
        answer="Фильм называется «1040» [S1], число 104 указывает на серию [S2], город — Рига [S3].",
        reranked=candidates,
        prefusion=candidates,
        cited_chunk_ids=["c1", "c3", "c2"],
        kiwix_url_ok=True,
    )

    assert scores.citation_recall == 1.0
    assert scores.citation_precision == 1.0
    assert scores.unsupported_claim_rate == 0.0
