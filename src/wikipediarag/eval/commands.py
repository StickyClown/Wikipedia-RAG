from __future__ import annotations

import asyncio
import time
from math import ceil
from pathlib import Path
from typing import Any, Literal, cast

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.api_client import EvalApiClient, HttpEvalApiClient, RetrievalEvalApiClient
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    DATASET_NAME,
    append_jsonl,
    load_latest_dataset,
    read_json,
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
from wikipediarag.eval.quality import (
    QUALITY_SUITE,
    QualitySuiteError,
    apply_quality_review,
    build_quality_report,
    freeze_quality_suite,
    ingest_quality_suite,
    prepare_quality_suite,
    validate_quality_suite,
)
from wikipediarag.eval.quality_fixture import build_quality_fixture
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
from wikipediarag.eval.source_binding import RuntimeBindingError, bind_runtime_tasks, verify_binding
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


def eval_quality_prepare(*, corpus_dir: Path, strict_counts: bool = True) -> dict[str, Any]:
    """Validate and immutably register the local P0.1 corpus."""

    manifest = prepare_quality_suite(corpus_dir, strict_counts=strict_counts)
    return manifest.model_dump(mode="json")


def eval_quality_scaffold(*, corpus_dir: Path, overwrite: bool = False) -> dict[str, object]:
    return build_quality_fixture(corpus_dir, overwrite=overwrite)


def eval_quality_review(*, corpus_dir: Path, decisions_path: Path | None = None) -> dict[str, Any]:
    """Return a content-free checklist for rows still needing review."""

    if decisions_path is not None:
        return apply_quality_review(corpus_dir, decisions_path)

    suite = validate_quality_suite(corpus_dir, strict_counts=False, require_reviewed=False)
    scope_families = {"partial", "conflicting", "not_found_in_scope", "freshness"}
    pending = [
        task.task_id
        for task in suite.tasks
        if not task.reviewed_by
        or not task.reviewed_at
        or (task.task_family in scope_families and not task.scope_review.reviewed)
    ]
    return {
        "suite": QUALITY_SUITE,
        "task_count": len(suite.tasks),
        "reviewed_count": len(suite.tasks) - len(pending),
        "pending_task_ids": pending,
        "manifest_path": str(suite.task_manifest_path),
    }


def eval_quality_freeze(*, corpus_dir: Path) -> dict[str, Any]:
    return freeze_quality_suite(corpus_dir)


def eval_quality_ingest(
    *,
    corpus_dir: Path,
    api: str,
    batch_size: int = 5,
    upload_concurrency: int = 2,
    timeout_seconds: int = 900,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return ingest_quality_suite(
        corpus_dir,
        api_url=api,
        batch_size=batch_size,
        upload_concurrency=upload_concurrency,
        timeout_seconds=timeout_seconds,
        run_id=run_id,
        resume_run_id=resume_run_id,
        rerun_failed=rerun_failed,
        settings=settings,
    )


def eval_quality_status(*, corpus_dir: Path, run_id: str | None = None) -> dict[str, Any]:
    try:
        suite = validate_quality_suite(corpus_dir, strict_counts=False, require_reviewed=False)
    except QualitySuiteError as exc:
        return {"suite": QUALITY_SUITE, "state": "invalid", "code": exc.code}
    report: dict[str, Any] = {
        "suite": QUALITY_SUITE,
        "state": "ready" if len(suite.tasks) == 220 else "incomplete",
        "task_count": len(suite.tasks),
        "dataset_hash": suite.dataset_hash,
        "source_count": len(suite.sources),
        "manifest_path": str(suite.corpus_dir / "manifest.json"),
    }
    status_root = ARTIFACT_ROOT / "quality" / QUALITY_SUITE
    status_path = status_root / str(run_id) / "status.json" if run_id else None
    if status_path is None:
        candidates = [path for path in status_root.glob("*/status.json") if path.is_file()]
        status_path = max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    if status_path and status_path.exists():
        report["run_status"] = read_json(status_path)
    return report


def eval_quality_report(*, corpus_dir: Path, results_path: Path | None = None) -> dict[str, Any]:
    suite = validate_quality_suite(corpus_dir, strict_counts=False, require_reviewed=False)
    path = results_path or _latest_quality_answer_path(suite.corpus_dir)
    if not path.exists():
        raise FileNotFoundError(f"quality results are missing: {path}")
    results = read_jsonl(path, EvalTaskResult)
    retrieval_path = path.with_name("retrieval.jsonl")
    retrieval_results = read_jsonl(retrieval_path, RetrievalTaskResult) if retrieval_path.exists() else []
    report = build_quality_report(
        suite.tasks,
        results,
        retrieval_results=retrieval_results,
        source_by_id={source.source_id: source for source in suite.sources},
    )
    report["dataset_hash"] = suite.dataset_hash
    report["source_manifest_hash"] = suite.source_hash
    report["answer_results_path"] = str(path)
    if retrieval_results:
        report["retrieval_results_path"] = str(retrieval_path)
    report_path = ARTIFACT_ROOT / "quality" / QUALITY_SUITE / "latest.json"
    write_json(report_path, report)
    return {"report_path": str(report_path), **report}


def _latest_quality_ingestion_state(corpus_dir: Path, dataset_hash: str) -> dict[str, Any] | None:
    """Load the newest completed ingestion mapping for this immutable suite."""

    ingestion_root = corpus_dir / "ingestion"
    candidates: list[Path] = []
    for path in ingestion_root.glob("*.json"):
        if not path.is_file():
            continue
        try:
            state = read_json(path)
        except (OSError, ValueError):
            continue
        if state.get("status") == "completed" and state.get("dataset_hash") == dataset_hash:
            candidates.append(path)
    if not candidates:
        return None
    return read_json(max(candidates, key=lambda path: path.stat().st_mtime))


def _quality_eval_config(settings: Settings) -> EvalConfig:
    """Build a run contract for the profile used to ingest the quality corpus."""

    profile_name = str(settings.retrieval_profile or "")
    if not profile_name:
        raise QualitySuiteError("RETRIEVAL_PROFILE_MISSING", "retrieval profile is empty")
    profile = get_retrieval_profile(profile_name, settings)
    profile_payload = profile.model_dump(mode="json")
    return EvalConfig(
        config_id=f"quality_{profile_name}",
        retrieval_profile=profile_name,
        retrieval_overrides={},
        mode="normal",
        config_hash=stable_json_hash({"profile": profile_name, "profile_config": profile_payload, "mode": "normal"}),
        model_aliases=profile.model_aliases.model_dump(),
    )


async def eval_quality_run(
    *,
    corpus_dir: Path,
    api: str,
    split: str,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the existing answer and retrieval evaluators over one locked split."""

    if split not in {"dev", "test"}:
        raise ValueError("split must be dev or test")
    if run_id and resume_run_id:
        raise ValueError("run_id and resume_run_id are mutually exclusive")
    if not run_id and not resume_run_id:
        raise ValueError("run_id is required for a new quality run")
    if split == "test" and not resume_run_id:
        raise ValueError("test split requires --resume-run-id after a successful dev split")
    suite = validate_quality_suite(corpus_dir, strict_counts=True)
    resolved = adapt_eval_settings(settings or get_settings())
    tasks = [task for task in suite.tasks if task.split == split]
    ingestion_state = _latest_quality_ingestion_state(corpus_dir, suite.dataset_hash)
    binding_state = dict((ingestion_state or {}).get("binding") or {})
    binding_path = Path(str(binding_state.get("path") or ""))
    if binding_state.get("status") != "completed" or not await asyncio.to_thread(binding_path.is_file):
        raise QualitySuiteError("RUNTIME_BINDING_REQUIRED", "a completed signed binding is required before evaluation")
    signing_key = str(getattr(resolved, "eval_binding_signing_key", "") or "")
    try:
        binding = read_json(binding_path)
        verify_binding(binding, signing_key=signing_key)
    except RuntimeBindingError as exc:
        raise QualitySuiteError(exc.code, str(exc)) from exc
    if (
        binding.get("suite") != QUALITY_SUITE
        or binding.get("dataset_hash") != suite.dataset_hash
        or binding.get("material_hash") != suite.source_hash
    ):
        raise QualitySuiteError("RUNTIME_BINDING_STALE", "binding does not match the frozen suite")
    tasks = [
        EvalTask.model_validate(task)
        for task in bind_runtime_tasks([task.model_dump(mode="json") for task in tasks], binding)
    ]
    knowledge_base_ids = {
        str(language): str(knowledge_base_id)
        for language, knowledge_base_id in (ingestion_state or {}).get("knowledge_base_ids", {}).items()
        if knowledge_base_id
    }
    if not knowledge_base_ids:
        raise QualitySuiteError("INGESTION_NOT_COMPLETE", "completed language knowledge-base mapping is missing")
    missing_languages = sorted(
        {
            str(task.language_group or task.language)
            for task in tasks
            if str(task.language_group or task.language) not in knowledge_base_ids
        }
    )
    if missing_languages:
        raise QualitySuiteError("INGESTION_LANGUAGE_MISSING", ",".join(missing_languages))
    tasks = [
        task.model_copy(update={"knowledge_base_ids": [knowledge_base_ids[task.language_group or task.language]]})
        if (task.language_group or task.language) in knowledge_base_ids
        else task
        for task in tasks
    ]
    config = _quality_eval_config(resolved)
    manifest = suite.manifest.model_copy(
        update={
            "index_version": "runtime",
            "retrieval_profile_hash": config.config_hash,
            "generator_alias": config.model_aliases.get("generator", ""),
            "verifier_alias": config.model_aliases.get("verifier", ""),
        }
    )
    actual_run_id = resume_run_id or run_id or f"{QUALITY_SUITE}-{split}-{suite.dataset_hash[:12]}"
    run_dir = ARTIFACT_ROOT / "quality" / QUALITY_SUITE / actual_run_id
    answer_path = run_dir / "answer.jsonl"
    retrieval_path = run_dir / "retrieval.jsonl"
    status_path = run_dir / "status.json"
    dev_marker_path = run_dir / "dev.completed.json"
    if split == "test":
        if not dev_marker_path.exists():
            raise QualitySuiteError("DEV_NOT_COMPLETE", "dev split must finish before test")
        dev_marker = read_json(dev_marker_path)
        if (
            dev_marker.get("dataset_hash") != suite.dataset_hash
            or dev_marker.get("config_hash") != config.config_hash
            or dev_marker.get("binding_hash") != binding.get("binding_hash")
        ):
            raise QualitySuiteError("DEV_CONTRACT_MISMATCH", "test must use the same dataset and settings as dev")
    answer_results = read_jsonl(answer_path, EvalTaskResult)
    retrieval_results = read_jsonl(retrieval_path, RetrievalTaskResult)
    completed_answer = {result.task_id: result for result in answer_results if result.status == "completed"}
    completed_retrieval = {result.task_id: result for result in retrieval_results if result.status == "completed"}
    client = HttpEvalApiClient.from_settings(resolved, include_kiwix_urls=False)
    started = time.perf_counter()
    write_json(
        status_path,
        {
            "suite": QUALITY_SUITE,
            "run_id": actual_run_id,
            "split": split,
            "state": "running",
            "task_count": len(tasks),
            "binding_hash": binding.get("binding_hash"),
            "processed": len(completed_answer),
            "started_at": utc_now_iso(),
            "last_step": "prepared",
        },
    )
    try:
        for index, task in enumerate(tasks, start=1):
            answer = completed_answer.get(task.task_id)
            retrieval = completed_retrieval.get(task.task_id)
            if answer is not None and retrieval is not None and not rerun_failed:
                print(
                    f"[quality] split={split} question={index}/{len(tasks)} task_id={task.task_id} state=resumed",
                    flush=True,
                )
                continue
            question_started = time.perf_counter()
            print(
                f"[quality] split={split} question={index}/{len(tasks)} task_id={task.task_id} state=running",
                flush=True,
            )
            if retrieval is None or rerun_failed:
                retrieval = await run_retrieval_task(
                    task,
                    config,
                    api=api,
                    manifest=manifest,
                    client=client,
                    settings=resolved,
                    batch_index=(index - 1) // 1 + 1,
                    task_index=index,
                )
                if retrieval.status == "failed":
                    retrieval = retrieval.model_copy(update={"status": "search_incomplete"})
                append_jsonl(retrieval_path, retrieval)
                completed_retrieval[task.task_id] = retrieval
            if answer is None or rerun_failed:
                answer = await run_task(
                    task,
                    config,
                    api=api,
                    manifest=manifest,
                    client=client,
                    settings=resolved,
                    eval_run_id=actual_run_id,
                    request_namespace=f"{QUALITY_SUITE}:{split}",
                )
                if answer.status == "failed":
                    answer = answer.model_copy(update={"status": "search_incomplete"})
                append_jsonl(answer_path, answer)
                completed_answer[task.task_id] = answer
            write_json(
                status_path,
                {
                    "suite": QUALITY_SUITE,
                    "run_id": actual_run_id,
                    "split": split,
                    "state": "running",
                    "task_count": len(tasks),
                    "processed": index,
                    "completed_answer": sum(item.status == "completed" for item in completed_answer.values()),
                    "completed_retrieval": sum(item.status == "completed" for item in completed_retrieval.values()),
                    "last_task_id": task.task_id,
                    "last_step": "answer_completed" if answer and answer.status == "completed" else "answer_failed",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "last_question_seconds": round(time.perf_counter() - question_started, 3),
                    "updated_at": utc_now_iso(),
                },
            )
    except Exception:
        write_json(
            status_path,
            {
                "suite": QUALITY_SUITE,
                "run_id": actual_run_id,
                "split": split,
                "state": "failed",
                "processed": len(completed_answer),
                "updated_at": utc_now_iso(),
            },
        )
        raise
    answer_results = read_jsonl(answer_path, EvalTaskResult)
    retrieval_results = read_jsonl(retrieval_path, RetrievalTaskResult)
    task_ids = {task.task_id for task in tasks}
    answer_results = [result for result in answer_results if result.task_id in task_ids]
    retrieval_results = [result for result in retrieval_results if result.task_id in task_ids]
    report = {
        "suite": QUALITY_SUITE,
        "run_id": actual_run_id,
        "split": split,
        "task_count": len(tasks),
        "binding_hash": binding.get("binding_hash"),
        "answer_results": len(answer_results),
        "retrieval_results": len(retrieval_results),
        "answer_completed": sum(result.status == "completed" for result in answer_results),
        "answer_failed": sum(result.status != "completed" for result in answer_results),
        "retrieval_completed": sum(result.status == "completed" for result in retrieval_results),
        "retrieval_failed": sum(result.status != "completed" for result in retrieval_results),
        "questions_not_started": len(tasks) - len(answer_results),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "answer_path": str(answer_path),
        "retrieval_path": str(retrieval_path),
    }
    write_json(status_path, {**report, "state": "completed", "updated_at": utc_now_iso()})
    if split == "dev":
        write_json(
            dev_marker_path,
            {
                "suite": QUALITY_SUITE,
                "dataset_hash": suite.dataset_hash,
                "config_hash": config.config_hash,
                "binding_hash": binding.get("binding_hash"),
                "model_aliases": config.model_aliases,
                "completed_at": utc_now_iso(),
            },
        )
    return report


def _latest_quality_answer_path(corpus_dir: Path) -> Path:
    default = corpus_dir / "results.jsonl"
    if default.exists():
        return default
    root = ARTIFACT_ROOT / "quality" / QUALITY_SUITE
    candidates = [path for path in root.glob("*/answer.jsonl") if path.is_file()]
    if not candidates:
        return default
    return max(candidates, key=lambda path: path.stat().st_mtime)


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
