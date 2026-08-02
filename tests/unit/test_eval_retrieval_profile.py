from __future__ import annotations

from threading import Lock
from typing import Any

import pytest

from wikipediarag.config import Settings
from wikipediarag.eval import commands
from wikipediarag.eval.commands import eval_profile_retrieval
from wikipediarag.eval.schemas import EvalConfig, EvalDatasetManifest, EvalTask


class _ProfileClient:
    def __init__(self) -> None:
        self._lock = Lock()
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
        del question, api, top_k, retrieval_profile, retrieval_overrides
        with self._lock:
            self.calls += 1
            value = self.calls * 10
        return {
            "trace_id": f"trace-{value}",
            "index_contract_id": "index-contract",
            "run_contract_id": "run-contract",
            "events": [
                {
                    "stage": "timings",
                    "timings_ms": {
                        "bm25": value,
                        "dense_embedding": value + 1,
                        "dense_search": value + 2,
                        "dense_total": value + 3,
                        "fusion": 1,
                        "rerank": value + 4,
                        "context": 1,
                        "retrieval_total": value + 5,
                    },
                },
                {"stage": "context", "latency_ms": value + 5, "candidates": []},
            ],
        }


@pytest.mark.asyncio
async def test_eval_profile_retrieval_writes_warm_profile_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    manifest = EvalDatasetManifest(
        dataset_name="reviewed-wikipedia-smoke-v1-dev",
        dataset_version="2026.07.1",
        dataset_hash="dataset-hash",
        task_count=2,
        created_at="2026-07-30T00:00:00Z",
        snapshot_id="snapshot",
        index_version="index",
        zim_checksum="zim",
        retrieval_profile_hash="profile-hash",
        generator_alias="generator",
        verifier_alias="verifier",
        jsonl_path="dev.jsonl",
    )
    tasks = [_task("task-1"), _task("task-2")]
    config = EvalConfig(
        config_id="sota_mvp_normal",
        retrieval_profile="sota_mvp",
        retrieval_overrides={},
        config_hash="config-hash",
        model_aliases={},
    )
    monkeypatch.setattr(commands, "ARTIFACT_ROOT", tmp_path / "eval")
    monkeypatch.setattr(commands, "load_locked_split_manifest", lambda _suite, _split: (manifest, {}))
    monkeypatch.setattr(commands, "load_locked_split_tasks", lambda _manifest, _split: tasks)
    monkeypatch.setattr(commands, "_single_eval_config", lambda _config_id, _settings: config)

    report = await eval_profile_retrieval(
        suite="reviewed-wikipedia-smoke-v1",
        split="dev",
        api="http://api.test",
        limit=2,
        warmup_iterations=1,
        measured_iterations=2,
        batch_size=2,
        settings=Settings(app_env="test", auth_mode="test"),
        client=_ProfileClient(),
    )

    assert report["warmup"] == {"total": 2, "completed": 2, "failed": 0}
    assert report["measured"] == {"total": 4, "completed": 4, "failed": 0}
    assert report["stage_latency"]["bm25"]["p50_ms"] == 40.0
    assert report["stage_latency"]["bm25"]["p95_ms"] == 60.0
    assert report["stage_latency"]["rerank"]["p95_ms"] == 64.0
    assert (tmp_path / "eval" / "retrieval-profiles" / "reviewed-wikipedia-smoke-v1" / "latest.json").exists()


def _task(task_id: str) -> EvalTask:
    return EvalTask(
        task_id=task_id,
        question=f"Question {task_id}",
        task_family="single_hop_factual",
        reference_answer="answer",
        accepted_answers=["answer"],
        unanswerable=False,
        expected_mode="normal_sufficient",
        gold_page_ids=["page"],
        gold_section_ids=["section"],
        gold_chunk_ids=["chunk"],
        gold_evidence=[],
        reasoning_path=[],
        generator_alias="generator",
        verifier_alias="verifier",
        zim_checksum="zim",
        snapshot_id="snapshot",
        index_version="index",
        retrieval_profile_hash="profile-hash",
    )
