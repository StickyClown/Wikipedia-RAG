from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Any

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.api_client import EvalApiClient, HttpEvalApiClient, RetrievalEvalApiClient
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    DATASET_NAME,
    load_latest_dataset,
    utc_now_iso,
    write_json,
    write_jsonl,
)
from wikipediarag.eval.corpus import load_candidate_chunks, load_corpus_snapshot
from wikipediarag.eval.external import transfer_miracl_ru
from wikipediarag.eval.generate_runs import load_generate_status, load_latest_generate_status
from wikipediarag.eval.generator import SMOKE_MARKER, build_smoke_tasks, generate_dataset
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.progress import EvalGenerateProgressCallback
from wikipediarag.eval.reporting import load_latest_run, write_report
from wikipediarag.eval.retrieval_reporting import write_retrieval_report
from wikipediarag.eval.retrieval_runner import (
    RetrievalProgressCallback,
    load_latest_retrieval_status,
    load_retrieval_status,
    run_retrieval_suite,
)
from wikipediarag.eval.review import eval_release_gate as run_reviewed_release_gate
from wikipediarag.eval.review import freeze_reviewed_suite, load_release_gate_status, write_review_pool
from wikipediarag.eval.runner import _eval_overrides, run_suite, run_task
from wikipediarag.eval.schemas import (
    EvalConfig,
    EvalDatasetManifest,
    EvalGenerateRunStatus,
    EvalTaskResult,
    ReleaseGateStatus,
    RetrievalEvalStatus,
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
    api_client = client or HttpEvalApiClient(
        kiwix_public_base_url=resolved.kiwix_public_base_url,
        kiwix_internal_base_url=resolved.kiwix_internal_base_url,
    )
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
    settings: Settings | None = None,
    client: EvalApiClient | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    manifest, tasks = load_latest_dataset(suite)
    run_manifest = await run_suite(
        manifest,
        tasks,
        api=api,
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
    run = await eval_run(suite=dataset.dataset_name, api=api, settings=resolved)
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


async def eval_miracl_map(*, input_path: Path, settings: Settings | None = None) -> dict[str, str]:
    return await transfer_miracl_ru(input_path=input_path, settings=adapt_eval_settings(settings or get_settings()))


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


def eval_release_gate_status(*, suite: str) -> ReleaseGateStatus:
    return load_release_gate_status(suite)
