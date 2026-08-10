from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TextIO

from wikipediarag.answerability import GATE_VERSION as ANSWERABILITY_GATE_VERSION
from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.api_client import EvalApiClient, HttpEvalApiClient
from wikipediarag.eval.artifacts import ARTIFACT_ROOT, append_jsonl, read_json, read_jsonl, utc_now_iso, write_json
from wikipediarag.eval.diagnostics import answer_result_diagnosis, diagnose_answer_task, root_cause_count_metrics
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.metrics import aggregate, percentile, score_task
from wikipediarag.eval.schemas import (
    CandidateRef,
    ConfigSummary,
    EvalConfig,
    EvalDatasetManifest,
    EvalRunManifest,
    EvalRunStatus,
    EvalTask,
    EvalTaskResult,
    TaskScores,
)
from wikipediarag.reliability import RetryPolicy, safe_failure_from_exception
from wikipediarag.retrieval_profile import get_retrieval_profile

type EvalRunProgressCallback = Callable[[EvalRunStatus, str], None]

DEFAULT_ANSWER_EVAL_BATCH_SIZE = 6
ANSWER_EVAL_MAX_ATTEMPTS = 2
EVAL_SEMANTICS_VERSION = "reviewed_gate_semantics_v4"


class EvalRunCliReporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def __call__(self, status: EvalRunStatus, event: str) -> None:
        try:
            print(format_eval_run_progress(status, event), file=self._stream, flush=True)
        except OSError:
            return


def format_eval_run_progress(status: EvalRunStatus, event: str) -> str:
    task = f" task_id={status.current_task_id}" if status.current_task_id else ""
    config = f" config={status.current_config_id}" if status.current_config_id else ""
    latency = f" last_latency_ms={status.last_latency_ms}" if status.last_latency_ms else ""
    eta = _format_eta(status.eta_seconds)
    return (
        f"[{_format_elapsed(status.elapsed_seconds)}] state={event}{config}"
        f" task={status.current_task_index}/{status.total_tasks}{task}"
        f" batch_size={status.batch_size}"
        f" processed={status.processed_task_runs}/{status.total_task_runs}"
        f"{latency} avg_s={status.avg_seconds_per_task:.2f} eta={eta}"
    )


def format_eval_run_status(status: EvalRunStatus) -> str:
    lines = [
        f"run_id={status.run_id} state={status.state} phase={status.phase}",
        f"updated_at={status.updated_at}",
        (
            f"progress processed={status.processed_task_runs}/{status.total_task_runs}"
            f" completed={status.completed_task_runs}"
            f" failed={status.failed_task_runs}"
            f" batch_size={status.batch_size}"
        ),
        (
            f"current config={status.current_config_id or '-'}"
            f" config_index={status.current_config_index}/{status.total_configs}"
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


def eval_configs(settings: Settings | None = None) -> list[EvalConfig]:
    resolved = settings or get_settings()
    base_profile = "sota_mvp"
    specs: list[tuple[str, str, dict[str, Any], Literal["normal", "extended", "auto"]]] = [
        (
            "bm25_only",
            base_profile,
            {
                "retrieval": {"bm25": True, "dense": False, "fusion": "none", "rerank": False},
                "postprocess": {"parent_expansion": "off", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "dense_only",
            base_profile,
            {
                "retrieval": {"bm25": False, "dense": True, "fusion": "none", "rerank": False},
                "postprocess": {"parent_expansion": "off", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "hybrid_rrf",
            base_profile,
            {
                "retrieval": {"bm25": True, "dense": True, "fusion": "rrf", "rerank": False},
                "postprocess": {"parent_expansion": "off", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "hybrid_rerank",
            base_profile,
            {
                "retrieval": {"bm25": True, "dense": True, "fusion": "rrf", "rerank": True},
                "postprocess": {"parent_expansion": "selective", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "sota_mvp_normal",
            base_profile,
            {"postprocess": {"extended_search": "conditional"}},
            "normal",
        ),
        (
            "sota_mvp_verified",
            "sota_mvp_verified",
            {"postprocess": {"extended_search": "off"}},
            "normal",
        ),
        (
            "sota_mvp_conditional_harness",
            base_profile,
            {},
            "normal",
        ),
    ]
    configs: list[EvalConfig] = []
    for config_id, profile_name, overrides, mode in specs:
        merged = _eval_overrides(overrides)
        profile = get_retrieval_profile(profile_name, resolved, merged)
        payload = {
            "profile": profile_name,
            "overrides": merged,
            "mode": mode,
            "answerability_gate_version": ANSWERABILITY_GATE_VERSION,
            "eval_semantics_version": EVAL_SEMANTICS_VERSION,
            "model_aliases": profile.model_aliases.model_dump(),
            "verification": profile.answer.verification.model_dump(mode="json"),
        }
        configs.append(
            EvalConfig(
                config_id=config_id,
                retrieval_profile=profile_name,
                retrieval_overrides=merged,
                mode=mode,
                config_hash=stable_json_hash(payload),
                model_aliases=profile.model_aliases.model_dump(),
            )
        )
    return configs


async def run_suite(
    manifest: EvalDatasetManifest,
    tasks: list[EvalTask],
    *,
    api: str,
    run_id: str | None = None,
    report_id: str = "",
    reuse_completed: bool = True,
    settings: Settings | None = None,
    client: EvalApiClient | None = None,
    batch_size: int = DEFAULT_ANSWER_EVAL_BATCH_SIZE,
    config_ids: set[str] | None = None,
    progress_callback: EvalRunProgressCallback | None = None,
) -> EvalRunManifest:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    resolved = settings or get_settings()
    resolved_run_id = run_id or f"{manifest.dataset_name}-{manifest.dataset_hash[:12]}"
    resolved_report_id = report_id or resolved_run_id
    run_dir = ARTIFACT_ROOT / "runs" / manifest.dataset_name / resolved_run_id
    configs = _filter_configs(eval_configs(resolved), config_ids)
    api_client = client or HttpEvalApiClient.from_settings(resolved)
    summaries: list[ConfigSummary] = []
    started_at = utc_now_iso()
    started = time.perf_counter()
    status = _initial_status(
        manifest,
        run_id=resolved_run_id,
        report_id=resolved_report_id,
        run_dir=run_dir,
        configs=configs,
        started_at=started_at,
        batch_size=batch_size,
        processed_task_runs=_processed_count(
            run_dir,
            configs,
            eval_run_id=None if reuse_completed else resolved_run_id,
        ),
        completed_task_runs=_completed_count(run_dir, eval_run_id=None if reuse_completed else resolved_run_id),
        failed_task_runs=_failed_count(run_dir, eval_run_id=None if reuse_completed else resolved_run_id),
    )
    _write_status(status)
    _log_event(run_dir, "run_started", status=status)
    _emit(progress_callback, status, "run_started")
    try:
        for config_index, config in enumerate(configs, start=1):
            status = _advance_status(
                status,
                started=started,
                current_task_id="",
                current_task_index=0,
                processed_task_runs=_processed_count(
                    run_dir,
                    configs,
                    eval_run_id=None if reuse_completed else resolved_run_id,
                ),
                count_eval_run_id=None if reuse_completed else resolved_run_id,
            ).model_copy(
                update={
                    "phase": "config_running",
                    "current_config_id": config.config_id,
                    "current_config_index": config_index,
                    "updated_at": utc_now_iso(),
                }
            )
            _write_status(status)
            _log_event(run_dir, "config_started", status=status, config=config)
            _emit(progress_callback, status, "config_started")
            results_path = _results_path(run_dir, config)
            existing = _latest_results(results_path) if reuse_completed else {}
            root_run_contract_id = _eval_root_run_contract_id(manifest, config)
            status = await _run_config_with_backfill(
                status,
                started=started,
                run_dir=run_dir,
                configs=configs,
                config=config,
                tasks=tasks,
                existing=existing,
                results_path=results_path,
                report_id=resolved_report_id,
                run_started_at=started_at,
                reuse_completed=reuse_completed,
                root_run_contract_id=root_run_contract_id,
                batch_size=batch_size,
                api=api,
                manifest=manifest,
                client=api_client,
                settings=resolved,
                progress_callback=progress_callback,
            )
            results = list(
                _latest_results(
                    results_path,
                    eval_run_id=resolved_run_id if not reuse_completed else None,
                    dataset_hash=manifest.dataset_hash if not reuse_completed else None,
                    config_hash=config.config_hash,
                ).values()
            )
            summary = summarize_config(config, tasks, results)
            summaries.append(summary)
            write_json(run_dir / "summaries" / f"{config.config_id}.json", summary.model_dump(mode="json"))
            status = _advance_status(
                status,
                started=started,
                current_task_id="",
                current_task_index=0,
                processed_task_runs=_processed_count(
                    run_dir,
                    configs,
                    eval_run_id=None if reuse_completed else resolved_run_id,
                ),
                count_eval_run_id=None if reuse_completed else resolved_run_id,
            )
            _write_status(status)
            _log_event(run_dir, "config_completed", status=status, config=config)
            _emit(progress_callback, status, "config_completed")
        run_manifest = EvalRunManifest(
            run_id=resolved_run_id,
            report_id=resolved_report_id,
            suite=manifest.dataset_name,
            dataset_hash=manifest.dataset_hash,
            dataset_path=manifest.jsonl_path,
            created_at=utc_now_iso(),
            batch_size=batch_size,
            config_summaries=summaries,
            run_dir=str(run_dir),
        )
        write_json(run_dir / "manifest.json", run_manifest.model_dump(mode="json"))
        write_json(ARTIFACT_ROOT / "runs" / "latest.json", run_manifest.model_dump(mode="json"))
        status = _advance_status(
            status,
            started=started,
            current_task_id="",
            current_task_index=0,
            processed_task_runs=_processed_count(
                run_dir,
                configs,
                eval_run_id=None if reuse_completed else resolved_run_id,
            ),
            count_eval_run_id=None if reuse_completed else resolved_run_id,
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
        failure = safe_failure_from_exception(exc, stage=status.phase)
        status = _advance_status(
            status,
            started=started,
            current_task_id=status.current_task_id,
            current_task_index=status.current_task_index,
            processed_task_runs=_processed_count(
                run_dir,
                configs,
                eval_run_id=None if reuse_completed else resolved_run_id,
            ),
            count_eval_run_id=None if reuse_completed else resolved_run_id,
        ).model_copy(
            update={
                "state": "failed",
                "phase": "failed",
                "updated_at": utc_now_iso(),
                "error_message": failure.error_code,
            }
        )
        _write_status(status)
        _log_event(run_dir, "run_failed", status=status, errors=[status.error_message])
        _emit(progress_callback, status, "run_failed")
        raise


async def run_task(
    task: EvalTask,
    config: EvalConfig,
    *,
    api: str,
    manifest: EvalDatasetManifest,
    client: EvalApiClient,
    settings: Settings,
    eval_run_id: str = "",
    report_id: str = "",
    run_started_at: str = "",
    root_run_contract_id: str = "",
    max_attempts: int = ANSWER_EVAL_MAX_ATTEMPTS,
    retry_slot_acquire: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
) -> EvalTaskResult:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    started = time.perf_counter()
    attempt_records: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        chat_kwargs: dict[str, Any] = {
            "api": api,
            "retrieval_profile": config.retrieval_profile,
            "retrieval_overrides": config.retrieval_overrides,
            "mode": config.mode,
        }
        if task.knowledge_base_ids:
            chat_kwargs["knowledge_base_ids"] = task.knowledge_base_ids
        try:
            async_run_chat = getattr(client, "run_chat_async", None)
            if callable(async_run_chat):
                payload = await async_run_chat(task.question, **chat_kwargs)
            else:
                # Compatibility for deterministic test clients. Production
                # HttpEvalApiClient uses the cancellable async path above.
                payload = await asyncio.to_thread(client.run_chat, task.question, **chat_kwargs)
        except Exception as exc:
            total_ms = int((time.perf_counter() - started) * 1000)
            failure = _failure_from_exception(exc, attempt=attempt)
            attempt_records.append(failure)
            retry_allowed = attempt < max_attempts and bool(failure.get("retryable", True))
            if retry_allowed and retry_slot_acquire is not None:
                retry_allowed = await retry_slot_acquire(failure)
            if retry_allowed:
                await asyncio.sleep(RetryPolicy().delay_seconds(attempt))
                continue
            return _failed_result(
                task,
                config,
                manifest,
                _failure_message(failure, attempt),
                total_ms,
                eval_run_id=eval_run_id,
                report_id=report_id,
                run_started_at=run_started_at,
                root_run_contract_id=root_run_contract_id,
                attempt_records=attempt_records,
                failure=failure,
            )
        total_ms = int((time.perf_counter() - started) * 1000)
        if payload.get("failed"):
            failure = _failure_from_payload(payload, attempt=attempt)
            attempt_records.append(failure)
            retry_allowed = attempt < max_attempts and bool(failure.get("retryable", True))
            if retry_allowed and retry_slot_acquire is not None:
                retry_allowed = await retry_slot_acquire(failure)
            if retry_allowed:
                await asyncio.sleep(RetryPolicy().delay_seconds(attempt))
                continue
            return _failed_result(
                task,
                config,
                manifest,
                _failure_message(failure, attempt),
                total_ms,
                eval_run_id=eval_run_id,
                report_id=report_id,
                run_started_at=run_started_at,
                root_run_contract_id=root_run_contract_id,
                payload=payload,
                attempt_records=attempt_records,
                failure=failure,
            )
        usage_event = dict(payload.get("usage") or {})
        usage_data = dict(usage_event.get("data") or {})
        retrieval = dict(usage_data.get("retrieval") or {})
        validation = dict(usage_data.get("citation_validation") or {})
        contract_ids = _contract_ids_from_payload(retrieval)
        retrieval_contract_ids = _retrieval_contract_ids_from_payload(retrieval)
        tool_contract_ids = _tool_contract_ids_from_payload(retrieval)
        effective_run_contract_id = root_run_contract_id or contract_ids.get("run_contract_id", "")
        if effective_run_contract_id:
            contract_ids["run_contract_id"] = effective_run_contract_id
        answer = str(payload.get("answer") or "")
        # The benchmark client is intentionally public-API-only.  The chat
        # response already carries document/chunk provenance in its evidence
        # and stage events, so do not resolve chunk IDs through PostgreSQL.
        prefusion, reranked = _extract_candidates(retrieval)
        evidence = list(retrieval.get("evidence") or [])
        cited_ids = [str(item) for item in validation.get("citations", [])]
        cited_chunk_ids = _cited_chunk_ids(cited_ids, evidence)
        cited_document_ids = _cited_document_ids(cited_ids, evidence)
        cited_urls = _cited_urls(cited_ids, evidence)
        gold_urls = [item.source_url for item in task.gold_evidence if item.source_url]
        kiwix_ok = await asyncio.to_thread(_all_urls_ok, client, [*gold_urls, *cited_urls])
        scores = score_task(
            task,
            answer=answer,
            reranked=reranked,
            prefusion=prefusion,
            cited_chunk_ids=cited_chunk_ids,
            cited_document_ids=cited_document_ids,
            kiwix_url_ok=kiwix_ok,
        )
        diagnosis = diagnose_answer_task(task, status="completed", scores=scores)
        retrieval_latency = _retrieval_latency_ms(retrieval)
        timings_ms = _combined_timings_ms(usage_data, retrieval, validation)
        model_call_ids = _model_call_ids(validation, retrieval)
        reported_cost = _cost(validation)
        usage = {
            "input_tokens": _usage_int(validation.get("usage"), "prompt_tokens"),
            "output_tokens": _usage_int(validation.get("usage"), "completion_tokens"),
            "total_tokens": _usage_int(validation.get("usage"), "total_tokens"),
            "provider_cost_usd": reported_cost,
            "provider_cost_source": "reported" if reported_cost is not None else None,
            "model_calls": len(model_call_ids) if model_call_ids else None,
            "model_call_ids": model_call_ids,
            "attempts": attempt,
            "retry_errors": [str(record.get("safe_message") or record.get("code") or "") for record in attempt_records],
            "attempt_records": attempt_records,
            "answerability_status": validation.get("answerability_status"),
            "insufficient_evidence": validation.get("insufficient_evidence"),
            "raw_generation_usage": validation.get("usage", {}),
        }
        return EvalTaskResult(
            task_id=task.task_id,
            config_id=config.config_id,
            config_hash=config.config_hash,
            eval_run_id=eval_run_id,
            report_id=report_id,
            run_started_at=run_started_at,
            dataset_hash=manifest.dataset_hash,
            status="completed",
            question=task.question,
            answer=answer,
            citations=cited_ids,
            cited_chunk_ids=cited_chunk_ids,
            cited_document_ids=cited_document_ids,
            retrieved_candidates=prefusion,
            reranked_candidates=reranked,
            mode_selected="harness" if _used_harness(retrieval) else "normal",
            run_contract_id=effective_run_contract_id,
            execution_path=_execution_path(retrieval),
            path_selection_reason=_path_selection_reason(config, retrieval),
            retrieval_contract_ids=retrieval_contract_ids,
            tool_contract_ids=tool_contract_ids,
            step_events=_step_events_from_payload(payload, config=config, attempt=attempt),
            attempts=attempt,
            last_successful_stage="task_completed",
            latency_ms={"total": total_ms, "retrieval": retrieval_latency, **timings_ms},
            usage=usage,
            scores=scores,
            diagnosis=diagnosis,
            errors=[],
            query_run_id=str(payload.get("query_run_id")) if payload.get("query_run_id") else None,
            trace_id=str(payload.get("trace_id")) if payload.get("trace_id") else None,
            server_terminal_event=bool(payload.get("server_terminal_event", False)),
            last_sequence=int(payload.get("last_sequence") or 0),
            corpus={
                "snapshot_id": manifest.snapshot_id,
                "index_version": manifest.index_version,
                "zim_checksum": manifest.zim_checksum,
            },
            model_aliases=config.model_aliases,
            contract_ids=contract_ids,
        )
    total_ms = int((time.perf_counter() - started) * 1000)
    return _failed_result(
        task,
        config,
        manifest,
        "chat failed without result",
        total_ms,
        eval_run_id=eval_run_id,
        report_id=report_id,
        run_started_at=run_started_at,
        root_run_contract_id=root_run_contract_id,
        attempt_records=attempt_records,
        failure={
            "stage": "chat",
            "code": "empty_result",
            "retryable": True,
            "safe_message": "chat failed without result",
            "last_successful_stage": "",
            "attempt": max_attempts,
        },
    )


async def _run_config_with_backfill(
    status: EvalRunStatus,
    *,
    started: float,
    run_dir: Path,
    configs: list[EvalConfig],
    config: EvalConfig,
    tasks: list[EvalTask],
    existing: dict[str, EvalTaskResult],
    results_path: Path,
    report_id: str,
    run_started_at: str,
    reuse_completed: bool,
    root_run_contract_id: str,
    batch_size: int,
    api: str,
    manifest: EvalDatasetManifest,
    client: EvalApiClient,
    settings: Settings,
    progress_callback: EvalRunProgressCallback | None,
) -> EvalRunStatus:
    eligible = [
        (task_index, task)
        for task_index, task in enumerate(tasks, start=1)
        if task.task_id not in existing or existing[task.task_id].status not in {"completed", "reused"}
    ]
    pending: dict[asyncio.Task[EvalTaskResult], tuple[int, EvalTask]] = {}
    next_index = 0

    async def schedule_next(current_status: EvalRunStatus) -> EvalRunStatus:
        nonlocal next_index
        if next_index >= len(eligible):
            return current_status
        task_index, task = eligible[next_index]
        next_index += 1
        updated = _advance_status(
            current_status,
            started=started,
            current_task_id=task.task_id,
            current_task_index=task_index,
            processed_task_runs=_processed_count(
                run_dir,
                configs,
                eval_run_id=None if reuse_completed else current_status.run_id,
            ),
            count_eval_run_id=None if reuse_completed else current_status.run_id,
        )
        _write_status(updated)
        _log_event(run_dir, "task_started", status=updated, config=config, task=task)
        _emit(progress_callback, updated, "task_started")
        future = asyncio.create_task(
            run_task(
                task,
                config,
                api=api,
                manifest=manifest,
                client=client,
                settings=settings,
                eval_run_id=status.run_id,
                report_id=report_id,
                run_started_at=run_started_at,
                root_run_contract_id=root_run_contract_id,
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
                existing[task.task_id] = result
                status = _advance_status(
                    status,
                    started=started,
                    current_task_id=task.task_id,
                    current_task_index=task_index,
                    processed_task_runs=_processed_count(
                        run_dir,
                        configs,
                        eval_run_id=None if reuse_completed else status.run_id,
                    ),
                    count_eval_run_id=None if reuse_completed else status.run_id,
                    last_latency_ms=int(result.latency_ms.get("total", 0)),
                )
                _write_status(status)
                event = "task_completed" if result.status == "completed" else "task_failed"
                _log_event(run_dir, event, status=status, config=config, task=task, errors=result.errors)
                _emit(progress_callback, status, event)
                status = await schedule_next(status)
        return status
    finally:
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*pending.keys(), return_exceptions=True)


def summarize_config(config: EvalConfig, tasks: list[EvalTask], results: list[EvalTaskResult]) -> ConfigSummary:
    task_by_id = {task.task_id: task for task in tasks}
    latest = list({result.task_id: result for result in results}.values())
    completed = [result for result in latest if result.scores is not None]
    answerable = [
        result for result in completed if (task := task_by_id.get(result.task_id)) is not None and not task.unanswerable
    ]
    unanswerable = [
        result for result in completed if (task := task_by_id.get(result.task_id)) is not None and task.unanswerable
    ]
    metrics = _aggregate_results(completed, answerable, unanswerable)
    metrics.update(
        root_cause_count_metrics(answer_result_diagnosis(task_by_id.get(result.task_id), result) for result in latest)
    )
    by_family: dict[str, dict[str, float]] = {}
    for family in sorted({task.task_family for task in tasks}):
        family_latest = [
            result
            for result in latest
            if (task := task_by_id.get(result.task_id)) is not None and task.task_family == family
        ]
        family_results = [
            result
            for result in completed
            if (task := task_by_id.get(result.task_id)) is not None and task.task_family == family
        ]
        family_answerable = [
            result
            for result in family_results
            if (task := task_by_id.get(result.task_id)) is not None and not task.unanswerable
        ]
        family_unanswerable = [
            result
            for result in family_results
            if (task := task_by_id.get(result.task_id)) is not None and task.unanswerable
        ]
        family_metrics = _aggregate_results(family_results, family_answerable, family_unanswerable)
        family_metrics.update(
            root_cause_count_metrics(
                answer_result_diagnosis(task_by_id.get(result.task_id), result) for result in family_latest
            )
        )
        by_family[str(family)] = family_metrics
    failed = [
        result.task_id
        for result in latest
        if result.status == "failed"
        or result.scores is None
        or _task_failed_threshold(task_by_id.get(result.task_id), result.scores)
    ]
    contract_ids = _summary_contract_ids(latest)
    return ConfigSummary(
        config_id=config.config_id,
        config_hash=config.config_hash,
        task_count=len(latest),
        metrics=metrics,
        by_family=by_family,
        failed_task_ids=sorted(set(failed)),
        errors=[error for result in latest for error in result.errors] + _contract_mix_errors(contract_ids),
        contract_ids=contract_ids,
    )


def _extract_candidates(retrieval: dict[str, Any]) -> tuple[list[CandidateRef], list[CandidateRef]]:
    events = [dict(item) for item in retrieval.get("events", []) if isinstance(item, dict)]
    prefusion_raw = (
        _stage_candidates(events, "rrf") or _stage_candidates(events, "bm25") or _stage_candidates(events, "dense")
    )
    context_raw = _stage_candidates(events, "context")
    evidence_raw = _evidence_candidates(retrieval)
    reranked_raw = _stage_candidates(events, "rerank") or context_raw or evidence_raw or prefusion_raw
    prefusion_raw = prefusion_raw or context_raw or evidence_raw
    evidence_by_chunk = {
        str(item.get("chunk_id")): item
        for item in retrieval.get("evidence", [])
        if isinstance(item, dict) and item.get("chunk_id")
    }
    return (
        _candidate_refs(prefusion_raw, evidence_by_chunk, "prefusion"),
        _candidate_refs(reranked_raw, evidence_by_chunk, "rerank"),
    )


def _candidate_refs(
    candidates: list[dict[str, Any]],
    evidence_by_chunk: dict[str, dict[str, Any]],
    stage: str,
) -> list[CandidateRef]:
    output: list[CandidateRef] = []
    for rank, item in enumerate(candidates[:20], start=1):
        chunk_id = str(item.get("chunk_id") or "")
        ref = evidence_by_chunk.get(chunk_id, {})
        output.append(
            CandidateRef(
                chunk_id=chunk_id,
                document_id=str(item.get("document_id") or ref.get("document_id") or ""),
                section_id=str(item.get("section_id") or ref.get("section_id") or ""),
                title=str(item.get("title") or ref.get("title") or ""),
                source_url=str(item.get("source_url") or ref.get("source_url") or ""),
                rank=rank,
                stage=stage,
                scores={key: float(value) for key, value in dict(item.get("scores") or {}).items()},
            )
        )
    return output


def _stage_candidates(events: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    for event in events:
        if event.get("stage") == stage and isinstance(event.get("candidates"), list):
            return [dict(item) for item in event["candidates"] if isinstance(item, dict)]
    return []


def _evidence_candidates(retrieval: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = retrieval.get("evidence")
    if not isinstance(evidence, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id") or "")
        if not chunk_id:
            continue
        candidates.append(
            {
                "chunk_id": chunk_id,
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                "scores": item.get("scores") if isinstance(item.get("scores"), dict) else {},
            }
        )
    return candidates


def _cited_chunk_ids(cited_ids: list[str], evidence: list[Any]) -> list[str]:
    by_id = {str(item.get("evidence_id")): str(item.get("chunk_id")) for item in evidence if isinstance(item, dict)}
    return [by_id[item] for item in cited_ids if item in by_id]


def _cited_document_ids(cited_ids: list[str], evidence: list[Any]) -> list[str]:
    by_id = {
        str(item.get("evidence_id")): str(item.get("document_id") or "") for item in evidence if isinstance(item, dict)
    }
    return [by_id[item] for item in cited_ids if item in by_id and by_id[item]]


def _cited_urls(cited_ids: list[str], evidence: list[Any]) -> list[str]:
    by_id = {str(item.get("evidence_id")): str(item.get("source_url")) for item in evidence if isinstance(item, dict)}
    return [by_id[item] for item in cited_ids if item in by_id and by_id[item]]


def _all_urls_ok(client: EvalApiClient, urls: list[str]) -> bool:
    return all(client.url_ok(url) for url in urls if url)


def _eval_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "retrieval": {
            "top_k": 20,
            "bm25_top_k": 100,
            "dense_top_k": 100,
            "fusion_top_k": 60,
            "rerank_top_k": 50,
        },
        # Keep generation context aligned with the profile default. The
        # benchmark still measures top-20 retrieval candidates separately.
    }
    return _deep_merge(merged, overrides)


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = {**left}
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _filter_configs(configs: list[EvalConfig], config_ids: set[str] | None) -> list[EvalConfig]:
    if not config_ids:
        return configs
    filtered = [config for config in configs if config.config_id in config_ids]
    missing = sorted(config_ids - {config.config_id for config in filtered})
    if missing:
        raise ValueError(f"unknown eval config IDs: {missing}")
    return filtered


def _aggregate_results(
    results: list[EvalTaskResult],
    answerable: list[EvalTaskResult],
    unanswerable: list[EvalTaskResult],
) -> dict[str, float]:
    retrieval_base = answerable or results
    scores = [result.scores for result in results if result.scores is not None]
    retrieval_scores = [result.scores for result in retrieval_base if result.scores is not None]
    latencies = [float(result.latency_ms.get("total", 0)) for result in results]
    unanswerable_scores = [result.scores for result in unanswerable if result.scores is not None]
    metrics = {
        "page_recall_at_1": aggregate(score.page_recall["1"] for score in retrieval_scores),
        "page_recall_at_5": aggregate(score.page_recall["5"] for score in retrieval_scores),
        "page_recall_at_10": aggregate(score.page_recall["10"] for score in retrieval_scores),
        "page_recall_at_20": aggregate(score.page_recall["20"] for score in retrieval_scores),
        "document_recall_at_1": aggregate(score.document_recall.get("1", 0.0) for score in retrieval_scores),
        "document_recall_at_5": aggregate(score.document_recall.get("5", 0.0) for score in retrieval_scores),
        "document_recall_at_10": aggregate(score.document_recall.get("10", 0.0) for score in retrieval_scores),
        "document_mrr_at_10": aggregate(score.document_mrr_at_10 for score in retrieval_scores),
        "document_reranker_gold_delta": aggregate(
            score.document_reranker_gold_delta or 0.0 for score in retrieval_scores
        ),
        "document_ndcg_at_10": aggregate(score.document_ndcg_at_10 for score in retrieval_scores),
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
        "exact_match": aggregate(score.exact_match for score in scores),
        "token_f1": aggregate(score.token_f1 for score in scores),
        "rouge_l": aggregate(score.rouge_l for score in scores),
        "citation_precision": aggregate(score.citation_precision for score in scores),
        "citation_recall": aggregate(score.citation_recall for score in scores),
        "document_citation_precision": aggregate(score.document_citation_precision for score in scores),
        "document_citation_recall": aggregate(score.document_citation_recall for score in scores),
        "unsupported_claim_rate": aggregate(score.unsupported_claim_rate for score in scores),
        "cited_hard_negative_rate": aggregate(score.cited_hard_negative_rate for score in scores),
        "kiwix_url_ok": aggregate(score.kiwix_url_ok for score in scores),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "model_calls": aggregate(
            float(result.usage["model_calls"])
            for result in results
            if isinstance(result.usage.get("model_calls"), int | float)
        ),
        "tokens": aggregate(
            float(result.usage["total_tokens"])
            for result in results
            if isinstance(result.usage.get("total_tokens"), int | float)
        ),
        "openrouter_cost_usd": aggregate(
            float(result.usage["provider_cost_usd"])
            for result in results
            if isinstance(result.usage.get("provider_cost_usd"), int | float)
        ),
    }
    if unanswerable_scores:
        metrics["unanswerable_accuracy"] = aggregate(score.unanswerable_accuracy for score in unanswerable_scores)
        metrics["soft_unanswerable_context_rate"] = aggregate(
            score.soft_unanswerable_context_rate for score in unanswerable_scores
        )
    metrics.update(_stage_latency_metrics(results))
    return metrics


def _task_failed_threshold(task: EvalTask | None, scores: TaskScores | None) -> bool:
    if task is None or scores is None:
        return True
    if task.unanswerable:
        return scores.unanswerable_accuracy < 1.0
    if task.evaluation_granularity == "document" or task.gold_document_ids:
        return scores.document_recall.get("10", 0.0) < 1.0 or scores.document_citation_precision < 1.0
    return (
        scores.page_recall["10"] < 1.0
        or scores.chunk_recall["20"] < 1.0
        or scores.citation_precision < 1.0
        or scores.cited_hard_negative_rate > 0.0
    )


def _failed_result(
    task: EvalTask,
    config: EvalConfig,
    manifest: EvalDatasetManifest,
    error: str,
    latency_ms: int,
    *,
    eval_run_id: str = "",
    report_id: str = "",
    run_started_at: str = "",
    root_run_contract_id: str = "",
    payload: dict[str, Any] | None = None,
    attempt_records: list[dict[str, Any]] | None = None,
    failure: dict[str, Any] | None = None,
) -> EvalTaskResult:
    payload = dict(payload or {})
    failure = dict(failure or {})
    usage_event = dict(payload.get("usage") or {})
    usage_data = dict(usage_event.get("data") or {})
    failed_event = dict(payload.get("failed_event") or {})
    failed_envelope = dict(failed_event.get("data") or {})
    failed_data = dict(failed_envelope.get("data") or failed_envelope)
    retrieval = dict(usage_data.get("retrieval") or failed_data.get("retrieval") or {})
    prefusion = _candidate_refs_sync(_safe_candidates_from_retrieval(retrieval, "prefusion"), "prefusion")
    reranked = _candidate_refs_sync(_safe_candidates_from_retrieval(retrieval, "rerank"), "rerank")
    failure_stage = str(failure.get("stage") or failed_data.get("stage") or "chat")
    failure_code = str(failure.get("code") or failed_data.get("code") or "chat_failed")
    retrieval_scores = (
        score_task(
            task,
            answer="",
            reranked=reranked,
            prefusion=prefusion,
            cited_chunk_ids=[],
            cited_document_ids=[],
            kiwix_url_ok=True,
        )
        if retrieval
        else None
    )
    last_successful_stage = str(failure.get("last_successful_stage") or failed_data.get("last_successful_stage") or "")
    attempts = max(
        int(failure.get("attempt") or failed_data.get("attempt") or 1),
        len(attempt_records or []),
    )
    return EvalTaskResult(
        task_id=task.task_id,
        config_id=config.config_id,
        config_hash=config.config_hash,
        eval_run_id=eval_run_id,
        report_id=report_id,
        run_started_at=run_started_at,
        dataset_hash=manifest.dataset_hash,
        status="failed",
        question=task.question,
        retrieved_candidates=prefusion,
        reranked_candidates=reranked,
        mode_selected="harness" if _used_harness(retrieval) else "normal",
        run_contract_id=root_run_contract_id,
        execution_path=_execution_path(retrieval),
        path_selection_reason=_path_selection_reason(config, retrieval),
        retrieval_contract_ids=_retrieval_contract_ids_from_payload(retrieval),
        tool_contract_ids=_tool_contract_ids_from_payload(retrieval),
        step_events=_step_events_from_payload(payload, config=config, attempt=attempts, failure=failure),
        failure_stage=failure_stage,
        failure_code=failure_code,
        failure_retryable=bool(
            failure.get("retryable") if "retryable" in failure else failed_data.get("retryable", True)
        ),
        attempts=attempts,
        last_successful_stage=last_successful_stage,
        latency_ms={"total": latency_ms, "retrieval": _retrieval_latency_ms(retrieval)},
        usage={
            "attempts": attempts,
            "attempt_records": attempt_records or [],
            "retrieval_snapshot": _safe_retrieval_snapshot(retrieval),
            "failure": {
                "stage": failure_stage,
                "code": failure_code,
                "retryable": bool(
                    failure.get("retryable") if "retryable" in failure else failed_data.get("retryable", True)
                ),
                "attempt": attempts,
                "last_successful_stage": last_successful_stage,
            },
        },
        diagnosis=diagnose_answer_task(task, status="failed", scores=None),
        retrieval_scores=retrieval_scores,
        errors=[error],
        query_run_id=str(payload.get("query_run_id")) if payload.get("query_run_id") else None,
        trace_id=str(payload.get("trace_id") or failed_data.get("trace_id"))
        if payload.get("trace_id") or failed_data.get("trace_id")
        else None,
        server_terminal_event=bool(payload.get("server_terminal_event", False)),
        last_sequence=int(payload.get("last_sequence") or 0),
        corpus={
            "snapshot_id": manifest.snapshot_id,
            "index_version": manifest.index_version,
            "zim_checksum": manifest.zim_checksum,
        },
        model_aliases=config.model_aliases,
        contract_ids={"run_contract_id": root_run_contract_id} if root_run_contract_id else {},
    )


def _retrieval_latency_ms(retrieval: dict[str, Any]) -> int:
    for event in reversed(list(retrieval.get("events", []))):
        if isinstance(event, dict) and event.get("stage") == "context" and event.get("latency_ms") is not None:
            return int(event["latency_ms"])
    return 0


def _model_call_ids(validation: dict[str, Any], retrieval: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    metadata = validation.get("model_gateway")
    if isinstance(metadata, dict):
        value = metadata.get("provider_request_id") or metadata.get("call_id")
        if isinstance(value, str) and value:
            ids.append(value)
    for event in retrieval.get("events", []):
        if not isinstance(event, dict):
            continue
        metadata = event.get("model_gateway")
        if isinstance(metadata, dict):
            value = metadata.get("provider_request_id") or metadata.get("call_id")
            if isinstance(value, str) and value:
                ids.append(value)
    return sorted(set(ids))


def _safe_retrieval_snapshot(retrieval: dict[str, Any]) -> dict[str, Any]:
    """Keep provenance useful for failed generation without persisting content."""

    if not retrieval:
        return {}
    evidence = []
    for item in list(retrieval.get("evidence") or [])[:20]:
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                key: item.get(key)
                for key in ("evidence_id", "chunk_id", "document_id", "document_version_id", "title", "source_url")
                if item.get(key) is not None
            }
        )
    return {
        "index_contract_id": retrieval.get("index_contract_id"),
        "run_contract_id": retrieval.get("run_contract_id"),
        "evidence": evidence,
        "candidate_count": sum(
            len(event.get("candidates") or []) for event in retrieval.get("events", []) if isinstance(event, dict)
        ),
        "answerability_status": (retrieval.get("answerability") or {}).get("status")
        if isinstance(retrieval.get("answerability"), dict)
        else None,
    }


def _combined_timings_ms(
    usage_data: dict[str, Any],
    retrieval: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, int]:
    timings: dict[str, int] = {}
    if isinstance(usage_data.get("timings_ms"), dict):
        timings.update(_safe_timing_dict(usage_data["timings_ms"]))
    for event in retrieval.get("events", []):
        if not isinstance(event, dict):
            continue
        if event.get("stage") == "timings" and isinstance(event.get("timings_ms"), dict):
            timings.update(_safe_timing_dict(event["timings_ms"]))
        if event.get("stage") == "harness" and isinstance(event.get("timings_ms"), dict):
            timings.update(_safe_timing_dict(event["timings_ms"]))
        if event.get("stage") == "harness_tool" and isinstance(event.get("latency_ms"), int | float):
            timings["extended_tool_search_total"] = timings.get("extended_tool_search_total", 0) + int(
                event["latency_ms"]
            )
    if isinstance(validation.get("timings_ms"), dict):
        timings.update(_safe_timing_dict(validation["timings_ms"]))
    return timings


def _safe_timing_dict(payload: dict[Any, Any]) -> dict[str, int]:
    return {
        str(key): max(0, int(value)) for key, value in payload.items() if isinstance(value, int | float) and str(key)
    }


def _step_events_from_payload(
    payload: dict[str, Any],
    *,
    config: EvalConfig,
    attempt: int,
    failure: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    usage_event = dict(payload.get("usage") or {})
    usage_data = dict(usage_event.get("data") or {})
    failed_event = dict(payload.get("failed_event") or {})
    failed_envelope = dict(failed_event.get("data") or {})
    failed_data = dict(failed_envelope.get("data") or failed_envelope)
    retrieval = dict(usage_data.get("retrieval") or failed_data.get("retrieval") or {})
    events = list(retrieval.get("events") or [])
    output: list[dict[str, Any]] = [
        {
            "name": "question_received",
            "status": "completed",
            "attempt": attempt,
            "reason": "eval_task",
        },
        {
            "name": "path_selected",
            "status": "completed",
            "attempt": attempt,
            "reason": _path_selection_reason(config, retrieval),
            "execution_path": _execution_path(retrieval),
        },
    ]
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "")
        step = _step_name_for_stage(stage)
        if not step:
            continue
        seen.add(step)
        output.append(_step_event_from_retrieval_event(step, event, attempt=attempt))
    if "extended_completed" not in seen:
        output.append(
            {
                "name": "extended_skipped",
                "status": "skipped",
                "attempt": attempt,
                "reason": "execution_path_not_harness",
            }
        )
    if failure:
        output.append(
            {
                "name": f"{str(failure.get('stage') or 'task')}_failed",
                "status": "failed",
                "attempt": attempt,
                "code": str(failure.get("code") or "unknown"),
                "safe_message": str(failure.get("safe_message") or ""),
                "retryable": bool(failure.get("retryable", True)),
                "last_successful_stage": str(failure.get("last_successful_stage") or ""),
            }
        )
        output.append({"name": "task_failed", "status": "failed", "attempt": attempt})
    else:
        output.append({"name": "answer_generation_completed", "status": "completed", "attempt": attempt})
        output.append({"name": "citation_check_completed", "status": "completed", "attempt": attempt})
        output.append({"name": "task_completed", "status": "completed", "attempt": attempt})
    return output


def _step_name_for_stage(stage: str) -> str:
    return {
        "bm25": "bm25_completed",
        "dense": "dense_completed",
        "rrf": "fusion_completed",
        "rerank": "rerank_completed",
        "context": "context_selected",
        "answerability": "answerability_checked",
        "harness_tool": "extended_tool_search_completed",
        "harness": "extended_completed",
    }.get(stage, "")


def _step_event_from_retrieval_event(name: str, event: dict[str, Any], *, attempt: int) -> dict[str, Any]:
    candidates = list(event.get("candidates") or [])
    chunk_ids = [str(item.get("chunk_id")) for item in candidates if isinstance(item, dict) and item.get("chunk_id")]
    payload: dict[str, Any] = {
        "name": name,
        "status": "completed",
        "attempt": attempt,
        "latency_ms": int(event.get("latency_ms") or event.get("stage_latency_ms") or 0),
        "candidate_count": int(event.get("count") or len(candidates)),
        "chunk_ids": chunk_ids[:20],
    }
    if event.get("run_contract_id"):
        payload["retrieval_contract_id"] = str(event["run_contract_id"])
    if event.get("stop_reason"):
        payload["reason"] = str(event["stop_reason"])
    if event.get("decision"):
        payload["decision"] = event["decision"]
    return payload


def _failure_from_exception(exc: Exception, *, attempt: int) -> dict[str, Any]:
    failure = safe_failure_from_exception(exc, stage="api_request", attempt=attempt)
    output: dict[str, Any] = {
        "stage": failure.stage,
        "code": failure.error_code,
        "retryable": failure.retryable,
        "attempt": attempt,
        "last_successful_stage": "",
        "safe_message": failure.error_code,
    }
    metadata = getattr(exc, "metadata", None)
    if isinstance(metadata, dict):
        output["model_call_metadata"] = {
            key: metadata.get(key)
            for key in (
                "model_alias",
                "provider_request_id",
                "finish_reason",
                "max_output_tokens",
                "latency_ms",
                "usage",
            )
            if metadata.get(key) is not None
        }
    return output


def _failure_from_payload(payload: dict[str, Any], *, attempt: int) -> dict[str, Any]:
    failed_event = dict(payload.get("failed_event") or {})
    envelope = dict(failed_event.get("data") or {})
    data = dict(envelope.get("data") or envelope)
    nested_failure = dict(payload.get("failure") or {})
    code = str(data.get("code") or nested_failure.get("code") or payload.get("error") or "run_failed")
    non_retryable = {
        "CONTRACT_MISMATCH",
        "CHECKSUM_MISMATCH",
        "PARSER_REJECTED",
        "KB_NOT_READY",
        "VALIDATION_ERROR",
    }
    return {
        "stage": str(data.get("stage") or nested_failure.get("stage") or "chat"),
        "code": code,
        "retryable": bool(data.get("retryable", nested_failure.get("retryable", code not in non_retryable))),
        "attempt": int(data.get("attempt") or attempt),
        "last_successful_stage": str(data.get("last_successful_stage") or ""),
        "safe_message": str(data.get("safe_message") or data.get("error") or payload.get("error") or "run failed"),
    }


def _failure_message(failure: dict[str, Any], attempts: int) -> str:
    stage = str(failure.get("stage") or "chat")
    code = str(failure.get("code") or "run_failed")
    return f"{stage}:{code} after {attempts} attempts"


def _execution_path(retrieval: dict[str, Any]) -> str:
    return "harness" if _used_harness(retrieval) else "normal"


def _path_selection_reason(config: EvalConfig, retrieval: dict[str, Any]) -> str:
    if config.mode == "extended":
        return "user_selected"
    if _used_harness(retrieval):
        return "auto_question_type"
    return "config_default"


def _safe_candidates_from_retrieval(retrieval: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    if not retrieval:
        return []
    if stage == "rerank":
        candidates = _stage_candidates(list(retrieval.get("events") or []), "rerank")
        return candidates or _evidence_candidates(retrieval)
    return _stage_candidates(list(retrieval.get("events") or []), "rrf") or _evidence_candidates(retrieval)


def _candidate_refs_sync(candidates: list[dict[str, Any]], stage: str) -> list[CandidateRef]:
    refs: list[CandidateRef] = []
    for rank, item in enumerate(candidates[:20], start=1):
        if not isinstance(item, dict) or not item.get("chunk_id"):
            continue
        refs.append(
            CandidateRef(
                chunk_id=str(item.get("chunk_id") or ""),
                document_id=str(item.get("document_id") or ""),
                section_id=str(item.get("section_id") or ""),
                title=str(item.get("title") or ""),
                source_url=str(item.get("source_url") or ""),
                rank=int(item.get("rank") or rank),
                stage=stage,
                scores={
                    str(key): float(value)
                    for key, value in dict(item.get("scores") or {}).items()
                    if isinstance(value, int | float)
                },
            )
        )
    return refs


def _contract_ids_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(payload[key])
        for key in ("index_contract_id", "run_contract_id")
        if isinstance(payload.get(key), str) and payload.get(key)
    }


def _eval_root_run_contract_id(manifest: EvalDatasetManifest, config: EvalConfig) -> str:
    return "sha256:" + stable_json_hash(
        {
            "schema": "eval_answer_run_contract_v1",
            "semantics_version": EVAL_SEMANTICS_VERSION,
            "dataset_hash": manifest.dataset_hash,
            "config_id": config.config_id,
            "config_hash": config.config_hash,
            "retrieval_profile": config.retrieval_profile,
            "retrieval_overrides": config.retrieval_overrides,
            "mode": config.mode,
            "model_aliases": config.model_aliases,
        }
    )


def _retrieval_contract_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    direct = payload.get("run_contract_id")
    if isinstance(direct, str) and direct:
        ids.append(direct)
    for event in payload.get("events", []):
        if isinstance(event, dict):
            value = event.get("run_contract_id")
            if isinstance(value, str) and value:
                ids.append(value)
    return sorted(set(ids))


def _tool_contract_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for event in payload.get("events", []):
        if isinstance(event, dict) and event.get("stage") == "harness_tool":
            value = event.get("run_contract_id")
            if isinstance(value, str) and value:
                ids.append(value)
    return sorted(set(ids))


def _summary_contract_ids(results: list[EvalTaskResult]) -> dict[str, list[str]]:
    keys = sorted({key for result in results for key in result.contract_ids})
    return {
        key: sorted({result.contract_ids[key] for result in results if result.contract_ids.get(key)}) for key in keys
    }


def _contract_mix_errors(contract_ids: dict[str, list[str]]) -> list[str]:
    return [f"mixed_contract_ids:{key}" for key, ids in contract_ids.items() if len(ids) > 1]


def _stage_latency_metrics(results: list[EvalTaskResult]) -> dict[str, float]:
    keys = sorted({key for result in results for key in result.latency_ms if key not in {"total", "retrieval"}})
    metrics: dict[str, float] = {}
    for key in keys:
        values = [float(result.latency_ms[key]) for result in results if key in result.latency_ms]
        metrics[f"stage_latency_{key}_p50_ms"] = percentile(values, 50)
        metrics[f"stage_latency_{key}_p95_ms"] = percentile(values, 95)
    return metrics


def load_eval_run_status(run_id: str) -> EvalRunStatus:
    direct = _runs_root() / run_id / "status.json"
    if direct.exists():
        return EvalRunStatus.model_validate(read_json(direct))
    matches = sorted(_runs_root().glob(f"*/{run_id}/status.json"))
    if not matches:
        raise FileNotFoundError(f"no answer eval status found for run_id {run_id}")
    return EvalRunStatus.model_validate(read_json(matches[-1]))


def load_latest_eval_run_status() -> EvalRunStatus:
    latest = _runs_root() / "latest-status.json"
    if not latest.exists():
        raise FileNotFoundError("no answer eval status is available yet")
    return EvalRunStatus.model_validate(read_json(latest))


def _initial_status(
    manifest: EvalDatasetManifest,
    *,
    run_id: str,
    report_id: str,
    run_dir: Path,
    configs: list[EvalConfig],
    started_at: str,
    batch_size: int,
    processed_task_runs: int = 0,
    completed_task_runs: int = 0,
    failed_task_runs: int = 0,
) -> EvalRunStatus:
    return EvalRunStatus(
        run_id=run_id,
        report_id=report_id,
        state="running",
        phase="preparing",
        suite=manifest.dataset_name,
        dataset_hash=manifest.dataset_hash,
        dataset_path=manifest.jsonl_path,
        run_dir=str(run_dir),
        batch_size=batch_size,
        total_configs=len(configs),
        total_tasks=manifest.task_count,
        total_task_runs=manifest.task_count * len(configs),
        processed_task_runs=processed_task_runs,
        completed_task_runs=completed_task_runs,
        failed_task_runs=failed_task_runs,
        started_at=started_at,
        updated_at=started_at,
    )


def _advance_status(
    status: EvalRunStatus,
    *,
    started: float,
    current_task_id: str,
    current_task_index: int,
    processed_task_runs: int,
    count_eval_run_id: str | None = None,
    last_latency_ms: int | None = None,
) -> EvalRunStatus:
    elapsed = max(0.0, time.perf_counter() - started)
    completed = _completed_count(Path(status.run_dir), eval_run_id=count_eval_run_id)
    failed = _failed_count(Path(status.run_dir), eval_run_id=count_eval_run_id)
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


def _processed_count(run_dir: Path, configs: list[EvalConfig], *, eval_run_id: str | None = None) -> int:
    return sum(len(_latest_results(_results_path(run_dir, config), eval_run_id=eval_run_id)) for config in configs)


def _completed_count(run_dir: Path, *, eval_run_id: str | None = None) -> int:
    return _status_count(run_dir, {"completed", "reused"}, eval_run_id=eval_run_id)


def _failed_count(run_dir: Path, *, eval_run_id: str | None = None) -> int:
    return _status_count(run_dir, {"failed"}, eval_run_id=eval_run_id)


def _status_count(run_dir: Path, statuses: set[str], *, eval_run_id: str | None = None) -> int:
    return sum(
        1
        for path in (run_dir / "results").glob("*.jsonl")
        for result in _latest_results(path, eval_run_id=eval_run_id).values()
        if result.status in statuses
    )


def _latest_results(
    path: Path,
    *,
    eval_run_id: str | None = None,
    dataset_hash: str | None = None,
    config_hash: str | None = None,
) -> dict[str, EvalTaskResult]:
    rows = read_jsonl(path, EvalTaskResult)
    if eval_run_id is not None:
        rows = [row for row in rows if row.eval_run_id == eval_run_id]
    if dataset_hash is not None:
        rows = [row for row in rows if row.dataset_hash == dataset_hash]
    if config_hash is not None:
        rows = [row for row in rows if row.config_hash == config_hash]
    return {row.task_id: row for row in rows}


def _results_path(run_dir: Path, config: EvalConfig) -> Path:
    return run_dir / "results" / f"{config.config_id}-{config.config_hash[:12]}.jsonl"


def _write_status(status: EvalRunStatus) -> None:
    write_json(Path(status.run_dir) / "status.json", status.model_dump(mode="json"))
    write_json(_runs_root() / "latest-status.json", status.model_dump(mode="json"))


def _log_event(
    run_dir: Path,
    event: str,
    *,
    status: EvalRunStatus,
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
            "processed_task_runs": status.processed_task_runs,
            "errors": errors or [],
        },
    )


def _emit(callback: EvalRunProgressCallback | None, status: EvalRunStatus, event: str) -> None:
    if callback is not None:
        callback(status, event)


def _runs_root() -> Path:
    return ARTIFACT_ROOT / "runs"


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return _format_elapsed(seconds)


def _used_harness(retrieval: dict[str, Any]) -> bool:
    return any(isinstance(event, dict) and event.get("stage") == "harness" for event in retrieval.get("events", []))


def _estimate_model_calls(config: EvalConfig, retrieval: dict[str, Any]) -> int:
    profile = get_retrieval_profile(config.retrieval_profile, overrides=config.retrieval_overrides)
    search_calls = len(
        [
            event
            for event in retrieval.get("events", [])
            if isinstance(event, dict) and event.get("stage") == "harness_tool"
        ]
    )
    retrieval_calls = 0
    if profile.retrieval.dense:
        retrieval_calls += max(1, search_calls)
    if profile.retrieval.rerank:
        retrieval_calls += max(1, search_calls)
    verifier_calls = 1 if profile.answer.verification.claim_verification_uses_llm else 0
    return 1 + retrieval_calls + verifier_calls


def _usage_int(usage: Any, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return int(value) if isinstance(value, int | float | str) and str(value).replace(".", "", 1).isdigit() else None


def _cost(validation: dict[str, Any]) -> float | None:
    for key in ("provider_cost", "cost", "total_cost"):
        value = validation.get(key)
        if isinstance(value, int | float | str):
            try:
                return float(value)
            except ValueError:
                return 0.0
    usage = validation.get("usage")
    if isinstance(usage, dict):
        for key in ("cost", "total_cost"):
            value = usage.get(key)
            if isinstance(value, int | float | str):
                try:
                    return float(value)
                except ValueError:
                    return 0.0
    return None
