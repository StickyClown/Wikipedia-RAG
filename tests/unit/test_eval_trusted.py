from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from wikipediarag.config import Settings
from wikipediarag.eval.artifacts import read_json, read_jsonl
from wikipediarag.eval.corpus import CorpusChunk, CorpusSnapshot
from wikipediarag.eval.trusted import (
    TrustedCatalogItem,
    TrustedEvalTask,
    TrustedFamily,
    TrustedGenerateCliReporter,
    TrustedGenerateRuntimeConfig,
    _catalog_items,
    _deterministic_trusted_candidate,
    _generate_trusted_family,
    _task_from_trusted_candidate,
    _trusted_task_valid,
    _TrustedTracker,
    generate_trusted_dataset,
    load_trusted_partial_tasks,
    load_trusted_status,
    trusted_family_targets,
    write_trusted_catalog,
    write_trusted_report,
)


def _snapshot() -> CorpusSnapshot:
    return CorpusSnapshot(
        snapshot_id="snapshot",
        index_version="index",
        physical_index="physical",
        read_alias="read",
        retrieval_profile="sota_mvp",
        retrieval_profile_hash="profile",
        embedding_alias="embed_default",
        embedding_dimensions=1024,
        zim_checksum="sha",
        zim_path=Path("zim/test.zim"),
    )


def _chunk(index: int, *, content: str | None = None, prev: str | None = None) -> CorpusChunk:
    return CorpusChunk(
        chunk_id=f"c{index}",
        document_id=f"p{index}",
        section_id=f"s{index}",
        title=f"Статья {index}",
        content=content or (f"Тестовый локальный факт {index} подтверждается этим parser output. " * 20).strip(),
        source_url=f"http://localhost/source/{index}",
        section_path=(f"Статья {index}", "Раздел") if prev else (f"Статья {index}",),
        parent_chunk_id=f"s{index}",
        prev_chunk_id=prev,
        next_chunk_id=None,
        metadata={"source_type": "wikipedia_zim", "zim_entry_path": f"A/Статья_{index}"},
    )


def _patch_artifact_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "eval"
    monkeypatch.setattr("wikipediarag.eval.artifacts.ARTIFACT_ROOT", root)
    monkeypatch.setattr("wikipediarag.eval.trusted.ARTIFACT_ROOT", root)
    monkeypatch.setattr("wikipediarag.eval.trusted.TRUSTED_RUNS_ROOT", root / "trusted-runs")
    monkeypatch.setattr("wikipediarag.eval.trusted.TRUSTED_REPORTS_ROOT", root / "trusted-reports")
    monkeypatch.setattr("wikipediarag.eval.trusted.TRUSTED_CATALOG_ROOT", root / "trusted-catalog")
    monkeypatch.setattr("wikipediarag.eval.trusted.LATEST_TRUSTED_RUN", root / "trusted-runs" / "latest.json")


def _trusted_task(family: TrustedFamily, index: int) -> TrustedEvalTask:
    item = _catalog_items([_chunk(index)], [])[0]
    candidate = _deterministic_trusted_candidate(family, [item])
    return _task_from_trusted_candidate(
        family,
        [item],
        candidate,
        attempt=index,
        seed=index,
        snapshot=_snapshot(),
        runtime=__import__("wikipediarag.eval.trusted", fromlist=["_resolve_trusted_runtime"])._resolve_trusted_runtime(
            _snapshot(),
            count=2,
            concurrency=1,
            rejection_budget=30,
            generator_alias="mock_generator_main",
            verifier_alias="mock_verifier",
            family_weights={"single_hop_prose": 1.0},
            run_id="resume-run",
            settings=Settings(),
        ),
    )


def _trusted_runtime(
    *,
    run_id: str,
    count: int,
    concurrency: int,
    family_targets: dict[TrustedFamily, int],
) -> TrustedGenerateRuntimeConfig:
    return cast(
        TrustedGenerateRuntimeConfig,
        __import__("wikipediarag.eval.trusted", fromlist=["_resolve_trusted_runtime"])._resolve_trusted_runtime(
            _snapshot(),
            count=count,
            concurrency=concurrency,
            rejection_budget=30,
            generator_alias="mock_generator_main",
            verifier_alias="mock_verifier",
            family_weights={family: float(target) for family, target in family_targets.items()},
            run_id=run_id,
            settings=Settings(),
        ),
    )


def test_trusted_family_targets_default_distribution_sums_to_300() -> None:
    targets = trusted_family_targets(300)

    assert sum(targets.values()) == 300
    assert targets["single_hop_prose"] == 90
    assert targets["deep_section_fact"] == 40
    assert targets["redirect_alias_rare"] == 35
    assert targets["structured_fact"] == 35
    assert targets["bridge_multi_hop"] == 25
    assert targets["comparison_multi_hop"] == 25
    assert targets["unanswerable"] == 25
    assert targets["hard_negative"] == 25


def test_write_json_atomic_retries_windows_file_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from wikipediarag.eval.artifacts import write_json_atomic

    original_replace = Path.replace
    calls = {"count": 0}

    def replace_once_locked(self: Path, target: Path) -> Path:
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("simulated Windows file lock")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace_once_locked)
    output = tmp_path / "status.json"

    write_json_atomic(output, {"state": "running"})

    assert calls["count"] == 2
    assert read_json(output) == {"state": "running"}


def test_hard_negative_uses_a_valid_local_counterfactual() -> None:
    items = _catalog_items([_chunk(1), _chunk(2)], [])
    candidate = _deterministic_trusted_candidate("hard_negative", items)
    task = _task_from_trusted_candidate(
        "hard_negative",
        items,
        candidate,
        attempt=1,
        seed=1,
        snapshot=_snapshot(),
        runtime=__import__("wikipediarag.eval.trusted", fromlist=["_resolve_trusted_runtime"])._resolve_trusted_runtime(
            _snapshot(),
            count=2,
            concurrency=1,
            rejection_budget=30,
            generator_alias="mock_generator_main",
            verifier_alias="mock_verifier",
            family_weights={"hard_negative": 1.0},
            run_id="hard-negative-run",
            settings=Settings(),
        ),
    )

    assert task.negative_candidates
    assert len(task.gold_chunk_ids) == 1
    assert task.gold_chunk_ids[0] != task.negative_candidates[0].chunk_id
    assert _trusted_task_valid(task)


@pytest.mark.asyncio
async def test_trusted_generate_family_refills_slot_when_attempt_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _trusted_runtime(
        run_id="trusted-backfill",
        count=4,
        concurrency=2,
        family_targets={"single_hop_prose": 4},
    )
    tracker = _TrustedTracker(runtime, _snapshot())
    pool = _catalog_items([_chunk(index) for index in range(1, 6)], [])
    active = 0
    max_active = 0
    started: dict[int, float] = {}
    finished: dict[int, float] = {}

    async def fake_build_task(
        family: TrustedFamily,
        packet: list[TrustedCatalogItem],
        attempt: int,
        *_args: object,
        **_kwargs: object,
    ) -> TrustedEvalTask:
        nonlocal active, max_active
        assert family == "single_hop_prose"
        active += 1
        max_active = max(max_active, active)
        started[attempt] = time.perf_counter()
        await asyncio.sleep(0.2 if attempt == 1 else 0.05)
        finished[attempt] = time.perf_counter()
        active -= 1
        candidate = _deterministic_trusted_candidate(family, packet)
        task = _task_from_trusted_candidate(
            family,
            packet,
            {
                **candidate,
                "question": f"Какой уникальный trusted факт подтверждается попыткой {attempt}?",
            },
            attempt=attempt,
            seed=attempt,
            snapshot=_snapshot(),
            runtime=runtime,
        )
        assert _trusted_task_valid(task)
        return task

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.trusted._build_trusted_task", fake_build_task)

    tasks: list[TrustedEvalTask] = []
    await _generate_trusted_family(
        "single_hop_prose",
        4,
        {"single_hop_prose": pool},
        tasks,
        set(),
        _snapshot(),
        runtime,
        Settings(),
        tracker,
    )

    assert len(tasks) == 4
    assert max_active == 2
    assert started[3] < finished[1]


@pytest.mark.asyncio
async def test_trusted_catalog_writes_structural_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return _snapshot()

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        assert limit == 5000
        return [
            _chunk(1),
            _chunk(2, prev="c1"),
            _chunk(3, content=("Список включает первый элемент и второй элемент. " * 20).strip()),
        ]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        assert limit == 1000
        return [("Alias 1", _chunk(1))]

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.trusted.default_load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_alias_chunks", fake_aliases)

    manifest = await write_trusted_catalog(settings=Settings())
    rows = read_jsonl(Path(manifest.jsonl_path), TrustedCatalogItem)

    assert manifest.item_count == 4
    assert manifest.by_structural_element["redirect_alias"] == 1
    assert {row.structural_element for row in rows} >= {"prose", "deep_section", "list_like", "redirect_alias"}


@pytest.mark.asyncio
async def test_trusted_catalog_path_is_windows_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshot = replace(_snapshot(), index_version="zim:snapshot:sota_mvp:embed_default:1024")

    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return snapshot

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        return [_chunk(1)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        return []

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.trusted.default_load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_alias_chunks", fake_aliases)

    manifest = await write_trusted_catalog(settings=Settings())

    assert ":" not in Path(manifest.jsonl_path).name
    assert read_jsonl(Path(manifest.jsonl_path), TrustedCatalogItem)


@pytest.mark.asyncio
async def test_trusted_generate_resume_keeps_partial_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    phase = {"value": "first"}

    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return _snapshot()

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        return [_chunk(1), _chunk(2)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        return []

    async def fake_family(
        family: TrustedFamily,
        target: int,
        pools: dict[TrustedFamily, list[object]],
        tasks: list[TrustedEvalTask],
        seen_questions: set[str],
        snapshot: CorpusSnapshot,
        runtime: object,
        settings: Settings,
        tracker: Any,
    ) -> None:
        assert family == "single_hop_prose"
        assert target == 2
        if phase["value"] == "first":
            task = _trusted_task("single_hop_prose", 1)
            tasks.append(task)
            seen_questions.add(task.question)
            await tracker.accept("single_hop_prose", 1, task)
            raise RuntimeError("simulated interruption")
        task = _trusted_task("single_hop_prose", 2)
        tasks.append(task)
        seen_questions.add(task.question)
        await tracker.accept("single_hop_prose", 2, task)

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.trusted.default_load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_alias_chunks", fake_aliases)
    monkeypatch.setattr("wikipediarag.eval.trusted._generate_trusted_family", fake_family)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        await generate_trusted_dataset(
            count=2,
            concurrency=1,
            generator_alias="mock_generator_main",
            verifier_alias="mock_verifier",
            family_weights={"single_hop_prose": 1.0},
            run_id="resume-run",
            settings=Settings(),
        )

    failed_status = load_trusted_status("resume-run")
    partial = load_trusted_partial_tasks("resume-run")
    assert failed_status.state == "failed"
    assert len(partial) == 1

    phase["value"] = "second"
    manifest = await generate_trusted_dataset(
        concurrency=1,
        rejection_budget=30,
        generator_alias="mock_generator_main",
        verifier_alias="mock_verifier",
        family_weights={"single_hop_prose": 1.0},
        resume_run_id="resume-run",
        settings=Settings(),
    )
    tasks = read_jsonl(Path(manifest.jsonl_path), TrustedEvalTask)
    completed_status = load_trusted_status("resume-run")
    events_path = tmp_path / "eval" / "trusted-runs" / "resume-run" / "events.jsonl"

    assert completed_status.state == "completed"
    assert manifest.task_count == 2
    assert len(tasks) == 2
    assert [task.task_id for task in tasks] == ["trusted-wiki-000001", "trusted-wiki-000002"]
    assert len({task.question for task in tasks}) == 2
    assert "run_completed" in events_path.read_text(encoding="utf-8")


def test_trusted_report_summarizes_train_unreviewed_dataset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from wikipediarag.eval.trusted import _write_trusted_dataset

    _patch_artifact_roots(monkeypatch, tmp_path)
    task = _trusted_task("single_hop_prose", 1)
    runtime = __import__("wikipediarag.eval.trusted", fromlist=["_resolve_trusted_runtime"])._resolve_trusted_runtime(
        _snapshot(),
        count=1,
        concurrency=1,
        rejection_budget=30,
        generator_alias="mock_generator_main",
        verifier_alias="mock_verifier",
        family_weights={"single_hop_prose": 1.0},
        run_id="report-run",
        settings=Settings(),
    )
    manifest = __import__("wikipediarag.eval.schemas", fromlist=["EvalDatasetManifest"]).EvalDatasetManifest(
        dataset_name="trusted-wikipedia-v2",
        dataset_version="2026.07.1",
        dataset_hash="hash",
        task_count=1,
        created_at="2026-07-26T00:00:00Z",
        snapshot_id="snapshot",
        index_version="index",
        zim_checksum="sha",
        retrieval_profile_hash="profile",
        generator_alias="mock_generator_main",
        verifier_alias="mock_verifier",
        jsonl_path=str(tmp_path / "eval" / "datasets" / "trusted-wikipedia-v2" / "tasks.jsonl"),
    )
    _write_trusted_dataset([task], manifest, runtime)

    report = write_trusted_report(suite="trusted-wikipedia-v2")
    payload = read_json(Path(report["json"]))

    assert payload["coverage"]["split"] == {"train": 1}
    assert payload["coverage"]["review_status"] == {"unreviewed": 1}
    assert payload["coverage"]["by_trusted_family"] == {"single_hop_prose": 1}


@pytest.mark.asyncio
async def test_trusted_live_reporter_prints_budget_and_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return _snapshot()

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        return [_chunk(1), _chunk(2)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        return []

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.trusted.default_load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_alias_chunks", fake_aliases)
    stream = StringIO()

    await generate_trusted_dataset(
        count=1,
        concurrency=1,
        rejection_budget=30,
        generator_alias="mock_generator_main",
        verifier_alias="mock_verifier",
        family_weights={"single_hop_prose": 1.0},
        run_id="reporter-run",
        settings=Settings(),
        progress_callback=TrustedGenerateCliReporter(stream),
    )

    output = stream.getvalue()
    assert "state=catalog_started" in output
    assert "family=single_hop_prose" in output
    assert "total=1/1" in output
    assert "rejected=0/30" in output
    assert "state=completed" in output


@pytest.mark.asyncio
async def test_trusted_rejection_budget_stops_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return _snapshot()

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        return [_chunk(1), _chunk(2)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        return []

    async def invalid_build(
        family: TrustedFamily,
        packet: list[TrustedCatalogItem],
        attempt: int,
        snapshot: CorpusSnapshot,
        runtime: object,
        settings: Settings,
        tracker: Any,
    ) -> TrustedEvalTask:
        task = _trusted_task("single_hop_prose", attempt)
        task.question = "Дублирующий вопрос?"
        task.source_spans = []
        task.gold_chunk_ids = []
        return task

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.trusted.default_load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_alias_chunks", fake_aliases)
    monkeypatch.setattr("wikipediarag.eval.trusted._build_trusted_task", invalid_build)

    with pytest.raises(RuntimeError, match="rejection_budget_exhausted"):
        await generate_trusted_dataset(
            count=2,
            concurrency=2,
            rejection_budget=2,
            generator_alias="mock_generator_main",
            verifier_alias="mock_verifier",
            family_weights={"single_hop_prose": 1.0},
            run_id="budget-run",
            settings=Settings(),
        )

    status = load_trusted_status("budget-run")
    assert status.state == "failed"
    assert status.stats.rejected == 2
    assert status.stats.accepted == 0
    assert status.config.rejection_budget == 2
    assert not (tmp_path / "eval" / "datasets" / "trusted-wikipedia-v2" / "latest.json").exists()


@pytest.mark.asyncio
async def test_hard_negative_production_path_uses_model_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return _snapshot()

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        return [_chunk(1), _chunk(2)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        return []

    calls: list[dict[str, Any]] = []

    async def fake_chat_completion(
        messages: list[dict[str, str]],
        settings: Settings,
        *,
        alias: str,
        response_format: dict[str, Any] | None = None,
        max_provider_attempts: int = 1,
    ) -> dict[str, Any]:
        calls.append(
            {
                "messages": messages,
                "alias": alias,
                "response_format": response_format,
                "max_provider_attempts": max_provider_attempts,
            }
        )
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"question":"Какой факт подтверждает первое свидетельство?",'
                            '"reference_answer":"Тестовый локальный факт 1",'
                            '"accepted_answers":["Тестовый локальный факт 1"],'
                            '"answer_type":"span","reasoning_path":["Статья 1"]}'
                        )
                    }
                }
            ]
        }

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.trusted.default_load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.trusted.load_alias_chunks", fake_aliases)
    monkeypatch.setattr("wikipediarag.eval.trusted.chat_completion", fake_chat_completion)

    manifest = await generate_trusted_dataset(
        count=1,
        concurrency=1,
        rejection_budget=30,
        generator_alias="generator_main",
        verifier_alias="verifier",
        family_weights={"hard_negative": 1.0},
        run_id="hard-negative-model-run",
        settings=Settings(),
    )
    tasks = read_jsonl(Path(manifest.jsonl_path), TrustedEvalTask)

    assert calls
    assert calls[0]["alias"] == "generator_main"
    assert "E1 является единственным gold evidence" in calls[0]["messages"][1]["content"]
    assert len(tasks[0].gold_chunk_ids) == 1
    assert tasks[0].negative_candidates[0].chunk_id != tasks[0].gold_chunk_ids[0]


def test_trusted_run_lock_blocks_active_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_artifact_roots(monkeypatch, tmp_path)
    module = __import__("wikipediarag.eval.trusted", fromlist=["_TrustedRunLock"])
    lock = module._TrustedRunLock.acquire("locked-run", takeover_stale=False)
    try:
        with pytest.raises(RuntimeError, match="already locked"):
            module._TrustedRunLock.acquire("locked-run", takeover_stale=False)
    finally:
        lock.release()
