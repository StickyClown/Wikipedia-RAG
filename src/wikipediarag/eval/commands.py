from __future__ import annotations

import asyncio
from math import ceil
from pathlib import Path
from typing import Any, Literal, cast

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.api_client import EvalApiClient, HttpEvalApiClient, RetrievalEvalApiClient
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    DATASET_NAME,
    load_latest_dataset,
    read_jsonl,
    utc_now_iso,
    write_json,
    write_jsonl,
)
from wikipediarag.eval.corpus import load_candidate_chunks, load_corpus_snapshot
from wikipediarag.eval.diagnostics import answer_result_diagnosis, retrieval_result_diagnosis
from wikipediarag.eval.external import transfer_miracl_ru, transfer_miracl_ru_from_huggingface
from wikipediarag.eval.generate_runs import load_generate_status, load_latest_generate_status
from wikipediarag.eval.generator import SMOKE_MARKER, build_smoke_tasks, generate_dataset
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.metrics import percentile
from wikipediarag.eval.progress import EvalGenerateProgressCallback
from wikipediarag.eval.reporting import load_latest_run, write_report
from wikipediarag.eval.retrieval_reporting import write_retrieval_report
from wikipediarag.eval.retrieval_runner import (
    RetrievalProgressCallback,
    load_latest_retrieval_status,
    load_retrieval_status,
    run_retrieval_suite,
    run_retrieval_task,
)
from wikipediarag.eval.review import eval_release_gate as run_reviewed_release_gate
from wikipediarag.eval.review import (
    freeze_reviewed_suite,
    load_locked_split_manifest,
    load_locked_split_tasks,
    load_release_gate_status,
    write_review_pool,
)
from wikipediarag.eval.runner import _eval_overrides, eval_configs, run_suite, run_task
from wikipediarag.eval.schemas import (
    CandidateRef,
    EvalConfig,
    EvalDatasetManifest,
    EvalGenerateRunStatus,
    EvalTask,
    EvalTaskResult,
    ReleaseGateStatus,
    RetrievalEvalStatus,
    RetrievalTaskResult,
    TaskFamily,
)
from wikipediarag.eval.settings import adapt_eval_settings
from wikipediarag.eval.trusted import (
    TRUSTED_DATASET_NAME,
    TrustedFamily,
    TrustedGenerateRunStatus,
    generate_trusted_dataset,
    load_latest_trusted_status,
    load_trusted_status,
    pool_trusted_dataset,
    write_trusted_catalog,
    write_trusted_report,
)
from wikipediarag.retrieval_profile import get_retrieval_profile


async def eval_smoke(
    *,
    count: int,
    api: str,
    settings: Settings | None = None,
    client: EvalApiClient | None = None,
) -> dict[str, Any]:
    resolved = adapt_eval_settings(settings or get_settings())
    snapshot = await load_corpus_snapshot(resolved)
    chunks = await load_candidate_chunks(limit=max(200, count * 20), settings=resolved)
    tasks = build_smoke_tasks(chunks, snapshot, count=count)
    profile = get_retrieval_profile(snapshot.retrieval_profile, resolved)
    overrides = _eval_overrides({"postprocess": {"extended_search": "off"}})
    config_hash = stable_json_hash({"profile": snapshot.retrieval_profile, "overrides": overrides, "mode": "normal"})
    config = EvalConfig(
        config_id="eval_smoke",
        retrieval_profile=snapshot.retrieval_profile,
        retrieval_overrides=overrides,
        mode="normal",
        config_hash=config_hash,
        model_aliases=profile.model_aliases.model_dump(),
    )
    manifest = EvalDatasetManifest(
        dataset_name="eval-smoke",
        dataset_version="2026.07.1",
        dataset_hash=stable_json_hash([task.model_dump(mode="json") for task in tasks]),
        task_count=len(tasks),
        created_at=utc_now_iso(),
        snapshot_id=snapshot.snapshot_id,
        index_version=snapshot.index_version,
        zim_checksum=snapshot.zim_checksum,
        retrieval_profile_hash=snapshot.retrieval_profile_hash,
        generator_alias="deterministic_smoke",
        verifier_alias="deterministic_smoke",
        jsonl_path=str(ARTIFACT_ROOT / "smoke" / "latest-tasks.jsonl"),
    )
    write_jsonl(Path(manifest.jsonl_path), tasks)
    api_client = client or HttpEvalApiClient.from_settings(resolved)
    results: list[EvalTaskResult] = []
    for task in tasks:
        results.append(await run_task(task, config, api=api, manifest=manifest, client=api_client, settings=resolved))
    page_hits = sum(1 for result in results if result.scores and result.scores.page_recall["10"] >= 1.0)
    chunk_hits = sum(1 for result in results if result.scores and result.scores.chunk_recall["20"] >= 1.0)
    citation_ok = all(result.citations and len(result.cited_chunk_ids) == len(result.citations) for result in results)
    kiwix_ok = all(result.scores and result.scores.kiwix_url_ok >= 1.0 for result in results)
    page_threshold = ceil(0.8 * len(tasks))
    chunk_threshold = ceil(0.7 * len(tasks))
    report = {
        "suite": "eval-smoke",
        "count": len(tasks),
        "snapshot_id": snapshot.snapshot_id,
        "index_version": snapshot.index_version,
        "zim_checksum": snapshot.zim_checksum,
        "retrieval_profile_hash": snapshot.retrieval_profile_hash,
        "page_recall_top10_hits": page_hits,
        "chunk_recall_top20_hits": chunk_hits,
        "citation_ok": citation_ok,
        "kiwix_url_ok": kiwix_ok,
        "page_recall_top10_threshold": page_threshold,
        "chunk_recall_top20_threshold": chunk_threshold,
        "passed": page_hits >= page_threshold and chunk_hits >= chunk_threshold and citation_ok and kiwix_ok,
        "cases": [
            {
                "task_id": result.task_id,
                "question": result.question,
                "page_top10": bool(result.scores and result.scores.page_recall["10"]),
                "chunk_top20": bool(result.scores and result.scores.chunk_recall["20"]),
                "citations": result.citations,
                "cited_chunk_ids": result.cited_chunk_ids,
                "kiwix_url_ok": bool(result.scores and result.scores.kiwix_url_ok),
                "errors": result.errors,
            }
            for result in results
        ],
    }
    smoke_dir = ARTIFACT_ROOT / "smoke"
    write_json(smoke_dir / "latest-report.json", report)
    write_jsonl(smoke_dir / "latest-results.jsonl", results)
    if report["passed"]:
        write_json(SMOKE_MARKER, report)
    return report


async def eval_generate(
    count: int | None = None,
    *,
    concurrency: int | None = None,
    generator_alias: str | None = None,
    verifier_alias: str | None = None,
    family_weights: dict[TaskFamily, float] | None = None,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    settings: Settings | None = None,
    progress_callback: EvalGenerateProgressCallback | None = None,
) -> EvalDatasetManifest:
    return await generate_dataset(
        count,
        concurrency=concurrency,
        generator_alias=generator_alias,
        verifier_alias=verifier_alias,
        family_weights=family_weights,
        run_id=run_id,
        resume_run_id=resume_run_id,
        settings=adapt_eval_settings(settings or get_settings()),
        progress_callback=progress_callback,
    )


async def eval_run(
    *,
    suite: str = DATASET_NAME,
    api: str,
    batch_size: int = 6,
    settings: Settings | None = None,
    client: EvalApiClient | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    manifest, tasks = load_latest_dataset(suite)
    run_manifest = await run_suite(
        manifest,
        tasks,
        api=api,
        batch_size=batch_size,
        settings=adapt_eval_settings(settings or get_settings()),
        client=client,
        progress_callback=progress_callback,
    )
    return run_manifest.model_dump(mode="json")


def eval_report_latest() -> dict[str, str]:
    run_manifest = load_latest_run()
    md_path, json_path = write_report(run_manifest)
    return {"markdown": str(md_path), "json": str(json_path), "run_id": run_manifest.run_id}


def eval_generate_status(*, run_id: str | None = None, latest: bool = False) -> EvalGenerateRunStatus:
    if latest:
        return load_latest_generate_status()
    if run_id is None:
        raise ValueError("eval-generate-status requires --run-id or --latest")
    return load_generate_status(run_id)


async def eval_retrieval_run(
    *,
    suite: str = DATASET_NAME,
    api: str,
    batch_size: int = 10,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    settings: Settings | None = None,
    client: RetrievalEvalApiClient | None = None,
    progress_callback: RetrievalProgressCallback | None = None,
) -> dict[str, Any]:
    manifest, tasks = load_latest_dataset(suite)
    run_manifest = await run_retrieval_suite(
        manifest,
        tasks,
        api=api,
        batch_size=batch_size,
        run_id=run_id,
        resume_run_id=resume_run_id,
        rerun_failed=rerun_failed,
        settings=adapt_eval_settings(settings or get_settings()),
        client=client,
        progress_callback=progress_callback,
    )
    return run_manifest.model_dump(mode="json")


def eval_retrieval_status(*, run_id: str | None = None, latest: bool = False) -> RetrievalEvalStatus:
    if latest:
        return load_latest_retrieval_status()
    if run_id is None:
        raise ValueError("eval-retrieval-status requires --run-id or --latest")
    return load_retrieval_status(run_id)


def eval_retrieval_report_latest() -> dict[str, str]:
    md_path, json_path = write_retrieval_report()
    return {"markdown": str(md_path), "json": str(json_path)}


async def eval_full(
    *,
    count: int | None,
    api: str,
    concurrency: int | None = None,
    generator_alias: str | None = None,
    verifier_alias: str | None = None,
    family_weights: dict[TaskFamily, float] | None = None,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    settings: Settings | None = None,
    progress_callback: EvalGenerateProgressCallback | None = None,
) -> dict[str, Any]:
    resolved = adapt_eval_settings(settings or get_settings())
    smoke = await eval_smoke(count=10, api=api, settings=resolved)
    if not smoke["passed"]:
        return {"passed": False, "stage": "smoke", "smoke": smoke}
    dataset = await eval_generate(
        count,
        concurrency=concurrency,
        generator_alias=generator_alias,
        verifier_alias=verifier_alias,
        family_weights=family_weights,
        run_id=run_id,
        resume_run_id=resume_run_id,
        settings=resolved,
        progress_callback=progress_callback,
    )
    run = await eval_run(suite=dataset.dataset_name, api=api, batch_size=6, settings=resolved)
    report = eval_report_latest()
    return {"passed": True, "smoke": smoke, "dataset": dataset.model_dump(mode="json"), "run": run, "report": report}


async def eval_trusted_catalog(*, settings: Settings | None = None) -> dict[str, Any]:
    manifest = await write_trusted_catalog(settings=adapt_eval_settings(settings or get_settings()))
    return manifest.model_dump(mode="json")


async def eval_trusted_generate(
    *,
    count: int | None = None,
    concurrency: int | None = None,
    rejection_budget: int = 30,
    generator_alias: str | None = None,
    verifier_alias: str | None = None,
    family_weights: dict[TrustedFamily, float] | None = None,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    takeover_stale_run: bool = False,
    settings: Settings | None = None,
    progress_callback: Any | None = None,
) -> EvalDatasetManifest:
    return await generate_trusted_dataset(
        count=count,
        concurrency=concurrency,
        rejection_budget=rejection_budget,
        generator_alias=generator_alias,
        verifier_alias=verifier_alias,
        family_weights=family_weights,
        run_id=run_id,
        resume_run_id=resume_run_id,
        takeover_stale_run=takeover_stale_run,
        settings=adapt_eval_settings(settings or get_settings()),
        progress_callback=progress_callback,
    )


def eval_trusted_status(*, run_id: str | None = None, latest: bool = False) -> TrustedGenerateRunStatus:
    if latest:
        return load_latest_trusted_status()
    if run_id is None:
        raise ValueError("eval-trusted-status requires --run-id or --latest")
    return load_trusted_status(run_id)


async def eval_trusted_pool(
    *,
    suite: str = TRUSTED_DATASET_NAME,
    api: str,
    batch_size: int = 10,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    settings: Settings | None = None,
    progress_callback: RetrievalProgressCallback | None = None,
) -> dict[str, Any]:
    return await pool_trusted_dataset(
        suite=suite,
        api=api,
        batch_size=batch_size,
        run_id=run_id,
        resume_run_id=resume_run_id,
        rerun_failed=rerun_failed,
        settings=adapt_eval_settings(settings or get_settings()),
        progress_callback=progress_callback,
    )


def eval_trusted_report(*, suite: str = TRUSTED_DATASET_NAME) -> dict[str, str]:
    return write_trusted_report(suite=suite)


async def eval_miracl_map(
    *,
    input_path: Path | None = None,
    from_huggingface: bool = False,
    split: str = "dev",
    limit: int = 100,
    output_suite: str = "miracl-ru-local-v1",
    cache_dir: Path | None = None,
    min_text_overlap: float = 0.08,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = adapt_eval_settings(settings or get_settings())
    if from_huggingface:
        if split not in {"dev", "train"}:
            raise ValueError("--split must be dev or train")
        return await transfer_miracl_ru_from_huggingface(
            split=cast(Literal["dev", "train"], split),
            limit=limit,
            output_suite=output_suite,
            cache_dir=cache_dir,
            min_text_overlap=min_text_overlap,
            settings=resolved,
        )
    if input_path is None:
        raise ValueError("eval-miracl-map requires --input unless --from-huggingface is used")
    return await transfer_miracl_ru(input_path=input_path, settings=resolved)


def eval_review_candidates(*, input_path: Path, output_suite: str) -> dict[str, Any]:
    manifest = write_review_pool(input_path=input_path, output_suite=output_suite)
    return manifest.model_dump(mode="json")


def eval_freeze_reviewed(*, suite: str, dev_count: int, test_count: int) -> dict[str, Any]:
    result = freeze_reviewed_suite(suite=suite, dev_count=dev_count, test_count=test_count)
    return result.model_dump(mode="json")


async def eval_release_gate(
    *,
    suite: str,
    api: str,
    settings: Settings | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    return await run_reviewed_release_gate(
        suite=suite,
        api=api,
        settings=adapt_eval_settings(settings or get_settings()),
        progress_callback=progress_callback,
    )


def eval_release_gate_status(*, suite: str, report_id: str | None = None) -> ReleaseGateStatus:
    return load_release_gate_status(suite, report_id=report_id)


async def eval_reviewed_short(
    *,
    suite: str,
    split: str,
    task_ids: list[str],
    api: str,
    config_id: str = "sota_mvp_normal",
    batch_size: int = 6,
    retrieval_batch_size: int = 10,
    run_answer: bool = True,
    run_retrieval: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if split not in {"dev", "test"}:
        raise ValueError("--split must be dev or test")
    if not task_ids:
        raise ValueError("at least one --task-id is required")
    if not run_answer and not run_retrieval:
        raise ValueError("at least one of answer or retrieval must be enabled")
    if batch_size < 1 or retrieval_batch_size < 1:
        raise ValueError("batch sizes must be >= 1")
    resolved = adapt_eval_settings(settings or get_settings())
    locked_split = cast(Literal["dev", "test"], split)
    manifest, _payload = load_locked_split_manifest(suite, locked_split)
    tasks = load_locked_split_tasks(manifest, locked_split)
    task_by_id = {task.task_id: task for task in tasks}
    missing = [task_id for task_id in task_ids if task_id not in task_by_id]
    selected = [task_by_id[task_id] for task_id in task_ids if task_id in task_by_id]
    if not selected:
        raise ValueError(f"none of the requested task IDs exist in {suite}/{split}: {missing}")
    config = _single_eval_config(config_id, resolved)
    client = HttpEvalApiClient.from_settings(resolved)
    answer_results: list[EvalTaskResult] = []
    retrieval_results: list[RetrievalTaskResult] = []
    if run_answer:
        answer_results = await _run_short_answer_tasks(
            selected,
            config,
            api=api,
            manifest=manifest,
            client=client,
            settings=resolved,
            batch_size=batch_size,
        )
    if run_retrieval:
        retrieval_results = await _run_short_retrieval_tasks(
            selected,
            config,
            api=api,
            manifest=manifest,
            client=client,
            settings=resolved,
            batch_size=retrieval_batch_size,
        )
    return {
        "suite": suite,
        "split": split,
        "config_id": config_id,
        "task_ids": [task.task_id for task in selected],
        "missing_task_ids": missing,
        "batch_size": batch_size,
        "retrieval_batch_size": retrieval_batch_size,
        "answer": [_short_answer_payload(result) for result in answer_results],
        "retrieval": [_short_retrieval_payload(result) for result in retrieval_results],
    }


async def eval_profile_retrieval(
    *,
    suite: str,
    split: str = "dev",
    api: str,
    config_id: str = "sota_mvp_normal",
    task_ids: list[str] | None = None,
    limit: int = 5,
    warmup_iterations: int = 1,
    measured_iterations: int = 1,
    batch_size: int = 5,
    settings: Settings | None = None,
    client: RetrievalEvalApiClient | None = None,
) -> dict[str, Any]:
    if split not in {"dev", "test"}:
        raise ValueError("--split must be dev or test")
    if limit < 1:
        raise ValueError("--limit must be >= 1")
    if warmup_iterations < 0:
        raise ValueError("--warmup-iterations must be >= 0")
    if measured_iterations < 1:
        raise ValueError("--measured-iterations must be >= 1")
    if batch_size < 1:
        raise ValueError("--batch-size must be >= 1")
    resolved = adapt_eval_settings(settings or get_settings())
    locked_split = cast(Literal["dev", "test"], split)
    manifest, _payload = load_locked_split_manifest(suite, locked_split)
    tasks = _profile_tasks(load_locked_split_tasks(manifest, locked_split), task_ids=task_ids, limit=limit)
    config = _single_eval_config(config_id, resolved)
    api_client = client or HttpEvalApiClient.from_settings(resolved, include_kiwix_urls=False)
    started_at = utc_now_iso()
    run_id = (
        f"{_compact_timestamp(started_at)}-{suite}-{split}-retrieval-profile-"
        f"{stable_json_hash([task.task_id for task in tasks])[:8]}"
    )
    run_dir = ARTIFACT_ROOT / "retrieval-profiles" / suite / run_id
    warmup_results: list[RetrievalTaskResult] = []
    measured_results: list[RetrievalTaskResult] = []
    for iteration in range(1, warmup_iterations + 1):
        warmup_results.extend(
            await _run_profile_iteration(
                tasks,
                config,
                api=api,
                manifest=manifest,
                client=api_client,
                settings=resolved,
                batch_size=batch_size,
                iteration=iteration,
            )
        )
        _write_retrieval_profile_status(run_dir, run_id, "warmup", started_at, warmup_results, measured_results)
    for iteration in range(1, measured_iterations + 1):
        measured_results.extend(
            await _run_profile_iteration(
                tasks,
                config,
                api=api,
                manifest=manifest,
                client=api_client,
                settings=resolved,
                batch_size=batch_size,
                iteration=iteration,
            )
        )
        _write_retrieval_profile_status(run_dir, run_id, "measuring", started_at, warmup_results, measured_results)
    report = {
        "run_id": run_id,
        "suite": suite,
        "split": split,
        "api": api,
        "config_id": config.config_id,
        "config_hash": config.config_hash,
        "retrieval_profile": config.retrieval_profile,
        "task_ids": [task.task_id for task in tasks],
        "warmup_iterations": warmup_iterations,
        "measured_iterations": measured_iterations,
        "batch_size": batch_size,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "run_dir": str(run_dir),
        "warmup": _profile_result_counts(warmup_results),
        "measured": _profile_result_counts(measured_results),
        "stage_latency": _profile_stage_latency(measured_results),
        "failed_task_ids": sorted({result.task_id for result in measured_results if result.status != "completed"}),
        "errors": [error for result in measured_results for error in result.errors],
    }
    write_json(run_dir / "report.json", report)
    write_json(ARTIFACT_ROOT / "retrieval-profiles" / suite / "latest.json", report)
    _write_retrieval_profile_status(run_dir, run_id, "completed", started_at, warmup_results, measured_results)
    return report


def _single_eval_config(config_id: str, settings: Settings) -> EvalConfig:
    configs = {config.config_id: config for config in eval_configs(settings)}
    if config_id not in configs:
        raise ValueError(f"unknown eval config: {config_id}")
    return configs[config_id]


def _profile_tasks(tasks: list[EvalTask], *, task_ids: list[str] | None, limit: int) -> list[EvalTask]:
    if task_ids:
        by_id = {task.task_id: task for task in tasks}
        missing = [task_id for task_id in task_ids if task_id not in by_id]
        if missing:
            raise ValueError(f"unknown task IDs for profiling: {missing}")
        selected = [by_id[task_id] for task_id in task_ids]
    else:
        selected = tasks[:limit]
    if not selected:
        raise ValueError("no profiling tasks selected")
    return selected


async def _run_profile_iteration(
    tasks: list[EvalTask],
    config: EvalConfig,
    *,
    api: str,
    manifest: EvalDatasetManifest,
    client: RetrievalEvalApiClient,
    settings: Settings,
    batch_size: int,
    iteration: int,
) -> list[RetrievalTaskResult]:
    semaphore = asyncio.Semaphore(batch_size)

    async def run_one(index: int, task: EvalTask) -> RetrievalTaskResult:
        async with semaphore:
            return await run_retrieval_task(
                task,
                config,
                api=api,
                manifest=manifest,
                client=client,
                settings=settings,
                batch_index=iteration,
                task_index=index,
            )

    return list(await asyncio.gather(*(run_one(index, task) for index, task in enumerate(tasks, start=1))))


def _profile_result_counts(results: list[RetrievalTaskResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "completed": sum(1 for result in results if result.status == "completed"),
        "failed": sum(1 for result in results if result.status != "completed"),
    }


def _profile_stage_latency(results: list[RetrievalTaskResult]) -> dict[str, dict[str, float]]:
    completed = [result for result in results if result.status == "completed"]
    preferred = [
        "bm25",
        "dense_embedding",
        "dense_search",
        "dense_total",
        "fusion",
        "rerank",
        "context",
        "retrieval_total",
        "retrieval",
        "total",
    ]
    keys = sorted({key for result in completed for key in result.latency_ms})
    ordered_keys = [key for key in preferred if key in keys] + [key for key in keys if key not in preferred]
    profile: dict[str, dict[str, float]] = {}
    for key in ordered_keys:
        values = [float(result.latency_ms[key]) for result in completed if key in result.latency_ms]
        profile[key] = {
            "count": float(len(values)),
            "p50_ms": percentile(values, 50),
            "p95_ms": percentile(values, 95),
            "max_ms": max(values) if values else 0.0,
        }
    return profile


def _compact_timestamp(value: str) -> str:
    return value.replace("-", "").replace(":", "")


def _write_retrieval_profile_status(
    run_dir: Path,
    run_id: str,
    phase: str,
    started_at: str,
    warmup_results: list[RetrievalTaskResult],
    measured_results: list[RetrievalTaskResult],
) -> None:
    status = {
        "run_id": run_id,
        "state": "completed" if phase == "completed" else "running",
        "phase": phase,
        "started_at": started_at,
        "updated_at": utc_now_iso(),
        "run_dir": str(run_dir),
        "warmup": _profile_result_counts(warmup_results),
        "measured": _profile_result_counts(measured_results),
    }
    write_json(run_dir / "status.json", status)
    write_json(ARTIFACT_ROOT / "retrieval-profiles" / "latest-status.json", status)


async def _run_short_answer_tasks(
    tasks: list[EvalTask],
    config: EvalConfig,
    *,
    api: str,
    manifest: EvalDatasetManifest,
    client: HttpEvalApiClient,
    settings: Settings,
    batch_size: int,
) -> list[EvalTaskResult]:
    semaphore = asyncio.Semaphore(batch_size)

    async def run_one(task: EvalTask) -> EvalTaskResult:
        async with semaphore:
            return await run_task(task, config, api=api, manifest=manifest, client=client, settings=settings)

    return list(await asyncio.gather(*(run_one(task) for task in tasks)))


async def _run_short_retrieval_tasks(
    tasks: list[EvalTask],
    config: EvalConfig,
    *,
    api: str,
    manifest: EvalDatasetManifest,
    client: HttpEvalApiClient,
    settings: Settings,
    batch_size: int,
) -> list[RetrievalTaskResult]:
    semaphore = asyncio.Semaphore(batch_size)

    async def run_one(index: int, task: EvalTask) -> RetrievalTaskResult:
        async with semaphore:
            return await run_retrieval_task(
                task,
                config,
                api=api,
                manifest=manifest,
                client=client,
                settings=settings,
                batch_index=1,
                task_index=index,
            )

    return list(await asyncio.gather(*(run_one(index, task) for index, task in enumerate(tasks, start=1))))


def _short_answer_payload(result: EvalTaskResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "status": result.status,
        "answer": result.answer,
        "citations": result.citations,
        "cited_chunk_ids": result.cited_chunk_ids,
        "answerability_status": result.usage.get("answerability_status"),
        "insufficient_evidence": result.usage.get("insufficient_evidence"),
        "scores": result.scores.model_dump(mode="json") if result.scores else None,
        "diagnosis": result.diagnosis,
        "timings_ms": result.latency_ms,
        "query_run_id": result.query_run_id,
        "trace_id": result.trace_id,
        "errors": result.errors,
    }


def _short_retrieval_payload(result: RetrievalTaskResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "status": result.status,
        "scores": result.scores.model_dump(mode="json") if result.scores else None,
        "diagnosis": result.diagnosis,
        "timings_ms": result.latency_ms,
        "trace_id": result.trace_id,
        "top_candidates": [
            {
                "rank": candidate.rank,
                "title": candidate.title,
                "document_id": candidate.document_id,
                "chunk_id": candidate.chunk_id,
                "scores": candidate.scores,
            }
            for candidate in result.final_candidates[:5]
        ],
        "errors": result.errors,
    }


def eval_task_diagnostics(
    *,
    suite: str,
    split: str,
    task_ids: list[str],
    config_id: str = "sota_mvp_normal",
) -> dict[str, Any]:
    if split not in {"dev", "test"}:
        raise ValueError("--split must be dev or test")
    if not task_ids:
        raise ValueError("at least one --task-id is required")
    locked_split = cast(Literal["dev", "test"], split)
    manifest, _payload = load_locked_split_manifest(suite, locked_split)
    tasks = load_locked_split_tasks(manifest, locked_split)
    task_by_id = {task.task_id: task for task in tasks}
    answer_results = _latest_answer_results(manifest, config_id)
    retrieval_results = _latest_retrieval_results(manifest, config_id)
    missing = [task_id for task_id in task_ids if task_id not in task_by_id]
    return {
        "suite": suite,
        "split": split,
        "config_id": config_id,
        "missing_task_ids": missing,
        "tasks": [
            _diagnostic_task_payload(
                task_by_id[task_id],
                answer=answer_results.get(task_id),
                retrieval=retrieval_results.get(task_id),
            )
            for task_id in task_ids
            if task_id in task_by_id
        ],
    }


def _latest_answer_results(manifest: EvalDatasetManifest, config_id: str) -> dict[str, EvalTaskResult]:
    run_dir = ARTIFACT_ROOT / "runs" / manifest.dataset_name / f"{manifest.dataset_name}-{manifest.dataset_hash[:12]}"
    rows: list[EvalTaskResult] = []
    for path in _sorted_result_paths(run_dir / "results", config_id):
        rows.extend(read_jsonl(path, EvalTaskResult))
    return {row.task_id: row for row in rows}


def _latest_retrieval_results(manifest: EvalDatasetManifest, config_id: str) -> dict[str, RetrievalTaskResult]:
    suite_dir = ARTIFACT_ROOT / "retrieval-runs" / manifest.dataset_name
    rows: list[RetrievalTaskResult] = []
    for path in sorted(
        suite_dir.glob(f"*/results/{config_id}-*.jsonl"),
        key=lambda item: item.stat().st_mtime,
    ):
        rows.extend(read_jsonl(path, RetrievalTaskResult))
    return {row.task_id: row for row in rows}


def _sorted_result_paths(results_dir: Path, config_id: str) -> list[Path]:
    return sorted(results_dir.glob(f"{config_id}-*.jsonl"), key=lambda item: item.stat().st_mtime)


def _diagnostic_task_payload(
    task: EvalTask,
    *,
    answer: EvalTaskResult | None,
    retrieval: RetrievalTaskResult | None,
) -> dict[str, Any]:
    cited_chunks = set(answer.cited_chunk_ids if answer else [])
    hard_negative_pages = set(task.hard_negative_page_ids)
    answer_diagnosis = answer_result_diagnosis(task, answer)
    retrieval_diagnosis = retrieval_result_diagnosis(task, retrieval)
    primary_diagnosis = answer_diagnosis if answer is not None else retrieval_diagnosis
    return {
        "task_id": task.task_id,
        "task_family": task.task_family,
        "unanswerable": task.unanswerable,
        "question": task.question,
        "diagnosis": {
            "root_cause": primary_diagnosis.get("root_cause", "not_evaluated"),
            "answer": answer_diagnosis,
            "retrieval": retrieval_diagnosis,
        },
        "expected": {
            "reference_answer": task.reference_answer,
            "accepted_answers": task.accepted_answers,
            "gold_page_ids": task.gold_page_ids,
            "gold_chunk_ids": task.gold_chunk_ids,
            "gold_evidence": [item.model_dump(mode="json") for item in task.gold_evidence],
        },
        "answer": None
        if answer is None
        else {
            "status": answer.status,
            "answer": answer.answer,
            "citations": answer.citations,
            "cited_chunk_ids": answer.cited_chunk_ids,
            "answerability_status": answer.usage.get("answerability_status"),
            "insufficient_evidence": answer.usage.get("insufficient_evidence"),
            "scores": answer.scores.model_dump(mode="json") if answer.scores else None,
            "diagnosis": answer_diagnosis,
            "timings_ms": answer.latency_ms,
            "query_run_id": answer.query_run_id,
            "trace_id": answer.trace_id,
            "top_candidates": [
                _candidate_payload(candidate, hard_negative_pages=hard_negative_pages, cited_chunks=cited_chunks)
                for candidate in answer.reranked_candidates[:10]
            ],
            "errors": answer.errors,
        },
        "retrieval": None
        if retrieval is None
        else {
            "status": retrieval.status,
            "scores": retrieval.scores.model_dump(mode="json") if retrieval.scores else None,
            "diagnosis": retrieval_diagnosis,
            "timings_ms": retrieval.latency_ms,
            "trace_id": retrieval.trace_id,
            "top_candidates": [
                _candidate_payload(candidate, hard_negative_pages=hard_negative_pages, cited_chunks=cited_chunks)
                for candidate in retrieval.final_candidates[:10]
            ],
            "errors": retrieval.errors,
        },
        "hard_negative_page_ids": task.hard_negative_page_ids,
    }


def _candidate_payload(
    candidate: CandidateRef,
    *,
    hard_negative_pages: set[str],
    cited_chunks: set[str],
) -> dict[str, Any]:
    return {
        "rank": candidate.rank,
        "title": candidate.title,
        "document_id": candidate.document_id,
        "section_id": candidate.section_id,
        "chunk_id": candidate.chunk_id,
        "scores": candidate.scores,
        "hard_negative": candidate.document_id in hard_negative_pages,
        "cited": candidate.chunk_id in cited_chunks,
        "source_url": candidate.source_url,
    }
