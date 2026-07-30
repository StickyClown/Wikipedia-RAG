from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from wikipediarag.eval.corpus import CorpusChunk
from wikipediarag.eval.metrics import score_retrieval_task
from wikipediarag.eval.retrieval_runner import (
    extract_search_debug_candidates,
    format_retrieval_progress,
    run_retrieval_suite,
    summarize_retrieval_config,
)
from wikipediarag.eval.schemas import (
    CandidateRef,
    EvalConfig,
    EvalDatasetManifest,
    EvalTask,
    GoldEvidence,
    RetrievalEvalStatus,
    RetrievalRunManifest,
    RetrievalTaskResult,
)


def _chunk(chunk_id: str, document_id: str, section_id: str) -> CorpusChunk:
    return CorpusChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        section_id=section_id,
        title=f"Title {chunk_id}",
        content="content",
        source_url=f"http://localhost/{chunk_id}",
        section_path=("Title",),
        parent_chunk_id=section_id,
        prev_chunk_id=None,
        next_chunk_id=None,
        metadata={},
    )


def _task(task_id: str = "t1", *, hard_negative_page_ids: list[str] | None = None) -> EvalTask:
    return EvalTask(
        task_id=task_id,
        question="Какой тестовый факт?",
        task_family="hard_negative" if hard_negative_page_ids else "single_hop_factual",
        reference_answer="Ответ",
        accepted_answers=["Ответ"],
        unanswerable=False,
        expected_mode="normal_sufficient",
        gold_page_ids=["p1"],
        gold_section_ids=["s1"],
        gold_chunk_ids=["c1"],
        gold_evidence=[
            GoldEvidence(
                evidence_id="e1",
                document_id="p1",
                section_id="s1",
                chunk_id="c1",
                quote="Ответ",
            )
        ],
        reasoning_path=["p1"],
        generator_alias="generator_main",
        verifier_alias="verifier",
        zim_checksum="sha",
        snapshot_id="snapshot",
        index_version="index",
        retrieval_profile_hash="profile",
        hard_negative_page_ids=hard_negative_page_ids or [],
    )


def _candidate(
    chunk_id: str,
    document_id: str,
    section_id: str,
    rank: int,
    *,
    scores: dict[str, float] | None = None,
) -> CandidateRef:
    return CandidateRef(
        chunk_id=chunk_id,
        document_id=document_id,
        section_id=section_id,
        title=f"Title {chunk_id}",
        source_url=f"http://localhost/{chunk_id}",
        rank=rank,
        stage="context",
        scores=scores or {},
    )


@pytest.mark.asyncio
async def test_extract_search_debug_candidates_uses_stage_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_refs(chunk_ids: list[str], **_kwargs: Any) -> dict[str, CorpusChunk]:
        assert sorted(chunk_ids) == ["c1", "c1", "c2"]
        return {"c1": _chunk("c1", "p1", "s1"), "c2": _chunk("c2", "p2", "s2"), "c3": _chunk("c3", "p3", "s3")}

    monkeypatch.setattr("wikipediarag.eval.retrieval_runner.load_chunk_refs", fake_refs)
    payload = {
        "events": [
            {"stage": "bm25", "candidates": [{"chunk_id": "c3"}]},
            {"stage": "rrf", "candidates": [{"chunk_id": "c2"}]},
            {"stage": "rerank", "candidates": [{"chunk_id": "c1"}]},
            {"stage": "context", "latency_ms": 5, "candidates": [{"chunk_id": "c1"}]},
        ]
    }

    prefusion, reranked, final = await extract_search_debug_candidates(payload)

    assert [item.chunk_id for item in prefusion] == ["c2"]
    assert [item.chunk_id for item in reranked] == ["c1"]
    assert final[0].document_id == "p1"
    assert final[0].section_id == "s1"


def test_hard_negative_metrics_track_rank_margin() -> None:
    task = _task(hard_negative_page_ids=["p2"])
    scores = score_retrieval_task(
        task,
        final=[_candidate("c2", "p2", "s2", 1), _candidate("c1", "p1", "s1", 2)],
        reranked=[_candidate("c2", "p2", "s2", 1), _candidate("c1", "p1", "s1", 2)],
        prefusion=[_candidate("c1", "p1", "s1", 1), _candidate("c2", "p2", "s2", 2)],
    )

    assert scores.hard_negative_page_hit_at_10 == 1.0
    assert scores.false_positive_evidence_rate == 0.5
    assert scores.dangerous_false_positive_evidence_rate == 0.5
    assert scores.gold_vs_hard_negative_rank_margin == -1.0
    assert scores.reranker_gold_delta == -1.0


def test_low_score_rank4_hard_negative_is_diagnostic_only() -> None:
    task = _task(hard_negative_page_ids=["p2"])
    scores = score_retrieval_task(
        task,
        final=[
            _candidate("c1", "p1", "s1", 1, scores={"rerank": 0.866}),
            _candidate("c3", "p3", "s3", 2, scores={"rerank": 0.4}),
            _candidate("c4", "p4", "s4", 3, scores={"rerank": 0.3}),
            _candidate("c2", "p2", "s2", 4, scores={"rerank": 0.177}),
        ],
        reranked=[
            _candidate("c1", "p1", "s1", 1, scores={"rerank": 0.866}),
            _candidate("c2", "p2", "s2", 4, scores={"rerank": 0.177}),
        ],
        prefusion=[_candidate("c1", "p1", "s1", 1), _candidate("c2", "p2", "s2", 2)],
    )

    assert scores.false_positive_evidence_rate == 0.25
    assert scores.dangerous_false_positive_evidence_rate == 0.0


def test_retrieval_summary_excludes_answer_citation_token_metrics() -> None:
    task = _task()
    scores = score_retrieval_task(
        task,
        final=[_candidate("c1", "p1", "s1", 1)],
        reranked=[_candidate("c1", "p1", "s1", 1)],
        prefusion=[_candidate("c1", "p1", "s1", 1)],
    )
    config = EvalConfig(
        config_id="bm25_only",
        retrieval_profile="sota_mvp",
        retrieval_overrides={},
        config_hash="hash",
    )
    result = RetrievalTaskResult(
        task_id=task.task_id,
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="completed",
        question=task.question,
        task_family=task.task_family,
        unanswerable=False,
        batch_index=1,
        task_index=1,
        final_candidates=[_candidate("c1", "p1", "s1", 1)],
        latency_ms={"total": 10, "retrieval": 7, "bm25": 2, "rerank": 4},
        scores=scores,
    )

    summary = summarize_retrieval_config(config, [task], [result])

    assert summary.metrics["chunk_recall_at_20"] == 1.0
    assert "citation_precision" not in summary.metrics
    assert "tokens" not in summary.metrics
    assert "exact_match" not in summary.metrics
    assert summary.metrics["stage_latency_bm25_p95_ms"] == 2.0
    assert summary.metrics["stage_latency_rerank_p50_ms"] == 4.0


def test_format_retrieval_progress_includes_current_position_and_eta() -> None:
    status = RetrievalEvalStatus(
        run_id="run",
        state="running",
        phase="config_running",
        suite="generated-wikipedia-v1",
        dataset_hash="dataset",
        dataset_path="tasks.jsonl",
        run_dir="artifacts/eval/retrieval-runs/generated-wikipedia-v1/run",
        batch_size=10,
        total_configs=2,
        supported_configs=2,
        total_tasks=150,
        total_task_runs=300,
        processed_task_runs=12,
        completed_task_runs=12,
        current_config_id="bm25_only",
        current_config_index=1,
        current_task_id="priv-wiki-000012",
        current_task_index=12,
        current_batch=2,
        total_batches=15,
        elapsed_seconds=60,
        eta_seconds=1440,
        avg_seconds_per_task=5.0,
        last_latency_ms=123,
        started_at="2026-07-26T00:00:00Z",
        updated_at="2026-07-26T00:01:00Z",
    )

    rendered = format_retrieval_progress(status, "task_completed")

    assert "config=bm25_only" in rendered
    assert "batch=2/15" in rendered
    assert "task=12/150" in rendered
    assert "task_id=priv-wiki-000012" in rendered
    assert "eta=00:24:00" in rendered


@pytest.mark.asyncio
async def test_retrieval_runner_batch_size_is_bounded_in_flight_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    class SlowRetrievalClient:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.in_flight = 0
            self.max_in_flight = 0
            self.started: dict[str, float] = {}
            self.finished: dict[str, float] = {}

        def run_search_debug(
            self,
            question: str,
            *,
            api: str,
            top_k: int,
            retrieval_profile: str,
            retrieval_overrides: dict[str, Any],
        ) -> dict[str, Any]:
            del api, top_k, retrieval_profile, retrieval_overrides
            with self.lock:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                self.started[question] = time.perf_counter()
            time.sleep(0.2 if question == "q1" else 0.05)
            with self.lock:
                self.finished[question] = time.perf_counter()
                self.in_flight -= 1
            return {
                "query": question,
                "trace_id": f"trace-{question}",
                "index_contract_id": "sha256:index",
                "run_contract_id": "sha256:run",
                "events": [
                    {"stage": "rrf", "candidates": [{"chunk_id": "c1"}]},
                    {"stage": "context", "latency_ms": 9, "candidates": [{"chunk_id": "c1"}]},
                    {
                        "stage": "timings",
                        "timings_ms": {"retrieval_total": 9, "bm25": 2, "context": 1},
                    },
                ],
            }

    async def fake_refs(chunk_ids: list[str], **_kwargs: object) -> dict[str, CorpusChunk]:
        assert sorted(set(chunk_ids)) == ["c1"]
        return {"c1": _chunk("c1", "p1", "s1")}

    config = EvalConfig(
        config_id="bm25_only",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"retrieval": {"bm25": True}},
        config_hash="hash-a",
    )
    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="fixture-hash",
        task_count=4,
        created_at="2026-07-27T00:00:00Z",
        snapshot_id="snapshot",
        index_version="index",
        zim_checksum="sha",
        retrieval_profile_hash="profile",
        generator_alias="generator_main",
        verifier_alias="verifier",
        jsonl_path=str(tmp_path / "tasks.jsonl"),
    )
    tasks = [_task(f"t{index}") for index in range(1, 5)]
    for index, task in enumerate(tasks, start=1):
        task.question = f"q{index}"
    client = SlowRetrievalClient()

    monkeypatch.setattr("wikipediarag.eval.retrieval_runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.retrieval_runner.eval_configs", lambda settings=None: [config])
    monkeypatch.setattr("wikipediarag.eval.retrieval_runner.ARTIFACT_ROOT", tmp_path / "eval")

    run = await run_retrieval_suite(
        manifest,
        tasks,
        api="http://api",
        batch_size=2,
        run_id="bounded",
        client=client,
    )

    assert isinstance(run, RetrievalRunManifest)
    assert client.max_in_flight == 2
    assert client.started["q3"] < client.finished["q1"]
    assert run.config_summaries[0].contract_ids["index_contract_id"] == ["sha256:index"]
    assert run.config_summaries[0].contract_ids["run_contract_id"] == ["sha256:run"]
    assert run.config_summaries[0].metrics["stage_latency_retrieval_total_p95_ms"] == 9.0
