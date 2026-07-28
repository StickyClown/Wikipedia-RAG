from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wikipediarag.eval.corpus import CorpusChunk
from wikipediarag.eval.retrieval_reporting import write_retrieval_report
from wikipediarag.eval.retrieval_runner import (
    load_latest_retrieval_status,
    run_retrieval_suite,
)
from wikipediarag.eval.schemas import EvalConfig, EvalDatasetManifest, EvalTask, GoldEvidence


class FakeRetrievalClient:
    def __init__(self) -> None:
        self.calls = 0

    def run_search_debug(
        self,
        question: str,
        *,
        api: str,
        top_k: int,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        assert api == "http://api"
        assert top_k == 20
        return {
            "query": question,
            "trace_id": f"trace-{self.calls}",
            "evidence": [{"chunk_id": "c1"}],
            "events": [
                {"stage": "rrf", "candidates": [{"chunk_id": "c1", "scores": {"rrf_total": 1.0}}]},
                {"stage": "rerank", "candidates": [{"chunk_id": "c1", "scores": {"rerank": 1.0}}]},
                {"stage": "context", "latency_ms": 9, "candidates": [{"chunk_id": "c1"}]},
                {"stage": "timings", "timings_ms": {"retrieval_total": 9, "bm25": 2, "rerank": 3}},
            ],
        }


def _task(task_id: str) -> EvalTask:
    return EvalTask(
        task_id=task_id,
        question="Что такое тест?",
        task_family="single_hop_factual",
        reference_answer="Тестовый ответ",
        accepted_answers=["Тестовый ответ"],
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
                quote="Тестовый ответ",
            )
        ],
        reasoning_path=["p1"],
        generator_alias="generator_main",
        verifier_alias="verifier",
        zim_checksum="sha",
        snapshot_id="snapshot",
        index_version="index",
        retrieval_profile_hash="profile",
    )


@pytest.mark.asyncio
async def test_retrieval_runner_keeps_configs_separate_resumes_and_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_refs(chunk_ids: list[str], **_kwargs: object) -> dict[str, CorpusChunk]:
        assert sorted(set(chunk_ids)) == ["c1"]
        return {
            "c1": CorpusChunk(
                chunk_id="c1",
                document_id="p1",
                section_id="s1",
                title="Статья",
                content="Тестовый ответ",
                source_url="http://localhost/source",
                section_path=("Статья",),
                parent_chunk_id="s1",
                prev_chunk_id=None,
                next_chunk_id=None,
                metadata={},
            )
        }

    configs = [
        EvalConfig(
            config_id="bm25_only",
            retrieval_profile="sota_mvp",
            retrieval_overrides={"retrieval": {"bm25": True, "dense": False}},
            config_hash="hash-a",
        ),
        EvalConfig(
            config_id="dense_only",
            retrieval_profile="sota_mvp",
            retrieval_overrides={"retrieval": {"bm25": False, "dense": True}},
            config_hash="hash-b",
        ),
        EvalConfig(
            config_id="sota_mvp_conditional_harness",
            retrieval_profile="sota_mvp",
            retrieval_overrides={},
            config_hash="hash-c",
        ),
    ]

    monkeypatch.setattr("wikipediarag.eval.retrieval_runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.retrieval_runner.eval_configs", lambda settings=None: configs)
    monkeypatch.setattr("wikipediarag.eval.retrieval_runner.ARTIFACT_ROOT", tmp_path / "eval")
    monkeypatch.setattr("wikipediarag.eval.retrieval_reporting.ARTIFACT_ROOT", tmp_path / "eval")

    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="fixture-hash",
        task_count=1,
        created_at="2026-07-26T00:00:00Z",
        snapshot_id="snapshot",
        index_version="index",
        zim_checksum="sha",
        retrieval_profile_hash="profile",
        generator_alias="generator_main",
        verifier_alias="verifier",
        jsonl_path=str(Path("artifacts/eval/fixtures/tasks.jsonl")),
    )
    client = FakeRetrievalClient()
    resume_events: list[Any] = []

    first = await run_retrieval_suite(
        manifest,
        [_task("t1")],
        api="http://api",
        batch_size=1,
        run_id="retrieval-test",
        client=client,
    )
    second = await run_retrieval_suite(
        manifest,
        [_task("t1")],
        api="http://api",
        batch_size=1,
        resume_run_id="retrieval-test",
        client=client,
        progress_callback=lambda status, _event: resume_events.append(status),
    )
    status = load_latest_retrieval_status()
    md_path, json_path = write_retrieval_report(second)

    assert first.run_id == second.run_id == "retrieval-test"
    assert client.calls == 2
    assert resume_events[0].processed_task_runs == 2
    assert {summary.config_id for summary in second.config_summaries} == {
        "bm25_only",
        "dense_only",
        "sota_mvp_conditional_harness",
    }
    assert any(summary.status == "unsupported" for summary in second.config_summaries)
    assert status.processed_task_runs == 2
    assert status.state == "completed"
    assert md_path.exists()
    assert json_path.exists()
    markdown = md_path.read_text(encoding="utf-8")
    assert "Retrieval evaluation report" in markdown
    assert "## Stage Timings" in markdown
    assert "| bm25 |" in markdown
