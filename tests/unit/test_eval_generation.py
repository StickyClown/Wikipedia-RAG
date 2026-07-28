from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from wikipediarag.config import Settings
from wikipediarag.eval.artifacts import read_json, read_jsonl
from wikipediarag.eval.corpus import CorpusChunk, CorpusSnapshot
from wikipediarag.eval.generate_runs import load_generate_partial_tasks, load_generate_status
from wikipediarag.eval.generator import (
    _generate_family,
    _GenerateProgressTracker,
    build_smoke_tasks,
    family_targets,
    generate_dataset,
)
from wikipediarag.eval.schemas import (
    EvalGenerateModelRef,
    EvalGenerateProgressEvent,
    EvalGenerateRuntimeConfig,
    EvalTask,
    TaskFamily,
)


def _chunk(index: int) -> CorpusChunk:
    return CorpusChunk(
        chunk_id=f"c{index}",
        document_id=f"p{index}",
        section_id=f"s{index}",
        title=f"Статья {index}",
        content=(f"Тестовый факт {index} подтверждён в локальном фрагменте. " * 20).strip(),
        source_url=f"http://localhost/source/{index}",
        section_path=(f"Статья {index}",),
        parent_chunk_id=f"s{index}",
        prev_chunk_id=None,
        next_chunk_id=None,
        metadata={"source_type": "wikipedia_zim"},
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


def _runtime(
    *,
    run_id: str,
    count: int,
    concurrency: int,
    generator_alias: str = "generator_main",
    verifier_alias: str = "verifier",
    family_targets_override: dict[TaskFamily, int],
    family_weights_override: dict[TaskFamily, float] | None = None,
) -> EvalGenerateRuntimeConfig:
    return EvalGenerateRuntimeConfig(
        run_id=run_id,
        count=count,
        concurrency=concurrency,
        generator=EvalGenerateModelRef(alias=generator_alias, provider="openrouter", model="gen-model"),
        verifier=EvalGenerateModelRef(alias=verifier_alias, provider="openrouter", model="ver-model"),
        family_weights=family_weights_override
        or {family: float(target) for family, target in family_targets_override.items()},
        family_targets=family_targets_override,
    )


def _patch_artifact_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "eval"
    monkeypatch.setattr("wikipediarag.eval.artifacts.ARTIFACT_ROOT", root)
    monkeypatch.setattr("wikipediarag.eval.generator.ARTIFACT_ROOT", root)
    monkeypatch.setattr("wikipediarag.eval.generate_runs.ARTIFACT_ROOT", root)
    monkeypatch.setattr("wikipediarag.eval.generate_runs.GENERATE_RUNS_ROOT", root / "generate-runs")
    monkeypatch.setattr("wikipediarag.eval.generate_runs.LATEST_GENERATE_RUN", root / "generate-runs" / "latest.json")


def _single_hop_task(question: str, index: int = 1) -> EvalTask:
    chunk = _chunk(index)
    return EvalTask(
        task_id=f"pending-single-{index}",
        question=question,
        task_family="single_hop_factual",
        reference_answer="Подтверждённый локальный факт",
        accepted_answers=["Подтверждённый локальный факт"],
        unanswerable=False,
        expected_mode="normal_sufficient",
        gold_page_ids=[chunk.document_id],
        gold_section_ids=[chunk.section_id],
        gold_chunk_ids=[chunk.chunk_id],
        gold_evidence=[],
        reasoning_path=[chunk.title],
        generator_alias="generator_main",
        verifier_alias="verifier",
        zim_checksum="sha",
        snapshot_id="snapshot",
        index_version="index",
        retrieval_profile_hash="profile",
        generation_seed=index,
    )


def test_family_targets_match_required_150_distribution() -> None:
    assert family_targets(150) == {
        "single_hop_factual": 60,
        "alias_redirect_rare": 20,
        "deep_section_fact": 20,
        "comparison_multi_hop": 25,
        "unanswerable": 15,
        "hard_negative": 10,
    }


def test_family_targets_support_explicit_weights() -> None:
    assert family_targets(
        100,
        {
            "single_hop_factual": 1.0,
            "alias_redirect_rare": 0.0,
            "deep_section_fact": 0.0,
            "comparison_multi_hop": 1.0,
            "unanswerable": 0.0,
            "hard_negative": 0.0,
        },
    ) == {
        "single_hop_factual": 50,
        "alias_redirect_rare": 0,
        "deep_section_fact": 0,
        "comparison_multi_hop": 50,
        "unanswerable": 0,
        "hard_negative": 0,
    }


def test_smoke_tasks_are_distinct_and_have_gold_chunks() -> None:
    tasks = build_smoke_tasks([_chunk(index) for index in range(12)], _snapshot(), count=10)

    assert len(tasks) == 10
    assert len({task.gold_page_ids[0] for task in tasks}) == 10
    assert all(task.gold_chunk_ids for task in tasks)
    assert all(task.gold_evidence[0].source_url.startswith("http://localhost/source/") for task in tasks)


@pytest.mark.asyncio
async def test_generate_dataset_emits_progress_reasons_and_writes_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    events: list[tuple[str, str | None, str]] = []
    responses: list[tuple[str, object]] = [
        ("generator_main", '{"question"'),
        ("generator_main", '{"question"'),
        (
            "generator_main",
            json.dumps(
                {
                    "question": "Какой вопрос verifier должен отклонить?",
                    "reference_answer": "Тестовый факт подтверждён в локальном фрагменте.",
                    "accepted_answers": ["Тестовый факт подтверждён в локальном фрагменте."],
                    "reasoning_path": ["Статья 1"],
                },
                ensure_ascii=False,
            ),
        ),
        ("verifier", json.dumps({"verdict": "reject"}, ensure_ascii=False)),
        (
            "generator_main",
            json.dumps(
                {
                    "question": "Какой вопрос verifier вернёт с битым JSON?",
                    "reference_answer": "Тестовый факт подтверждён в локальном фрагменте.",
                    "accepted_answers": ["Тестовый факт подтверждён в локальном фрагменте."],
                    "reasoning_path": ["Статья 1"],
                },
                ensure_ascii=False,
            ),
        ),
        ("verifier", '{"verdict"'),
        ("generator_main", RuntimeError("provider unavailable")),
        (
            "generator_main",
            json.dumps(
                {
                    "question": "Что известно о статье «Статья 1» в локальном корпусе?",
                    "reference_answer": "Тестовый факт подтверждён в локальном фрагменте.",
                    "accepted_answers": ["Тестовый факт подтверждён в локальном фрагменте."],
                    "reasoning_path": ["Статья 1"],
                },
                ensure_ascii=False,
            ),
        ),
        ("verifier", json.dumps({"verdict": "accept"}, ensure_ascii=False)),
        (
            "generator_main",
            json.dumps(
                {
                    "question": "Какой подтверждённый факт локально указан для «Статья 1»?",
                    "reference_answer": "Тестовый факт подтверждён в локальном фрагменте.",
                    "accepted_answers": ["Тестовый факт подтверждён в локальном фрагменте."],
                    "reasoning_path": ["Статья 1"],
                },
                ensure_ascii=False,
            ),
        ),
        ("verifier", json.dumps({"verdict": "accept"}, ensure_ascii=False)),
    ]

    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return snapshot

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        assert limit >= 12
        return [_chunk(1)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        assert limit >= 4
        return []

    async def fake_chat_completion(
        messages: list[dict[str, str]],
        settings: Settings | None = None,
        *,
        alias: str = "generator_main",
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert messages
        assert response_format == {"type": "json_object"}
        expected_alias, payload = responses.pop(0)
        assert alias == expected_alias
        if isinstance(payload, Exception):
            raise payload
        return {"choices": [{"message": {"content": payload}}]}

    def capture(event: EvalGenerateProgressEvent) -> None:
        events.append((event.event, event.reason, event.question))

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.generator._load_and_validate_smoke_marker", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.generator.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.generator.load_alias_chunks", fake_aliases)
    monkeypatch.setattr(
        "wikipediarag.eval.generator.resolve_generate_runtime_config",
        lambda *_args, **_kwargs: _runtime(
            run_id="run-basic",
            count=1,
            concurrency=1,
            family_targets_override={"single_hop_factual": 1},
        ),
    )
    monkeypatch.setattr("wikipediarag.eval.generator.chat_completion", fake_chat_completion)

    manifest = await generate_dataset(1, settings=Settings(), progress_callback=capture)
    latest = read_json(tmp_path / "eval" / "datasets" / manifest.dataset_name / "latest.json")
    stored_tasks = read_jsonl(Path(manifest.jsonl_path), EvalTask)
    status = load_generate_status("run-basic")
    partial = load_generate_partial_tasks("run-basic")

    assert manifest.task_count == 1
    assert latest["task_count"] == 1
    assert len(stored_tasks) == 1
    assert stored_tasks[0].question == "Какой подтверждённый факт локально указан для «Статья 1»?"
    assert len(partial) == 1
    assert status.state == "completed"
    assert status.stats.accepted == 1
    assert status.accepted_tasks[0].gold_chunk_ids == ["c1"]
    assert [event for event, _reason, _question in events] == [
        "run_started",
        "family_started",
        "attempt_started",
        "candidate_rejected",
        "candidate_rejected",
        "attempt_started",
        "candidate_generated",
        "candidate_rejected",
        "candidate_generated",
        "candidate_rejected",
        "attempt_started",
        "provider_error",
        "attempt_started",
        "candidate_generated",
        "candidate_rejected",
        "attempt_started",
        "candidate_generated",
        "task_accepted",
        "family_completed",
        "run_completed",
    ]
    assert [reason for _event, reason, _question in events if reason] == [
        "invalid_generator_json",
        "invalid_generator_json",
        "verifier_rejected",
        "invalid_verifier_json",
        "provider_error",
        "local_validation_rejected",
    ]
    assert responses == []


@pytest.mark.asyncio
async def test_generate_dataset_supports_custom_aliases_targets_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    alias_calls: list[str] = []

    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return snapshot

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        assert limit >= 240
        return [_chunk(index) for index in range(1, 41)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        return []

    async def fake_chat_completion(
        messages: list[dict[str, str]],
        settings: Settings | None = None,
        *,
        alias: str = "generator_main",
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert messages
        alias_calls.append(alias)
        if alias == "custom_verifier":
            return {"choices": [{"message": {"content": json.dumps({"verdict": "accept"}, ensure_ascii=False)}}]}
        prompt = messages[-1]["content"]
        titles = re.findall(r"title=(.+)", prompt)
        document_ids = re.findall(r"document_id=(.+)", prompt)
        left = titles[0]
        right = titles[1]
        left_doc = document_ids[0]
        right_doc = document_ids[1]
        question = f"Чем отличаются документы {left_doc} и {right_doc} по локальным фактам статей «{left}» и «{right}»?"
        payload = {
            "question": question,
            "reference_answer": f"Подтверждённые различия для {left_doc} и {right_doc}",
            "accepted_answers": [f"Различия для {left_doc}", f"Различия для {right_doc}"],
            "reasoning_path": [left, right],
        }
        return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.generator._load_and_validate_smoke_marker", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.generator.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.generator.load_alias_chunks", fake_aliases)
    monkeypatch.setattr(
        "wikipediarag.eval.generator.resolve_generate_runtime_config",
        lambda *_args, **_kwargs: _runtime(
            run_id="run-comparison",
            count=20,
            concurrency=20,
            generator_alias="custom_generator",
            verifier_alias="custom_verifier",
            family_targets_override={"comparison_multi_hop": 20},
            family_weights_override={
                "single_hop_factual": 0.0,
                "alias_redirect_rare": 0.0,
                "deep_section_fact": 0.0,
                "comparison_multi_hop": 1.0,
                "unanswerable": 0.0,
                "hard_negative": 0.0,
            },
        ),
    )
    monkeypatch.setattr("wikipediarag.eval.generator.chat_completion", fake_chat_completion)

    manifest = await generate_dataset(
        20,
        concurrency=20,
        generator_alias="custom_generator",
        verifier_alias="custom_verifier",
        family_weights={
            "single_hop_factual": 0.0,
            "alias_redirect_rare": 0.0,
            "deep_section_fact": 0.0,
            "comparison_multi_hop": 1.0,
            "unanswerable": 0.0,
            "hard_negative": 0.0,
        },
        settings=Settings(),
    )
    tasks = read_jsonl(Path(manifest.jsonl_path), EvalTask)
    status = load_generate_status("run-comparison")
    partial = load_generate_partial_tasks("run-comparison")

    assert manifest.task_count == 20
    assert manifest.generator_alias == "custom_generator"
    assert manifest.verifier_alias == "custom_verifier"
    assert all(task.task_family == "comparison_multi_hop" for task in tasks)
    assert len(partial) == 20
    assert status.config.concurrency == 20
    assert status.config.generator.alias == "custom_generator"
    assert status.config.verifier.alias == "custom_verifier"
    assert status.stats.accepted == 20
    assert len(status.accepted_tasks) == 20
    assert "custom_generator" in alias_calls
    assert "custom_verifier" in alias_calls


@pytest.mark.asyncio
async def test_generate_dataset_resume_continues_from_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    phase = {"value": "first"}
    runtime = _runtime(
        run_id="resume-run",
        count=2,
        concurrency=1,
        family_targets_override={"single_hop_factual": 2},
    )

    async def fake_snapshot(_settings: Settings) -> CorpusSnapshot:
        return snapshot

    async def fake_candidates(*, limit: int, settings: Settings) -> list[CorpusChunk]:
        return [_chunk(1), _chunk(2)]

    async def fake_aliases(*, limit: int, settings: Settings) -> list[tuple[str, CorpusChunk]]:
        return []

    async def fake_generate_one(
        family: TaskFamily,
        pools: dict[TaskFamily, list[object]],
        attempt: int,
        **_kwargs: object,
    ) -> tuple[int, EvalTask | None]:
        assert family == "single_hop_factual"
        assert "single_hop_factual" in pools
        if phase["value"] == "first":
            if attempt == 1:
                return attempt, _single_hop_task("Какой локальный факт подтверждается для документа pagep1?", 1)
            raise RuntimeError("simulated interruption")
        if attempt == 3:
            return attempt, _single_hop_task("Какой локальный факт подтверждается для документа pagep2?", 2)
        return attempt, None

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.generator._load_and_validate_smoke_marker", fake_snapshot)
    monkeypatch.setattr("wikipediarag.eval.generator.load_candidate_chunks", fake_candidates)
    monkeypatch.setattr("wikipediarag.eval.generator.load_alias_chunks", fake_aliases)
    monkeypatch.setattr(
        "wikipediarag.eval.generator.resolve_generate_runtime_config",
        lambda *_args, **_kwargs: runtime,
    )
    monkeypatch.setattr("wikipediarag.eval.generator._generate_one", fake_generate_one)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        await generate_dataset(2, concurrency=1, run_id="resume-run", settings=Settings())

    failed_status = load_generate_status("resume-run")
    failed_partial = load_generate_partial_tasks("resume-run")
    assert failed_status.state == "failed"
    assert failed_status.stats.accepted == 1
    assert len(failed_partial) == 1

    phase["value"] = "second"
    manifest = await generate_dataset(resume_run_id="resume-run", settings=Settings())
    completed_status = load_generate_status("resume-run")
    completed_tasks = read_jsonl(Path(manifest.jsonl_path), EvalTask)

    assert manifest.task_count == 2
    assert completed_status.state == "completed"
    assert completed_status.stats.accepted == 2
    assert len(completed_tasks) == 2


@pytest.mark.asyncio
async def test_generate_family_refills_slot_when_attempt_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    runtime = _runtime(
        run_id="backfill-run",
        count=4,
        concurrency=2,
        family_targets_override={"single_hop_factual": 4},
    )
    tracker = _GenerateProgressTracker(runtime=runtime, snapshot=snapshot)
    active = 0
    max_active = 0
    started: dict[int, float] = {}
    finished: dict[int, float] = {}
    questions = {
        1: "Какой контрольный параметр указан для северного сценария?",
        2: "Какая проверочная метка относится к южному примеру?",
        3: "Какой отдельный индикатор описывает западный образец?",
        4: "Какая уникальная характеристика дана для восточной записи?",
    }

    async def fake_generate_one(
        family: TaskFamily,
        pools: dict[TaskFamily, list[object]],
        attempt: int,
        **_kwargs: object,
    ) -> tuple[int, EvalTask | None]:
        nonlocal active, max_active
        assert family == "single_hop_factual"
        assert pools["single_hop_factual"]
        active += 1
        max_active = max(max_active, active)
        started[attempt] = time.perf_counter()
        await asyncio.sleep(0.2 if attempt == 1 else 0.05)
        finished[attempt] = time.perf_counter()
        active -= 1
        return attempt, _single_hop_task(questions[attempt], attempt)

    _patch_artifact_roots(monkeypatch, tmp_path)
    monkeypatch.setattr("wikipediarag.eval.generator._generate_one", fake_generate_one)

    tasks: list[EvalTask] = []
    await _generate_family(
        "single_hop_factual",
        4,
        {"single_hop_factual": [object()]},
        tasks,
        [],
        snapshot=snapshot,
        runtime=runtime,
        settings=Settings(),
        tracker=tracker,
    )

    assert len(tasks) == 4
    assert max_active == 2
    assert started[3] < finished[1]
