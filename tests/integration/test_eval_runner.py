from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wikipediarag.eval.artifacts import read_json
from wikipediarag.eval.corpus import CorpusChunk
from wikipediarag.eval.runner import format_eval_run_progress, run_suite
from wikipediarag.eval.schemas import EvalConfig, EvalDatasetManifest, EvalRunStatus, EvalTask, GoldEvidence


class FakeEvalClient:
    def __init__(self) -> None:
        self.calls = 0

    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        self.calls += 1
        return {
            "failed": False,
            "query_run_id": "run",
            "trace_id": "trace",
            "answer": "Тестовый ответ [S1]",
            "usage": {
                "data": {
                    "retrieval": {
                        "index_contract_id": "sha256:index",
                        "run_contract_id": "sha256:run",
                        "evidence": [
                            {
                                "evidence_id": "S1",
                                "chunk_id": "c1",
                                "source_url": "http://localhost/source",
                            }
                        ],
                        "events": [
                            {"stage": "rrf", "candidates": [{"chunk_id": "c1", "scores": {"rrf_total": 1.0}}]},
                            {"stage": "rerank", "candidates": [{"chunk_id": "c1", "scores": {"rerank": 1.0}}]},
                            {"stage": "context", "latency_ms": 7, "candidates": [{"chunk_id": "c1"}]},
                        ],
                    },
                    "citation_validation": {
                        "citations": ["S1"],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                        "provider_cost": 0.01,
                        "timings_ms": {"generation_total": 11, "model_chat": 10},
                    },
                    "timings_ms": {"retrieval_total": 7, "generation_total": 11, "model_chat": 10},
                }
            },
        }

    def url_ok(self, url: str) -> bool:
        return True


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
                source_url="http://localhost/source",
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
async def test_runner_keeps_config_results_separate_and_reuses_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_refs(chunk_ids: list[str], **_kwargs: object) -> dict[str, CorpusChunk]:
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
            retrieval_overrides={"retrieval": {"bm25": True, "dense": False, "fusion": "none", "rerank": False}},
            config_hash="hash-a",
        ),
        EvalConfig(
            config_id="dense_only",
            retrieval_profile="sota_mvp",
            retrieval_overrides={"retrieval": {"bm25": False, "dense": True, "fusion": "none", "rerank": False}},
            config_hash="hash-b",
        ),
    ]

    monkeypatch.setattr("wikipediarag.eval.runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.runner.eval_configs", lambda settings=None: configs)
    monkeypatch.setattr("wikipediarag.eval.runner.ARTIFACT_ROOT", tmp_path / "eval")

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
    client = FakeEvalClient()
    progress_events: list[tuple[EvalRunStatus, str]] = []

    first = await run_suite(
        manifest,
        [_task("t1")],
        api="http://api",
        client=client,
        progress_callback=lambda status, event: progress_events.append((status, event)),
    )
    second = await run_suite(manifest, [_task("t1")], api="http://api", client=client)

    assert first.run_id == second.run_id
    assert {summary.config_id for summary in first.config_summaries} == {"bm25_only", "dense_only"}
    assert client.calls == 2
    assert all(summary.metrics["chunk_recall_at_20"] == 1.0 for summary in second.config_summaries)
    assert all(summary.metrics["stage_latency_generation_total_p95_ms"] == 11.0 for summary in second.config_summaries)
    assert all(summary.contract_ids["index_contract_id"] == ["sha256:index"] for summary in second.config_summaries)
    assert all(summary.contract_ids["run_contract_id"] == ["sha256:run"] for summary in second.config_summaries)
    assert progress_events[0][1] == "run_started"
    assert progress_events[-1][1] == "run_completed"
    assert any(event == "task_started" for _, event in progress_events)
    assert all("Что такое тест" not in format_eval_run_progress(status, event) for status, event in progress_events)

    run_dir = tmp_path / "eval" / "runs" / "fixture-suite" / "fixture-suite-fixture-hash"
    status = read_json(run_dir / "status.json")
    assert status["state"] == "completed"
    assert status["processed_task_runs"] == 2
    assert status["completed_task_runs"] == 2
    events_path = run_dir / "logs" / "events.jsonl"
    events_text = events_path.read_text(encoding="utf-8")
    assert '"event":"run_started"' in events_text
    assert '"event":"task_completed"' in events_text
    assert "Что такое тест" not in events_text
