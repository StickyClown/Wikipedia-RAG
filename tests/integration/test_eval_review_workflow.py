from __future__ import annotations

import json
from pathlib import Path

import pytest

from wikipediarag.eval.review import (
    freeze_reviewed_suite,
    load_locked_split_manifest,
    load_locked_split_tasks,
    write_review_pool,
)


def _row(index: int, decision: str = "AUTO_ACCEPT") -> dict[str, object]:
    return {
        "candidate_id": f"candidate-{index}",
        "decision_status": decision,
        "trusted_task": {
            "task_id": f"task-{index}",
            "question": f"Что подтверждает reviewed row {index}?",
            "gold_page_ids": [f"page-{index}"],
            "gold_chunk_ids": [f"chunk-{index}"],
            "gold_titles": [f"Title {index}"],
            "provenance": {
                "snapshot_id": "snapshot",
                "index_version": "index",
                "zim_checksum": "zim",
                "retrieval_profile_hash": "profile",
            },
        },
    }


def test_review_freeze_manifests_only_include_reviewed_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("wikipediarag.eval.review.ARTIFACT_ROOT", tmp_path / "eval")
    input_path = tmp_path / "candidates.jsonl"
    rows = [_row(1), _row(2), _row(3, "REVIEW"), _row(4, "REJECT")]
    input_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )

    pool = write_review_pool(input_path=input_path, output_suite="reviewed-suite")
    frozen = freeze_reviewed_suite(suite="reviewed-suite", dev_count=1, test_count=1)
    dev_manifest, _ = load_locked_split_manifest("reviewed-suite", "dev")
    test_manifest, _ = load_locked_split_manifest("reviewed-suite", "test")
    dev_tasks = load_locked_split_tasks(dev_manifest, "dev")
    test_tasks = load_locked_split_tasks(test_manifest, "test")

    assert pool.reviewed_count == 2
    assert pool.unreviewed_count == 1
    assert pool.rejected_count == 1
    assert Path(frozen.dev_manifest).exists()
    assert Path(frozen.test_manifest).exists()
    assert len(dev_tasks) == 1
    assert len(test_tasks) == 1
    assert {task.task_id for task in [*dev_tasks, *test_tasks]} <= {"task-1", "task-2"}
