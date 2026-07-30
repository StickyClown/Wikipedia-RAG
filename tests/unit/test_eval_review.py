from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from wikipediarag.eval.artifacts import read_json
from wikipediarag.eval.review import (
    eval_release_gate,
    evaluate_release_gate,
    format_release_gate_progress,
    freeze_reviewed_suite,
    load_release_gate_status,
    review_status_for_decision,
    write_review_pool,
)
from wikipediarag.eval.schemas import (
    ConfigSummary,
    EvalDatasetManifest,
    EvalRunManifest,
    EvalRunStatus,
    ReleaseGateStatus,
    RetrievalConfigSummary,
    RetrievalEvalStatus,
    RetrievalRunManifest,
)


def _candidate(
    candidate_id: str,
    *,
    decision: str = "AUTO_ACCEPT",
    review_status: str = "unreviewed",
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "decision_status": decision,
        "review_status": review_status,
        "trusted_task": {
            "task_id": f"task-{candidate_id}",
            "question": f"Что подтверждает кандидат {candidate_id}?",
            "gold_page_ids": [f"page-{candidate_id}"],
            "gold_chunk_ids": [f"chunk-{candidate_id}"],
            "gold_titles": [f"Title {candidate_id}"],
            "provenance": {
                "snapshot_id": "snapshot",
                "index_version": "index",
                "zim_checksum": "zim",
                "retrieval_profile_hash": "profile",
                "index_contract_id": "sha256:index",
                "run_contract_id": "sha256:run",
            },
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def _patch_review_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("wikipediarag.eval.review.ARTIFACT_ROOT", tmp_path / "eval")


def test_review_status_transitions_cover_auto_manual_and_reject() -> None:
    assert review_status_for_decision("AUTO_ACCEPT") == "reviewed"
    assert review_status_for_decision("REVIEW") == "unreviewed"
    assert review_status_for_decision("REVIEW", current_status="reviewed") == "reviewed"
    assert review_status_for_decision("REJECT") == "rejected"
    assert review_status_for_decision("REJECT", current_status="reviewed") == "rejected"


def test_freeze_reviewed_is_deterministic_and_refuses_different_locked_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_review_root(monkeypatch, tmp_path)
    input_path = tmp_path / "candidates.jsonl"
    _write_jsonl(input_path, [_candidate("1"), _candidate("2"), _candidate("3"), _candidate("4")])
    first_pool = write_review_pool(input_path=input_path, output_suite="suite-a")

    first = freeze_reviewed_suite(suite="suite-a", dev_count=1, test_count=2)
    second = freeze_reviewed_suite(suite="suite-a", dev_count=1, test_count=2)

    assert first.dev_dataset_hash == second.dev_dataset_hash
    assert first.test_dataset_hash == second.test_dataset_hash
    assert second.reused_locked == ["dev", "test"]
    assert Path(first_pool.jsonl_path).exists()

    test_manifest = tmp_path / "eval" / "datasets" / "suite-a" / "locked" / "test.manifest.json"
    payload = read_json(test_manifest)
    payload["dataset_hash"] = "different"
    test_manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(FileExistsError, match="different hash"):
        freeze_reviewed_suite(suite="suite-a", dev_count=1, test_count=2)


def _config_summary(
    *,
    citation_precision: float = 1.0,
    unsupported_claim_rate: float = 0.0,
    unanswerable_accuracy: float = 1.0,
    cited_hard_negative_rate: float = 0.0,
    latency_p95_ms: float = 100.0,
    mixed_contracts: bool = False,
) -> ConfigSummary:
    return ConfigSummary(
        config_id="sota_mvp_normal",
        config_hash="hash",
        task_count=1,
        metrics={
            "citation_precision": citation_precision,
            "unsupported_claim_rate": unsupported_claim_rate,
            "unanswerable_accuracy": unanswerable_accuracy,
            "cited_hard_negative_rate": cited_hard_negative_rate,
            "latency_p95_ms": latency_p95_ms,
        },
        by_family={
            "single_hop_factual": {
                "page_recall_at_10": 1.0,
                "chunk_recall_at_10": 1.0,
                "mrr_at_10": 1.0,
                "ndcg_at_10": 1.0,
            }
        },
        failed_task_ids=[],
        errors=["mixed_contract_ids:index_contract_id"] if mixed_contracts else [],
        contract_ids={"index_contract_id": ["a", "b"] if mixed_contracts else ["a"]},
    )


def _retrieval_summary(
    *,
    false_positive_evidence_rate: float = 0.0,
    dangerous_false_positive_evidence_rate: float = 0.0,
    page_recall_at_10: float = 1.0,
    latency_p95_ms: float = 100.0,
) -> RetrievalConfigSummary:
    return RetrievalConfigSummary(
        config_id="sota_mvp_normal",
        config_hash="hash",
        status="completed",
        task_count=1,
        metrics={
            "false_positive_evidence_rate": false_positive_evidence_rate,
            "dangerous_false_positive_evidence_rate": dangerous_false_positive_evidence_rate,
            "latency_p95_ms": latency_p95_ms,
        },
        by_family={
            "single_hop_factual": {
                "page_recall_at_10": page_recall_at_10,
                "chunk_recall_at_10": 1.0,
                "mrr_at_10": 1.0,
                "ndcg_at_10": 1.0,
            }
        },
        failed_task_ids=[],
    )


def _answer_run(run_id: str, summary: ConfigSummary) -> EvalRunManifest:
    return EvalRunManifest(
        run_id=run_id,
        suite="suite",
        dataset_hash="dataset",
        dataset_path="tasks.jsonl",
        created_at="2026-07-28T00:00:00Z",
        config_summaries=[summary],
        run_dir="runs",
    )


def _retrieval_run(run_id: str, summary: RetrievalConfigSummary) -> RetrievalRunManifest:
    return RetrievalRunManifest(
        run_id=run_id,
        suite="suite",
        dataset_hash="dataset",
        dataset_path="tasks.jsonl",
        created_at="2026-07-28T00:00:00Z",
        batch_size=1,
        config_summaries=[summary],
        run_dir="retrieval-runs",
    )


def test_release_gate_blocks_test_failures_and_keeps_dev_diagnostic() -> None:
    baseline = {
        "retrieval": {
            "sota_mvp_normal": {
                "metrics": {"latency_p95_ms": 100.0},
                "by_family": {
                    "single_hop_factual": {
                        "page_recall_at_10": 1.0,
                        "chunk_recall_at_10": 1.0,
                        "mrr_at_10": 1.0,
                        "ndcg_at_10": 1.0,
                    }
                },
            }
        }
    }

    report = evaluate_release_gate(
        suite="suite",
        baseline=baseline,
        dev_answer=_answer_run("dev-answer", _config_summary(citation_precision=0.0)),
        dev_retrieval=_retrieval_run("dev-retrieval", _retrieval_summary(page_recall_at_10=0.9)),
        test_answer=_answer_run("test-answer", _config_summary(mixed_contracts=True, unsupported_claim_rate=1.0)),
        test_retrieval=_retrieval_run(
            "test-retrieval",
            _retrieval_summary(
                false_positive_evidence_rate=0.5,
                dangerous_false_positive_evidence_rate=0.5,
                latency_p95_ms=130.0,
            ),
        ),
    )

    assert report["passed"] is False
    blocking_metrics = {item["metric"] for item in report["blocking_failures"]}
    diagnostic_metrics = {item["metric"] for item in report["diagnostic_findings"]}
    assert {
        "index_contract_id",
        "contract_ids",
        "unsupported_claim_rate",
        "dangerous_false_positive_evidence_rate",
    } <= blocking_metrics
    assert "latency_p95_ms" in blocking_metrics
    assert "citation_precision" in diagnostic_metrics
    assert "single_hop_factual.page_recall_at_10" in diagnostic_metrics


def test_release_gate_keeps_legacy_false_positive_diagnostic_only() -> None:
    report = evaluate_release_gate(
        suite="suite",
        baseline={},
        dev_answer=_answer_run("dev-answer", _config_summary()),
        dev_retrieval=_retrieval_run("dev-retrieval", _retrieval_summary()),
        test_answer=_answer_run("test-answer", _config_summary()),
        test_retrieval=_retrieval_run(
            "test-retrieval",
            _retrieval_summary(false_positive_evidence_rate=0.5, dangerous_false_positive_evidence_rate=0.0),
        ),
    )

    assert report["passed"] is True
    assert report["blocking_failures"] == []


def test_release_gate_blocks_answer_cited_hard_negative() -> None:
    report = evaluate_release_gate(
        suite="suite",
        baseline={},
        dev_answer=_answer_run("dev-answer", _config_summary()),
        dev_retrieval=_retrieval_run("dev-retrieval", _retrieval_summary()),
        test_answer=_answer_run("test-answer", _config_summary(cited_hard_negative_rate=1.0)),
        test_retrieval=_retrieval_run("test-retrieval", _retrieval_summary()),
    )

    assert report["passed"] is False
    assert {item["metric"] for item in report["blocking_failures"]} == {"cited_hard_negative_rate"}


def test_release_gate_does_not_require_unanswerable_metric_when_split_has_none() -> None:
    answer_summary = _config_summary()
    answer_summary.metrics.pop("unanswerable_accuracy")
    report = evaluate_release_gate(
        suite="suite",
        baseline={},
        dev_answer=_answer_run("dev-answer", _config_summary()),
        dev_retrieval=_retrieval_run("dev-retrieval", _retrieval_summary()),
        test_answer=_answer_run("test-answer", answer_summary),
        test_retrieval=_retrieval_run("test-retrieval", _retrieval_summary()),
    )

    assert report["passed"] is True
    assert report["blocking_failures"] == []


def test_format_release_gate_progress_omits_question_text() -> None:
    status = ReleaseGateStatus(
        run_id="suite-release-gate",
        state="running",
        phase="dev_answer",
        suite="suite",
        api="http://api",
        run_dir="artifacts/eval/release-gates/suite/suite-release-gate",
        total_stages=5,
        stage_index=1,
        current_stage="dev_answer",
        child_run_id="answer-run",
        child_processed_task_runs=2,
        child_total_task_runs=10,
        child_failed_task_runs=1,
        elapsed_seconds=65,
        timings_ms={"dev_answer": 1200},
        started_at="2026-07-28T00:00:00Z",
        updated_at="2026-07-28T00:01:05Z",
    )

    rendered = format_release_gate_progress(status, "child_task_completed")

    assert "stage=dev_answer" in rendered
    assert "processed=2/10" in rendered
    assert "timings_ms=dev_answer:1200" in rendered
    assert "Что подтверждает" not in rendered


@pytest.mark.asyncio
async def test_eval_release_gate_writes_top_level_status_and_timings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_review_root(monkeypatch, tmp_path)
    input_path = tmp_path / "candidates.jsonl"
    _write_jsonl(input_path, [_candidate("1"), _candidate("2"), _candidate("3")])
    write_review_pool(input_path=input_path, output_suite="suite-status")
    freeze_reviewed_suite(suite="suite-status", dev_count=1, test_count=1)
    progress_events: list[tuple[ReleaseGateStatus, str]] = []
    answer_batch_sizes: list[int] = []
    answer_run_ids: list[str] = []
    answer_reuse_completed: list[object] = []
    retrieval_batch_sizes: list[int] = []
    retrieval_run_ids: list[str] = []

    async def fake_run_suite(*args: object, **kwargs: object) -> EvalRunManifest:
        manifest = cast(EvalDatasetManifest, args[0])
        answer_batch_size = kwargs.get("batch_size")
        assert isinstance(answer_batch_size, int)
        answer_batch_sizes.append(answer_batch_size)
        answer_run_ids.append(str(kwargs.get("run_id") or ""))
        answer_reuse_completed.append(kwargs.get("reuse_completed"))
        callback = kwargs.get("progress_callback")
        if callable(callback):
            callback(
                EvalRunStatus(
                    run_id=f"{manifest.dataset_name}-answer",
                    state="running",
                    phase="config_running",
                    suite=manifest.dataset_name,
                    dataset_hash=manifest.dataset_hash,
                    dataset_path=manifest.jsonl_path,
                    run_dir=f"answer/{manifest.dataset_name}",
                    batch_size=answer_batch_sizes[-1],
                    total_configs=1,
                    total_tasks=1,
                    total_task_runs=1,
                    processed_task_runs=1,
                    completed_task_runs=1,
                    failed_task_runs=0,
                    current_config_id="sota_mvp_normal",
                    current_config_index=1,
                    current_task_id="task-1",
                    current_task_index=1,
                    started_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:01Z",
                ),
                "task_completed",
            )
        return EvalRunManifest(
            run_id=f"{manifest.dataset_name}-answer",
            suite=manifest.dataset_name,
            dataset_hash=manifest.dataset_hash,
            dataset_path=manifest.jsonl_path,
            created_at="2026-07-28T00:00:01Z",
            batch_size=answer_batch_sizes[-1],
            config_summaries=[_config_summary()],
            run_dir=f"answer/{manifest.dataset_name}",
        )

    async def fake_run_retrieval_suite(*args: object, **kwargs: object) -> RetrievalRunManifest:
        manifest = cast(EvalDatasetManifest, args[0])
        retrieval_batch_size = kwargs.get("batch_size")
        assert isinstance(retrieval_batch_size, int)
        retrieval_batch_sizes.append(retrieval_batch_size)
        retrieval_run_ids.append(str(kwargs.get("run_id") or ""))
        callback = kwargs.get("progress_callback")
        if callable(callback):
            callback(
                RetrievalEvalStatus(
                    run_id=f"{manifest.dataset_name}-retrieval",
                    state="running",
                    phase="config_running",
                    suite=manifest.dataset_name,
                    dataset_hash=manifest.dataset_hash,
                    dataset_path=manifest.jsonl_path,
                    run_dir=f"retrieval/{manifest.dataset_name}",
                    batch_size=10,
                    total_configs=1,
                    supported_configs=1,
                    total_tasks=1,
                    total_task_runs=1,
                    processed_task_runs=1,
                    completed_task_runs=1,
                    failed_task_runs=0,
                    current_config_id="sota_mvp_normal",
                    current_config_index=1,
                    current_task_id="task-1",
                    current_task_index=1,
                    current_batch=1,
                    total_batches=1,
                    started_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:01Z",
                ),
                "task_completed",
            )
        return RetrievalRunManifest(
            run_id=f"{manifest.dataset_name}-retrieval",
            suite=manifest.dataset_name,
            dataset_hash=manifest.dataset_hash,
            dataset_path=manifest.jsonl_path,
            created_at="2026-07-28T00:00:01Z",
            batch_size=10,
            config_summaries=[_retrieval_summary()],
            run_dir=f"retrieval/{manifest.dataset_name}",
        )

    monkeypatch.setattr("wikipediarag.eval.review.run_suite", fake_run_suite)
    monkeypatch.setattr("wikipediarag.eval.review.run_retrieval_suite", fake_run_retrieval_suite)

    report = await eval_release_gate(
        suite="suite-status",
        api="http://api",
        progress_callback=lambda status, event: progress_events.append((status, event)),
    )

    assert report["passed"] is True
    assert {"dev_answer", "dev_retrieval", "test_answer", "test_retrieval", "gate_evaluation", "total"} <= set(
        report["timings_ms"]
    )
    assert "-suite-status-release-gate-" in report["release_gate_run"]["run_id"]
    assert report["release_gate_run"]["report_id"] == report["release_gate_run"]["run_id"]
    assert report["config_snapshot"]["config_id"] == "sota_mvp_normal"
    assert any(event == "child_task_completed" for _, event in progress_events)
    assert progress_events[-1][1] == "run_completed"
    assert answer_batch_sizes == [6, 6]
    assert all(run_id and "-answer-" in run_id for run_id in answer_run_ids)
    assert answer_reuse_completed == [False, False]
    assert retrieval_batch_sizes == [10, 10]
    assert all(run_id and "-retrieval-" in run_id for run_id in retrieval_run_ids)

    status = load_release_gate_status("suite-status")
    assert status.state == "completed"
    assert status.passed is True
    assert status.blocking_failures == 0
    assert status.report_id == status.run_id
    assert status.report_path.endswith("report.json")
    assert status.stage_report_paths["dev_answer"].endswith("manifest.json")
    explicit_status = load_release_gate_status("suite-status", report_id=status.report_id)
    assert explicit_status.run_id == status.run_id
    status_path = Path(status.run_dir) / "status.json"
    assert status_path.exists()
    assert read_json(Path(status.report_path))["passed"] is True
    events_text = status_path.parent.joinpath("logs", "events.jsonl").read_text(encoding="utf-8")
    assert '"event":"run_started"' in events_text
    assert "Что подтверждает" not in events_text
