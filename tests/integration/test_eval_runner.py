from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from wikipediarag.eval.artifacts import append_jsonl, read_json
from wikipediarag.eval.corpus import CorpusChunk
from wikipediarag.eval.runner import format_eval_run_progress, run_suite, summarize_config
from wikipediarag.eval.schemas import (
    EvalConfig,
    EvalDatasetManifest,
    EvalRunStatus,
    EvalTask,
    EvalTaskResult,
    GoldEvidence,
    TaskScores,
)


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


class EvidenceOnlyEvalClient(FakeEvalClient):
    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        del question, api, retrieval_profile, retrieval_overrides, mode
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
                                "title": "Статья",
                                "source_url": "http://localhost/source",
                                "scores": {"rerank": 0.9},
                            }
                        ],
                        "events": [{"stage": "harness", "timings_ms": {"extended_search_total": 12}}],
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


class MixedPathEvalClient(FakeEvalClient):
    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        payload = super().run_chat(
            question,
            api=api,
            retrieval_profile=retrieval_profile,
            retrieval_overrides=retrieval_overrides,
            mode=mode,
        )
        retrieval = payload["usage"]["data"]["retrieval"]
        if "harness" in question:
            retrieval["run_contract_id"] = "sha256:harness-child"
            retrieval["events"].append(
                {
                    "stage": "harness_tool",
                    "run_contract_id": "sha256:harness-child",
                    "latency_ms": 3,
                    "candidates": [{"chunk_id": "c1", "title": "Статья"}],
                }
            )
            retrieval["events"].append({"stage": "harness", "timings_ms": {"extended_search_total": 3}})
        else:
            retrieval["run_contract_id"] = "sha256:normal-child"
        return payload


class FlakyEvalClient(FakeEvalClient):
    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        if self.calls == 0:
            self.calls += 1
            return {"failed": True, "error": "run.failed"}
        return super().run_chat(
            question,
            api=api,
            retrieval_profile=retrieval_profile,
            retrieval_overrides=retrieval_overrides,
            mode=mode,
        )


class FailingWithRetrievalEvalClient(FakeEvalClient):
    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        del question, api, retrieval_profile, retrieval_overrides, mode
        self.calls += 1
        return {
            "failed": True,
            "error": "TimeoutError",
            "query_run_id": "run-failed",
            "trace_id": "trace-failed",
            "failed_event": {
                "event": "run.failed",
                "query_run_id": "run-failed",
                "data": {
                    "stage": "answer_generation",
                    "code": "TimeoutError",
                    "retryable": True,
                    "attempt": 1,
                    "last_successful_stage": "retrieval",
                    "trace_id": "trace-failed",
                    "safe_message": "TimeoutError",
                    "retrieval": {
                        "trace_id": "trace-failed",
                        "index_contract_id": "sha256:index",
                        "run_contract_id": "sha256:retrieval-child",
                        "events": [
                            {
                                "stage": "rerank",
                                "count": 1,
                                "latency_ms": 5,
                                "candidates": [
                                    {
                                        "chunk_id": "c1",
                                        "document_id": "p1",
                                        "section_id": "s1",
                                        "title": "Статья",
                                        "source_url": "http://localhost/source",
                                        "scores": {"rerank": 1.0},
                                    }
                                ],
                            }
                        ],
                    },
                },
            },
        }


def _task(task_id: str, *, question: str = "Что такое тест?") -> EvalTask:
    return EvalTask(
        task_id=task_id,
        question=question,
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


def _scores() -> TaskScores:
    return TaskScores(
        page_recall={"1": 1.0, "5": 1.0, "10": 1.0, "20": 1.0},
        section_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        chunk_recall={"5": 1.0, "10": 1.0, "20": 1.0},
        mrr_at_10=1.0,
        ndcg_at_10=1.0,
        full_hop_recall=1.0,
        path_completion=1.0,
        reranker_gold_delta=0.0,
        exact_match=1.0,
        token_f1=1.0,
        unanswerable_accuracy=0.0,
        citation_precision=1.0,
        citation_recall=1.0,
        unsupported_claim_rate=0.0,
        kiwix_url_ok=1.0,
    )


def test_summary_uses_latest_result_per_task_id() -> None:
    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"postprocess": {"extended_search": "conditional"}},
        config_hash="hash-answer",
    )
    task = _task("t1")
    failed = EvalTaskResult(
        task_id="t1",
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="failed",
        question=task.question,
        errors=["old failure"],
    )
    completed = EvalTaskResult(
        task_id="t1",
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="completed",
        question=task.question,
        answer="Тестовый ответ [S1]",
        scores=_scores(),
    )

    summary = summarize_config(config, [task], [failed, completed])

    assert summary.task_count == 1
    assert summary.failed_task_ids == []
    assert summary.errors == []
    assert summary.metrics["root_cause_passed_count"] == 1.0
    assert summary.metrics["root_cause_execution_error_count"] == 0.0
    assert summary.by_family["single_hop_factual"]["root_cause_passed_count"] == 1.0


@pytest.mark.asyncio
async def test_answer_eval_scores_extended_payload_from_final_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_refs(chunk_ids: list[str], **_kwargs: object) -> dict[str, CorpusChunk]:
        assert chunk_ids == ["c1", "c1"]
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

    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"postprocess": {"extended_search": "conditional"}},
        config_hash="hash-answer",
    )
    monkeypatch.setattr("wikipediarag.eval.runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.runner.eval_configs", lambda settings=None: [config])
    monkeypatch.setattr("wikipediarag.eval.runner.ARTIFACT_ROOT", tmp_path / "eval")
    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="evidence-hash",
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

    run = await run_suite(manifest, [_task("t1")], api="http://api", client=EvidenceOnlyEvalClient())
    summary = run.config_summaries[0]

    assert summary.failed_task_ids == []
    assert summary.metrics["chunk_recall_at_20"] == 1.0
    assert summary.metrics["page_recall_at_10"] == 1.0
    assert summary.metrics["citation_precision"] == 1.0


@pytest.mark.asyncio
async def test_answer_eval_retries_transient_run_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_refs(chunk_ids: list[str], **_kwargs: object) -> dict[str, CorpusChunk]:
        del chunk_ids
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

    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"postprocess": {"extended_search": "conditional"}},
        config_hash="hash-answer",
    )
    monkeypatch.setattr("wikipediarag.eval.runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.runner.eval_configs", lambda settings=None: [config])
    monkeypatch.setattr("wikipediarag.eval.runner.ARTIFACT_ROOT", tmp_path / "eval")
    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="retry-hash",
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
    client = FlakyEvalClient()

    run = await run_suite(manifest, [_task("t1")], api="http://api", client=client)
    summary = run.config_summaries[0]

    assert client.calls == 2
    assert summary.failed_task_ids == []
    assert summary.errors == []


class BackfillEvalClient(FakeEvalClient):
    def __init__(self, delays: dict[str, float]) -> None:
        super().__init__()
        self.delays = delays
        self.in_flight = 0
        self.max_in_flight = 0
        self.started_at: dict[str, float] = {}
        self.finished_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def run_chat(
        self,
        question: str,
        *,
        api: str,
        retrieval_profile: str,
        retrieval_overrides: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        del api, retrieval_profile, retrieval_overrides, mode
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.started_at[question] = time.perf_counter()
        try:
            time.sleep(self.delays.get(question, 0.01))
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
        finally:
            with self._lock:
                self.finished_at[question] = time.perf_counter()
                self.in_flight -= 1


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
    assert all(summary.contract_ids["run_contract_id"][0].startswith("sha256:") for summary in second.config_summaries)
    assert {summary.contract_ids["run_contract_id"][0] for summary in second.config_summaries} != {"sha256:run"}
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


@pytest.mark.asyncio
async def test_runner_strict_report_ignores_stale_rows_with_other_eval_run_id(
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

    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"postprocess": {"extended_search": "conditional"}},
        config_hash="hash-answer",
    )
    monkeypatch.setattr("wikipediarag.eval.runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.runner.eval_configs", lambda settings=None: [config])
    monkeypatch.setattr("wikipediarag.eval.runner.ARTIFACT_ROOT", tmp_path / "eval")
    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="strict-hash",
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
    run_id = "20260729T000000Z-fixture-suite-test-answer-deadbeef"
    results_path = (
        tmp_path / "eval" / "runs" / "fixture-suite" / run_id / "results" / "sota_mvp_normal-hash-answer.jsonl"
    )
    append_jsonl(
        results_path,
        EvalTaskResult(
            task_id="t1",
            config_id="sota_mvp_normal",
            config_hash="hash-answer",
            eval_run_id="old-run",
            report_id="old-report",
            run_started_at="2026-07-28T00:00:00Z",
            dataset_hash="strict-hash",
            status="completed",
            question="old",
            answer="old",
            scores=_scores(),
            contract_ids={"run_contract_id": "sha256:old"},
        ),
    )
    client = FakeEvalClient()

    run = await run_suite(
        manifest,
        [_task("t1")],
        api="http://api",
        client=client,
        run_id=run_id,
        report_id="report-1",
        reuse_completed=False,
    )

    assert client.calls == 1
    assert run.run_id == run_id
    assert run.report_id == "report-1"
    summary = run.config_summaries[0]
    assert summary.task_count == 1
    assert summary.errors == []
    assert summary.contract_ids["run_contract_id"][0].startswith("sha256:")
    result_rows = (results_path.read_text(encoding="utf-8")).splitlines()
    assert '"retrieval_contract_ids":["sha256:run"]' in result_rows[-1]
    status = read_json(tmp_path / "eval" / "runs" / "fixture-suite" / run_id / "status.json")
    assert status["processed_task_runs"] == 1


@pytest.mark.asyncio
async def test_runner_uses_one_root_contract_for_normal_and_harness_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_refs(chunk_ids: list[str], **_kwargs: object) -> dict[str, CorpusChunk]:
        del chunk_ids
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

    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"postprocess": {"extended_search": "conditional"}},
        config_hash="hash-answer",
    )
    monkeypatch.setattr("wikipediarag.eval.runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.runner.eval_configs", lambda settings=None: [config])
    monkeypatch.setattr("wikipediarag.eval.runner.ARTIFACT_ROOT", tmp_path / "eval")
    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="mixed-path-hash",
        task_count=2,
        created_at="2026-07-26T00:00:00Z",
        snapshot_id="snapshot",
        index_version="index",
        zim_checksum="sha",
        retrieval_profile_hash="profile",
        generator_alias="generator_main",
        verifier_alias="verifier",
        jsonl_path=str(Path("artifacts/eval/fixtures/tasks.jsonl")),
    )

    run = await run_suite(
        manifest,
        [_task("normal", question="normal"), _task("harness", question="harness")],
        api="http://api",
        client=MixedPathEvalClient(),
        reuse_completed=False,
    )

    summary = run.config_summaries[0]
    assert summary.errors == []
    assert len(summary.contract_ids["run_contract_id"]) == 1
    results_path = Path(run.run_dir) / "results" / "sota_mvp_normal-hash-answer.jsonl"
    rows = [EvalTaskResult.model_validate_json(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
    assert {row.execution_path for row in rows} == {"normal", "harness"}
    assert {tuple(row.retrieval_contract_ids) for row in rows} == {
        ("sha256:normal-child",),
        ("sha256:harness-child",),
    }
    assert any(row.tool_contract_ids == ["sha256:harness-child"] for row in rows)
    assert any(event["name"] == "path_selected" for row in rows for event in row.step_events)


@pytest.mark.asyncio
async def test_runner_failed_row_preserves_safe_failure_stage_and_retrieved_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"postprocess": {"extended_search": "conditional"}},
        config_hash="hash-answer",
    )
    monkeypatch.setattr("wikipediarag.eval.runner.eval_configs", lambda settings=None: [config])
    monkeypatch.setattr("wikipediarag.eval.runner.ARTIFACT_ROOT", tmp_path / "eval")
    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="failed-row-hash",
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
    client = FailingWithRetrievalEvalClient()

    run = await run_suite(manifest, [_task("t1")], api="http://api", client=client, reuse_completed=False)

    assert client.calls == 3
    summary = run.config_summaries[0]
    assert summary.failed_task_ids == ["t1"]
    result_path = Path(run.run_dir) / "results" / "sota_mvp_normal-hash-answer.jsonl"
    result = EvalTaskResult.model_validate_json(result_path.read_text(encoding="utf-8").splitlines()[-1])
    assert result.failure_stage == "answer_generation"
    assert result.failure_code == "TimeoutError"
    assert result.failure_retryable is True
    assert result.attempts == 3
    assert result.last_successful_stage == "retrieval"
    assert result.query_run_id == "run-failed"
    assert result.trace_id == "trace-failed"
    assert [candidate.chunk_id for candidate in result.reranked_candidates] == ["c1"]
    assert result.retrieval_contract_ids == ["sha256:retrieval-child"]
    assert result.diagnosis["root_cause"] == "execution_error"
    assert summary.metrics["root_cause_execution_error_count"] == 1.0
    assert any(event["name"] == "answer_generation_failed" for event in result.step_events)


@pytest.mark.asyncio
async def test_runner_batch_size_is_bounded_in_flight_backfill_scheduler(
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

    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={"postprocess": {"extended_search": "off"}},
        config_hash="hash-answer",
    )
    monkeypatch.setattr("wikipediarag.eval.runner.load_chunk_refs", fake_refs)
    monkeypatch.setattr("wikipediarag.eval.runner.eval_configs", lambda settings=None: [config])
    monkeypatch.setattr("wikipediarag.eval.runner.ARTIFACT_ROOT", tmp_path / "eval")
    manifest = EvalDatasetManifest(
        dataset_name="fixture-suite",
        dataset_version="test",
        dataset_hash="backfill-hash",
        task_count=4,
        created_at="2026-07-26T00:00:00Z",
        snapshot_id="snapshot",
        index_version="index",
        zim_checksum="sha",
        retrieval_profile_hash="profile",
        generator_alias="generator_main",
        verifier_alias="verifier",
        jsonl_path=str(Path("artifacts/eval/fixtures/tasks.jsonl")),
    )
    tasks = [_task(f"t{index}", question=f"q{index}") for index in range(1, 5)]
    client = BackfillEvalClient({"q1": 0.20, "q2": 0.02, "q3": 0.02, "q4": 0.02})

    first = await run_suite(manifest, tasks, api="http://api", client=client, batch_size=2)
    second = await run_suite(manifest, tasks, api="http://api", client=client, batch_size=2)

    assert first.batch_size == 2
    assert second.batch_size == 2
    assert client.calls == 4
    assert client.max_in_flight == 2
    assert client.started_at["q3"] < client.finished_at["q1"]

    run_dir = tmp_path / "eval" / "runs" / "fixture-suite" / "fixture-suite-backfill-has"
    status = read_json(run_dir / "status.json")
    assert status["batch_size"] == 2
    assert status["processed_task_runs"] == 4
