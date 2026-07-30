from __future__ import annotations

import asyncio
import math
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TextIO

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.api_client import HttpEvalApiClient, RetrievalEvalApiClient
from wikipediarag.eval.artifacts import ARTIFACT_ROOT, append_jsonl, read_json, read_jsonl, utc_now_iso, write_json
from wikipediarag.eval.corpus import load_chunk_refs
from wikipediarag.eval.metrics import aggregate, percentile, score_retrieval_task
from wikipediarag.eval.runner import eval_configs
from wikipediarag.eval.schemas import (
    CandidateRef,
    EvalConfig,
    EvalDatasetManifest,
    EvalTask,
    RetrievalConfigSummary,
    RetrievalEvalStatus,
    RetrievalRunManifest,
    RetrievalTaskResult,
    RetrievalTaskScores,
)

type RetrievalProgressCallback = Callable[[RetrievalEvalStatus, str], None]


class RetrievalEvalCliReporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def __call__(self, status: RetrievalEvalStatus, event: str) -> None:
        try:
            print(format_retrieval_progress(status, event), file=self._stream, flush=True)
        except OSError:
            return


def format_retrieval_progress(status: RetrievalEvalStatus, event: str) -> str:
    question = f" task_id={status.current_task_id}" if status.current_task_id else ""
    config = f" config={status.current_config_id}" if status.current_config_id else ""
    latency = f" last_latency_ms={status.last_latency_ms}" if status.last_latency_ms else ""
    eta = _format_eta(status.eta_seconds)
    return (
        f"[{_format_elapsed(status.elapsed_seconds)}] state={event}{config}"
        f" batch={status.current_batch}/{status.total_batches}"
        f" task={status.current_task_index}/{status.total_tasks}{question}"
        f" processed={status.processed_task_runs}/{status.total_task_runs}"
        f"{latency} avg_s={status.avg_seconds_per_task:.2f} eta={eta}"
    )


def format_retrieval_status(status: RetrievalEvalStatus) -> str:
    lines = [
        f"run_id={status.run_id} state={status.state} phase={status.phase}",
        f"updated_at={status.updated_at}",
        (
            f"progress processed={status.processed_task_runs}/{status.total_task_runs}"
            f" completed={status.completed_task_runs}"
            f" failed={status.failed_task_runs}"
        ),
        (
            f"current config={status.current_config_id or '-'}"
            f" config_index={status.current_config_index}/{status.total_configs}"
            f" batch={status.current_batch}/{status.total_batches}"
            f" task={status.current_task_index}/{status.total_tasks}"
            f" task_id={status.current_task_id or '-'}"
        ),
        (
            f"timing elapsed={_format_elapsed(status.elapsed_seconds)}"
            f" avg_s={status.avg_seconds_per_task:.2f}"
            f" eta={_format_eta(status.eta_seconds)}"
            f" last_latency_ms={status.last_latency_ms}"
        ),
        f"dataset={status.suite} hash={status.dataset_hash}",
        f"run_dir={status.run_dir}",
    ]
    if status.error_message:
        lines.append(f"error={status.error_message}")
    return "\n".join(lines)


async def run_retrieval_suite(
    manifest: EvalDatasetManifest,
    tasks: list[EvalTask],
    *,
    api: str,
    batch_size: int = 10,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    settings: Settings | None = None,
    client: RetrievalEvalApiClient | None = None,
    config_ids: set[str] | None = None,
    progress_callback: RetrievalProgressCallback | None = None,
) -> RetrievalRunManifest:
    if batch_size < 1:
        raise ValueError("batch size must be >= 1")
    if run_id and resume_run_id:
        raise ValueError("--run-id and --resume-run-id are mutually exclusive")
    resolved = settings or get_settings()
    actual_run_id = resume_run_id or run_id or f"{manifest.dataset_name}-{manifest.dataset_hash[:12]}-retrieval"
    run_dir = _run_dir(manifest.dataset_name, actual_run_id)
    configs = _filter_configs(eval_configs(resolved), config_ids)
    supported_configs = [config for config in configs if _is_supported(config)]
    api_client = client or HttpEvalApiClient()
    started_at = utc_now_iso()
    started = time.perf_counter()
    summaries: list[RetrievalConfigSummary] = []
    status = _initial_status(
        manifest,
        run_id=actual_run_id,
        run_dir=run_dir,
        batch_size=batch_size,
        configs=configs,
        supported_configs=supported_configs,
        started_at=started_at,
        processed_task_runs=_processed_count(run_dir, configs),
        completed_task_runs=_completed_count(run_dir),
        failed_task_runs=_failed_count(run_dir),
    )
    _write_status(status)
    _log_event(run_dir, "run_started", status=status)
    _emit(progress_callback, status, "run_started")
    try:
        for config_index, config in enumerate(configs, start=1):
            if not _is_supported(config):
                summary = _unsupported_summary(config, tasks)
                summaries.append(summary)
                _write_config_summary(run_dir, summary)
                _log_event(run_dir, "config_unsupported", status=status, config=config)
                continue
            status = status.model_copy(
                update={
                    "phase": "config_running",
                    "current_config_id": config.config_id,
                    "current_config_index": config_index,
                    "current_batch": 0,
                    "current_task_index": 0,
                    "current_task_id": "",
                    "updated_at": utc_now_iso(),
                }
            )
            _write_status(status)
            results_path = _results_path(run_dir, config)
            latest_by_task = _latest_results(results_path)
            total_batches = math.ceil(len(tasks) / batch_size) if tasks else 0
            status = status.model_copy(update={"total_batches": total_batches, "updated_at": utc_now_iso()})
            _write_status(status)
            status = await _run_retrieval_config_tasks(
                tasks,
                config,
                api=api,
                manifest=manifest,
                client=api_client,
                settings=resolved,
                batch_size=batch_size,
                results_path=results_path,
                latest_by_task=latest_by_task,
                rerun_failed=rerun_failed,
                status=status,
                started=started,
                configs=configs,
                run_dir=run_dir,
                progress_callback=progress_callback,
            )
            latest_by_task = _latest_results(results_path)
            summary = summarize_retrieval_config(config, tasks, list(latest_by_task.values()))
            summaries.append(summary)
            _write_config_summary(run_dir, summary)
        run_manifest = RetrievalRunManifest(
            run_id=actual_run_id,
            suite=manifest.dataset_name,
            dataset_hash=manifest.dataset_hash,
            dataset_path=manifest.jsonl_path,
            created_at=utc_now_iso(),
            batch_size=batch_size,
            config_summaries=summaries,
            run_dir=str(run_dir),
        )
        write_json(run_dir / "manifest.json", run_manifest.model_dump(mode="json"))
        write_json(_runs_root() / "latest.json", run_manifest.model_dump(mode="json"))
        status = _advance_status(
            status,
            started=started,
            current_task_id="",
            current_task_index=0,
            processed_task_runs=_processed_count(run_dir, configs),
        ).model_copy(
            update={
                "state": "completed",
                "phase": "completed",
                "updated_at": utc_now_iso(),
                "eta_seconds": 0.0,
            }
        )
        _write_status(status)
        _log_event(run_dir, "run_completed", status=status)
        _emit(progress_callback, status, "run_completed")
        return run_manifest
    except Exception as exc:
        status = _advance_status(
            status,
            started=started,
            current_task_id=status.current_task_id,
            current_task_index=status.current_task_index,
            processed_task_runs=_processed_count(run_dir, configs),
        ).model_copy(
            update={
                "state": "failed",
                "phase": "failed",
                "updated_at": utc_now_iso(),
                "error_message": type(exc).__name__ + ": " + str(exc),
            }
        )
        _write_status(status)
        _log_event(run_dir, "run_failed", status=status, errors=[status.error_message])
        _emit(progress_callback, status, "run_failed")
        raise


async def run_retrieval_task(
    task: EvalTask,
    config: EvalConfig,
    *,
    api: str,
    manifest: EvalDatasetManifest,
    client: RetrievalEvalApiClient,
    settings: Settings,
    batch_index: int,
    task_index: int,
) -> RetrievalTaskResult:
    started = time.perf_counter()
    try:
        payload = await asyncio.to_thread(
            client.run_search_debug,
            task.question,
            api=api,
            top_k=20,
            retrieval_profile=config.retrieval_profile,
            retrieval_overrides=config.retrieval_overrides,
        )
        total_ms = int((time.perf_counter() - started) * 1000)
        prefusion, reranked, final = await extract_search_debug_candidates(payload, settings=settings)
        scores = score_retrieval_task(task, final=final, reranked=reranked, prefusion=prefusion)
        timings_ms = _search_debug_timings_ms(payload)
        contract_ids = _contract_ids_from_payload(payload)
        return RetrievalTaskResult(
            task_id=task.task_id,
            config_id=config.config_id,
            config_hash=config.config_hash,
            status="completed",
            question=task.question,
            task_family=task.task_family,
            unanswerable=task.unanswerable,
            batch_index=batch_index,
            task_index=task_index,
            retrieved_candidates=prefusion,
            reranked_candidates=reranked,
            final_candidates=final,
            latency_ms={"total": total_ms, "retrieval": _retrieval_latency_ms(payload), **timings_ms},
            scores=scores,
            errors=[],
            trace_id=str(payload.get("trace_id")) if payload.get("trace_id") else None,
            corpus={
                "snapshot_id": manifest.snapshot_id,
                "index_version": manifest.index_version,
                "zim_checksum": manifest.zim_checksum,
            },
            model_aliases=config.model_aliases,
            contract_ids=contract_ids,
        )
    except Exception as exc:
        total_ms = int((time.perf_counter() - started) * 1000)
        return RetrievalTaskResult(
            task_id=task.task_id,
            config_id=config.config_id,
            config_hash=config.config_hash,
            status="failed",
            question=task.question,
            task_family=task.task_family,
            unanswerable=task.unanswerable,
            batch_index=batch_index,
            task_index=task_index,
            latency_ms={"total": total_ms},
            errors=[type(exc).__name__ + ": " + str(exc)],
            corpus={
                "snapshot_id": manifest.snapshot_id,
                "index_version": manifest.index_version,
                "zim_checksum": manifest.zim_checksum,
            },
            model_aliases=config.model_aliases,
            contract_ids={},
        )


async def extract_search_debug_candidates(
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> tuple[list[CandidateRef], list[CandidateRef], list[CandidateRef]]:
    events = [dict(item) for item in payload.get("events", []) if isinstance(item, dict)]
    prefusion_raw = _stage_candidates(events, ("rrf", "bm25", "dense"))
    reranked_raw = _stage_candidates(events, ("rerank", "context"))
    final_raw = _stage_candidates(events, ("context", "rerank"))
    ids = [str(item.get("chunk_id")) for item in [*prefusion_raw, *reranked_raw, *final_raw] if item.get("chunk_id")]
    refs = await load_chunk_refs(ids, settings=settings)
    return (
        _candidate_refs(prefusion_raw, refs, "prefusion"),
        _candidate_refs(reranked_raw, refs, "rerank"),
        _candidate_refs(final_raw, refs, "context"),
    )


def summarize_retrieval_config(
    config: EvalConfig,
    tasks: list[EvalTask],
    results: list[RetrievalTaskResult],
) -> RetrievalConfigSummary:
    task_by_id = {task.task_id: task for task in tasks}
    latest = list({result.task_id: result for result in results}.values())
    completed = [result for result in latest if result.scores is not None]
    answerable = [result for result in completed if not task_by_id[result.task_id].unanswerable]
    by_family: dict[str, dict[str, float]] = {}
    for family in sorted({task.task_family for task in tasks}):
        family_results = [result for result in completed if task_by_id[result.task_id].task_family == family]
        family_answerable = [result for result in family_results if not task_by_id[result.task_id].unanswerable]
        by_family[str(family)] = _aggregate_retrieval_results(family_results, family_answerable)
    failed = [
        result.task_id
        for result in latest
        if result.status == "failed"
        or result.scores is None
        or _retrieval_task_failed_threshold(task_by_id.get(result.task_id), result.scores)
    ]
    contract_ids = _summary_contract_ids(latest)
    return RetrievalConfigSummary(
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="completed",
        task_count=len(latest),
        metrics=_aggregate_retrieval_results(completed, answerable),
        by_family=by_family,
        failed_task_ids=sorted(set(failed)),
        errors=[error for result in latest for error in result.errors] + _contract_mix_errors(contract_ids),
        contract_ids=contract_ids,
    )


def load_retrieval_status(run_id: str) -> RetrievalEvalStatus:
    direct = _runs_root() / run_id / "status.json"
    if direct.exists():
        return RetrievalEvalStatus.model_validate(read_json(direct))
    matches = sorted(_runs_root().glob(f"*/{run_id}/status.json"))
    if not matches:
        raise FileNotFoundError(f"no retrieval eval status found for run_id {run_id}")
    return RetrievalEvalStatus.model_validate(read_json(matches[-1]))


def load_latest_retrieval_status() -> RetrievalEvalStatus:
    latest = _runs_root() / "latest-status.json"
    if latest.exists():
        return RetrievalEvalStatus.model_validate(read_json(latest))
    manifest_latest = _runs_root() / "latest.json"
    if not manifest_latest.exists():
        raise FileNotFoundError("no retrieval eval run found")
    manifest = RetrievalRunManifest.model_validate(read_json(manifest_latest))
    return load_retrieval_status(manifest.run_id)


def load_latest_retrieval_run() -> RetrievalRunManifest:
    latest = _runs_root() / "latest.json"
    if not latest.exists():
        raise FileNotFoundError("no retrieval eval run manifest found")
    return RetrievalRunManifest.model_validate(read_json(latest))


def _aggregate_retrieval_results(
    results: list[RetrievalTaskResult],
    answerable: list[RetrievalTaskResult],
) -> dict[str, float]:
    retrieval_base = answerable or results
    scores = [result.scores for result in results if result.scores is not None]
    retrieval_scores = [result.scores for result in retrieval_base if result.scores is not None]
    latencies = [float(result.latency_ms.get("total", 0)) for result in results]
    retrieval_latencies = [float(result.latency_ms.get("retrieval", 0)) for result in results]
    metrics = {
        "page_recall_at_1": aggregate(score.page_recall["1"] for score in retrieval_scores),
        "page_recall_at_5": aggregate(score.page_recall["5"] for score in retrieval_scores),
        "page_recall_at_10": aggregate(score.page_recall["10"] for score in retrieval_scores),
        "page_recall_at_20": aggregate(score.page_recall["20"] for score in retrieval_scores),
        "section_recall_at_5": aggregate(score.section_recall["5"] for score in retrieval_scores),
        "section_recall_at_10": aggregate(score.section_recall["10"] for score in retrieval_scores),
        "section_recall_at_20": aggregate(score.section_recall["20"] for score in retrieval_scores),
        "chunk_recall_at_5": aggregate(score.chunk_recall["5"] for score in retrieval_scores),
        "chunk_recall_at_10": aggregate(score.chunk_recall["10"] for score in retrieval_scores),
        "chunk_recall_at_20": aggregate(score.chunk_recall["20"] for score in retrieval_scores),
        "mrr_at_10": aggregate(score.mrr_at_10 for score in retrieval_scores),
        "ndcg_at_10": aggregate(score.ndcg_at_10 for score in retrieval_scores),
        "full_hop_recall": aggregate(score.full_hop_recall for score in retrieval_scores),
        "path_completion": aggregate(score.path_completion for score in retrieval_scores),
        "reranker_gold_delta": aggregate(
            score.reranker_gold_delta for score in retrieval_scores if score.reranker_gold_delta is not None
        ),
        "retrieved_gold_leak_rate": aggregate(score.retrieved_gold_leak_rate for score in scores),
        "false_positive_evidence_rate": aggregate(score.false_positive_evidence_rate for score in scores),
        "dangerous_false_positive_evidence_rate": aggregate(
            score.dangerous_false_positive_evidence_rate for score in scores
        ),
        "hard_negative_page_hit_at_10": aggregate(score.hard_negative_page_hit_at_10 for score in scores),
        "hard_negative_page_hit_at_20": aggregate(score.hard_negative_page_hit_at_20 for score in scores),
        "gold_vs_hard_negative_rank_margin": aggregate(
            score.gold_vs_hard_negative_rank_margin
            for score in scores
            if score.gold_vs_hard_negative_rank_margin is not None
        ),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "retrieval_latency_p50_ms": percentile(retrieval_latencies, 50),
        "retrieval_latency_p95_ms": percentile(retrieval_latencies, 95),
        "error_rate": aggregate(1.0 if result.status == "failed" else 0.0 for result in results),
    }
    metrics.update(_stage_latency_metrics(results))
    return metrics


def _retrieval_task_failed_threshold(task: EvalTask | None, scores: RetrievalTaskScores | None) -> bool:
    if task is None or scores is None:
        return True
    if task.unanswerable:
        return scores.retrieved_gold_leak_rate > 0.0
    return scores.page_recall["10"] < 1.0 or scores.chunk_recall["20"] < 1.0


def _candidate_refs(
    candidates: list[dict[str, Any]],
    refs: dict[str, Any],
    stage: str,
) -> list[CandidateRef]:
    output: list[CandidateRef] = []
    for rank, item in enumerate(candidates[:20], start=1):
        chunk_id = str(item.get("chunk_id") or "")
        ref = refs.get(chunk_id)
        output.append(
            CandidateRef(
                chunk_id=chunk_id,
                document_id=ref.document_id if ref else "",
                section_id=ref.section_id if ref else "",
                title=str(item.get("title") or (ref.title if ref else "")),
                source_url=str(item.get("source_url") or (ref.source_url if ref else "")),
                rank=rank,
                stage=stage,
                scores={key: float(value) for key, value in dict(item.get("scores") or {}).items()},
            )
        )
    return output


def _stage_candidates(events: list[dict[str, Any]], stages: tuple[str, ...]) -> list[dict[str, Any]]:
    for stage in stages:
        for event in events:
            if event.get("stage") == stage and isinstance(event.get("candidates"), list):
                return [dict(item) for item in event["candidates"] if isinstance(item, dict)]
    return []


def _retrieval_latency_ms(payload: dict[str, Any]) -> int:
    for event in reversed(list(payload.get("events", []))):
        if isinstance(event, dict) and event.get("stage") == "context" and event.get("latency_ms") is not None:
            return int(event["latency_ms"])
    return 0


def _search_debug_timings_ms(payload: dict[str, Any]) -> dict[str, int]:
    timings: dict[str, int] = {}
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        if event.get("stage") == "timings" and isinstance(event.get("timings_ms"), dict):
            timings.update(_safe_timing_dict(event["timings_ms"]))
    return timings


def _safe_timing_dict(payload: dict[Any, Any]) -> dict[str, int]:
    return {
        str(key): max(0, int(value)) for key, value in payload.items() if isinstance(value, int | float) and str(key)
    }


def _contract_ids_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(payload[key])
        for key in ("index_contract_id", "run_contract_id")
        if isinstance(payload.get(key), str) and payload.get(key)
    }


def _summary_contract_ids(results: list[RetrievalTaskResult]) -> dict[str, list[str]]:
    keys = sorted({key for result in results for key in result.contract_ids})
    return {
        key: sorted({result.contract_ids[key] for result in results if result.contract_ids.get(key)}) for key in keys
    }


def _contract_mix_errors(contract_ids: dict[str, list[str]]) -> list[str]:
    return [f"mixed_contract_ids:{key}" for key, ids in contract_ids.items() if len(ids) > 1]


def _stage_latency_metrics(results: list[RetrievalTaskResult]) -> dict[str, float]:
    keys = sorted({key for result in results for key in result.latency_ms if key not in {"total", "retrieval"}})
    metrics: dict[str, float] = {}
    for key in keys:
        values = [float(result.latency_ms[key]) for result in results if key in result.latency_ms]
        metrics[f"stage_latency_{key}_p50_ms"] = percentile(values, 50)
        metrics[f"stage_latency_{key}_p95_ms"] = percentile(values, 95)
    return metrics


def _unsupported_summary(config: EvalConfig, tasks: list[EvalTask]) -> RetrievalConfigSummary:
    return RetrievalConfigSummary(
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="unsupported",
        task_count=0,
        metrics={},
        by_family={family: {} for family in sorted({task.task_family for task in tasks})},
        failed_task_ids=[],
        errors=["search:debug does not execute the conditional harness; use eval-run for harness evaluation"],
    )


def _is_supported(config: EvalConfig) -> bool:
    return config.config_id != "sota_mvp_conditional_harness"


def _filter_configs(configs: list[EvalConfig], config_ids: set[str] | None) -> list[EvalConfig]:
    if not config_ids:
        return configs
    filtered = [config for config in configs if config.config_id in config_ids]
    missing = sorted(config_ids - {config.config_id for config in filtered})
    if missing:
        raise ValueError(f"unknown eval config IDs: {missing}")
    return filtered


async def _run_retrieval_config_tasks(
    tasks: list[EvalTask],
    config: EvalConfig,
    *,
    api: str,
    manifest: EvalDatasetManifest,
    client: RetrievalEvalApiClient,
    settings: Settings,
    batch_size: int,
    results_path: Path,
    latest_by_task: dict[str, RetrievalTaskResult],
    rerun_failed: bool,
    status: RetrievalEvalStatus,
    started: float,
    configs: list[EvalConfig],
    run_dir: Path,
    progress_callback: RetrievalProgressCallback | None,
) -> RetrievalEvalStatus:
    eligible = [
        (index, task)
        for index, task in enumerate(tasks, start=1)
        if not (latest_by_task.get(task.task_id) and _skip_existing(latest_by_task[task.task_id], rerun_failed))
    ]
    next_index = 0
    pending: dict[asyncio.Task[RetrievalTaskResult], tuple[int, EvalTask]] = {}

    def current_batch(task_index: int) -> int:
        return math.ceil(task_index / batch_size) if task_index else 0

    async def schedule_next(current_status: RetrievalEvalStatus) -> RetrievalEvalStatus:
        nonlocal next_index
        if next_index >= len(eligible):
            return current_status
        task_index, task = eligible[next_index]
        next_index += 1
        batch_index = current_batch(task_index)
        updated = _advance_status(
            current_status,
            started=started,
            current_task_id=task.task_id,
            current_task_index=task_index,
            processed_task_runs=_processed_count(run_dir, configs),
        ).model_copy(update={"current_batch": batch_index, "updated_at": utc_now_iso()})
        _write_status(updated)
        if not pending or batch_index != current_status.current_batch:
            _log_event(run_dir, "batch_started", status=updated, config=config)
            _emit(progress_callback, updated, "batch_started")
        _log_event(run_dir, "task_started", status=updated, config=config, task=task)
        _emit(progress_callback, updated, _question_event("task_started", task.question))
        future = asyncio.create_task(
            run_retrieval_task(
                task,
                config,
                api=api,
                manifest=manifest,
                client=client,
                settings=settings,
                batch_index=batch_index,
                task_index=task_index,
            )
        )
        pending[future] = (task_index, task)
        return updated

    try:
        while next_index < len(eligible) and len(pending) < batch_size:
            status = await schedule_next(status)
        while pending:
            done, _pending = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                task_index, task = pending.pop(finished)
                result = await finished
                append_jsonl(results_path, result)
                latest_by_task[task.task_id] = result
                status = _advance_status(
                    status,
                    started=started,
                    current_task_id=task.task_id,
                    current_task_index=task_index,
                    processed_task_runs=_processed_count(run_dir, configs),
                    last_latency_ms=int(result.latency_ms.get("total", 0)),
                ).model_copy(update={"current_batch": current_batch(task_index), "updated_at": utc_now_iso()})
                _write_status(status)
                _log_event(
                    run_dir,
                    "task_completed" if result.status == "completed" else "task_failed",
                    status=status,
                    config=config,
                    task=task,
                    errors=result.errors,
                )
                _emit(progress_callback, status, _question_event(result.status, task.question))
                status = await schedule_next(status)
        if eligible:
            _log_event(run_dir, "batch_completed", status=status, config=config)
            _emit(progress_callback, status, "batch_completed")
        return status
    finally:
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*pending.keys(), return_exceptions=True)


def _skip_existing(result: RetrievalTaskResult, rerun_failed: bool) -> bool:
    if result.status == "completed":
        return True
    return result.status == "failed" and not rerun_failed


def _batches(items: list[EvalTask], batch_size: int) -> Iterable[list[EvalTask]]:
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def _latest_results(path: Path) -> dict[str, RetrievalTaskResult]:
    rows = read_jsonl(path, RetrievalTaskResult)
    return {row.task_id: row for row in rows}


def _processed_count(run_dir: Path, configs: list[EvalConfig]) -> int:
    total = 0
    for config in configs:
        if not _is_supported(config):
            continue
        total += len(_latest_results(_results_path(run_dir, config)))
    return total


def _advance_status(
    status: RetrievalEvalStatus,
    *,
    started: float,
    current_task_id: str,
    current_task_index: int,
    processed_task_runs: int,
    last_latency_ms: int | None = None,
) -> RetrievalEvalStatus:
    elapsed = max(0.0, time.perf_counter() - started)
    completed = _completed_count(Path(status.run_dir))
    failed = _failed_count(Path(status.run_dir))
    avg = elapsed / processed_task_runs if processed_task_runs else 0.0
    remaining = max(0, status.total_task_runs - processed_task_runs)
    return status.model_copy(
        update={
            "processed_task_runs": processed_task_runs,
            "completed_task_runs": completed,
            "failed_task_runs": failed,
            "current_task_id": current_task_id,
            "current_task_index": current_task_index,
            "elapsed_seconds": elapsed,
            "eta_seconds": avg * remaining if processed_task_runs else None,
            "avg_seconds_per_task": avg,
            "last_latency_ms": status.last_latency_ms if last_latency_ms is None else last_latency_ms,
            "updated_at": utc_now_iso(),
        }
    )


def _completed_count(run_dir: Path) -> int:
    return _status_count(run_dir, "completed")


def _failed_count(run_dir: Path) -> int:
    return _status_count(run_dir, "failed")


def _status_count(run_dir: Path, status: str) -> int:
    return sum(
        1
        for path in (run_dir / "results").glob("*.jsonl")
        for result in _latest_results(path).values()
        if result.status == status
    )


def _initial_status(
    manifest: EvalDatasetManifest,
    *,
    run_id: str,
    run_dir: Path,
    batch_size: int,
    configs: list[EvalConfig],
    supported_configs: list[EvalConfig],
    started_at: str,
    processed_task_runs: int = 0,
    completed_task_runs: int = 0,
    failed_task_runs: int = 0,
) -> RetrievalEvalStatus:
    total_batches = math.ceil(manifest.task_count / batch_size) if manifest.task_count else 0
    return RetrievalEvalStatus(
        run_id=run_id,
        state="running",
        phase="preparing",
        suite=manifest.dataset_name,
        dataset_hash=manifest.dataset_hash,
        dataset_path=manifest.jsonl_path,
        run_dir=str(run_dir),
        batch_size=batch_size,
        total_configs=len(configs),
        supported_configs=len(supported_configs),
        total_tasks=manifest.task_count,
        total_task_runs=manifest.task_count * len(supported_configs),
        processed_task_runs=processed_task_runs,
        completed_task_runs=completed_task_runs,
        failed_task_runs=failed_task_runs,
        current_batch=0,
        total_batches=total_batches,
        started_at=started_at,
        updated_at=started_at,
    )


def _write_status(status: RetrievalEvalStatus) -> None:
    write_json(Path(status.run_dir) / "status.json", status.model_dump(mode="json"))
    write_json(_runs_root() / "latest-status.json", status.model_dump(mode="json"))


def _write_config_summary(run_dir: Path, summary: RetrievalConfigSummary) -> None:
    write_json(run_dir / "summaries" / f"{summary.config_id}.json", summary.model_dump(mode="json"))


def _log_event(
    run_dir: Path,
    event: str,
    *,
    status: RetrievalEvalStatus,
    config: EvalConfig | None = None,
    task: EvalTask | None = None,
    errors: list[str] | None = None,
) -> None:
    append_jsonl(
        run_dir / "logs" / "events.jsonl",
        {
            "event": event,
            "timestamp": utc_now_iso(),
            "run_id": status.run_id,
            "config_id": config.config_id if config else status.current_config_id,
            "config_hash": config.config_hash if config else "",
            "task_id": task.task_id if task else status.current_task_id,
            "task_index": status.current_task_index,
            "batch": status.current_batch,
            "processed_task_runs": status.processed_task_runs,
            "errors": errors or [],
        },
    )


def _emit(callback: RetrievalProgressCallback | None, status: RetrievalEvalStatus, event: str) -> None:
    if callback is not None:
        callback(status, event)


def _question_event(event: str, question: str) -> str:
    del question
    return event


def _results_path(run_dir: Path, config: EvalConfig) -> Path:
    return run_dir / "results" / f"{config.config_id}-{config.config_hash[:12]}.jsonl"


def _run_dir(suite: str, run_id: str) -> Path:
    return _runs_root() / suite / run_id


def _runs_root() -> Path:
    return ARTIFACT_ROOT / "retrieval-runs"


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return _format_elapsed(seconds)
