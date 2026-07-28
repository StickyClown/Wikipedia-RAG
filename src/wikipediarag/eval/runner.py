from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TextIO

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.api_client import EvalApiClient, HttpEvalApiClient
from wikipediarag.eval.artifacts import ARTIFACT_ROOT, append_jsonl, read_json, read_jsonl, utc_now_iso, write_json
from wikipediarag.eval.corpus import load_chunk_refs
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
from wikipediarag.retrieval_profile import get_retrieval_profile

type EvalRunProgressCallback = Callable[[EvalRunStatus, str], None]


class EvalRunCliReporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def __call__(self, status: EvalRunStatus, event: str) -> None:
        print(format_eval_run_progress(status, event), file=self._stream, flush=True)


def format_eval_run_progress(status: EvalRunStatus, event: str) -> str:
    task = f" task_id={status.current_task_id}" if status.current_task_id else ""
    config = f" config={status.current_config_id}" if status.current_config_id else ""
    latency = f" last_latency_ms={status.last_latency_ms}" if status.last_latency_ms else ""
    eta = _format_eta(status.eta_seconds)
    return (
        f"[{_format_elapsed(status.elapsed_seconds)}] state={event}{config}"
        f" task={status.current_task_index}/{status.total_tasks}{task}"
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
    specs: list[tuple[str, dict[str, Any], Literal["normal", "extended", "auto"]]] = [
        (
            "bm25_only",
            {
                "retrieval": {"bm25": True, "dense": False, "fusion": "none", "rerank": False},
                "postprocess": {"parent_expansion": "off", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "dense_only",
            {
                "retrieval": {"bm25": False, "dense": True, "fusion": "none", "rerank": False},
                "postprocess": {"parent_expansion": "off", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "hybrid_rrf",
            {
                "retrieval": {"bm25": True, "dense": True, "fusion": "rrf", "rerank": False},
                "postprocess": {"parent_expansion": "off", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "hybrid_rerank",
            {
                "retrieval": {"bm25": True, "dense": True, "fusion": "rrf", "rerank": True},
                "postprocess": {"parent_expansion": "selective", "extended_search": "off"},
            },
            "normal",
        ),
        (
            "sota_mvp_normal",
            {"postprocess": {"extended_search": "off"}},
            "normal",
        ),
        (
            "sota_mvp_conditional_harness",
            {},
            "normal",
        ),
    ]
    configs: list[EvalConfig] = []
    for config_id, overrides, mode in specs:
        merged = _eval_overrides(overrides)
        profile = get_retrieval_profile(base_profile, resolved, merged)
        payload = {
            "profile": base_profile,
            "overrides": merged,
            "mode": mode,
            "model_aliases": profile.model_aliases.model_dump(),
        }
        configs.append(
            EvalConfig(
                config_id=config_id,
                retrieval_profile=base_profile,
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
    settings: Settings | None = None,
    client: EvalApiClient | None = None,
    progress_callback: EvalRunProgressCallback | None = None,
) -> EvalRunManifest:
    resolved = settings or get_settings()
    run_id = f"{manifest.dataset_name}-{manifest.dataset_hash[:12]}"
    run_dir = ARTIFACT_ROOT / "runs" / manifest.dataset_name / run_id
    configs = eval_configs(resolved)
    api_client = client or HttpEvalApiClient(
        kiwix_public_base_url=resolved.kiwix_public_base_url,
        kiwix_internal_base_url=resolved.kiwix_internal_base_url,
    )
    summaries: list[ConfigSummary] = []
    started_at = utc_now_iso()
    started = time.perf_counter()
    status = _initial_status(
        manifest,
        run_id=run_id,
        run_dir=run_dir,
        configs=configs,
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
            status = _advance_status(
                status,
                started=started,
                current_task_id="",
                current_task_index=0,
                processed_task_runs=_processed_count(run_dir, configs),
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
            existing = _latest_results(results_path)
            for task_index, task in enumerate(tasks, start=1):
                if task.task_id in existing and existing[task.task_id].status in {"completed", "reused"}:
                    continue
                status = _advance_status(
                    status,
                    started=started,
                    current_task_id=task.task_id,
                    current_task_index=task_index,
                    processed_task_runs=_processed_count(run_dir, configs),
                )
                _write_status(status)
                _log_event(run_dir, "task_started", status=status, config=config, task=task)
                _emit(progress_callback, status, "task_started")
                result = await run_task(task, config, api=api, manifest=manifest, client=api_client, settings=resolved)
                append_jsonl(results_path, result)
                existing[task.task_id] = result
                status = _advance_status(
                    status,
                    started=started,
                    current_task_id=task.task_id,
                    current_task_index=task_index,
                    processed_task_runs=_processed_count(run_dir, configs),
                    last_latency_ms=int(result.latency_ms.get("total", 0)),
                )
                _write_status(status)
                _log_event(
                    run_dir,
                    "task_completed" if result.status == "completed" else "task_failed",
                    status=status,
                    config=config,
                    task=task,
                    errors=result.errors,
                )
                _emit(progress_callback, status, "task_completed" if result.status == "completed" else "task_failed")
            results = read_jsonl(results_path, EvalTaskResult)
            summary = summarize_config(config, tasks, results)
            summaries.append(summary)
            write_json(run_dir / "summaries" / f"{config.config_id}.json", summary.model_dump(mode="json"))
            status = _advance_status(
                status,
                started=started,
                current_task_id="",
                current_task_index=0,
                processed_task_runs=_processed_count(run_dir, configs),
            )
            _write_status(status)
            _log_event(run_dir, "config_completed", status=status, config=config)
            _emit(progress_callback, status, "config_completed")
        run_manifest = EvalRunManifest(
            run_id=run_id,
            suite=manifest.dataset_name,
            dataset_hash=manifest.dataset_hash,
            dataset_path=manifest.jsonl_path,
            created_at=utc_now_iso(),
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


async def run_task(
    task: EvalTask,
    config: EvalConfig,
    *,
    api: str,
    manifest: EvalDatasetManifest,
    client: EvalApiClient,
    settings: Settings,
) -> EvalTaskResult:
    started = time.perf_counter()
    try:
        payload = client.run_chat(
            task.question,
            api=api,
            retrieval_profile=config.retrieval_profile,
            retrieval_overrides=config.retrieval_overrides,
            mode=config.mode,
        )
        total_ms = int((time.perf_counter() - started) * 1000)
        if payload.get("failed"):
            return _failed_result(task, config, manifest, str(payload.get("error") or "chat failed"), total_ms)
        usage_event = dict(payload.get("usage") or {})
        usage_data = dict(usage_event.get("data") or {})
        retrieval = dict(usage_data.get("retrieval") or {})
        validation = dict(usage_data.get("citation_validation") or {})
        contract_ids = _contract_ids_from_payload(retrieval)
        answer = str(payload.get("answer") or "")
        prefusion, reranked = await _extract_candidates(retrieval, settings)
        evidence = list(retrieval.get("evidence") or [])
        cited_ids = [str(item) for item in validation.get("citations", [])]
        cited_chunk_ids = _cited_chunk_ids(cited_ids, evidence)
        cited_urls = _cited_urls(cited_ids, evidence)
        gold_urls = [item.source_url for item in task.gold_evidence if item.source_url]
        kiwix_ok = all(client.url_ok(url) for url in [*gold_urls, *cited_urls] if url)
        scores = score_task(
            task,
            answer=answer,
            reranked=reranked,
            prefusion=prefusion,
            cited_chunk_ids=cited_chunk_ids,
            kiwix_url_ok=kiwix_ok,
        )
        retrieval_latency = _retrieval_latency_ms(retrieval)
        timings_ms = _combined_timings_ms(usage_data, retrieval, validation)
        model_calls = _estimate_model_calls(config, retrieval)
        usage = {
            "input_tokens": _usage_int(validation.get("usage"), "prompt_tokens"),
            "output_tokens": _usage_int(validation.get("usage"), "completion_tokens"),
            "total_tokens": _usage_int(validation.get("usage"), "total_tokens"),
            "estimated_cost_usd": _cost(validation),
            "model_calls": model_calls,
            "raw_generation_usage": validation.get("usage", {}),
        }
        return EvalTaskResult(
            task_id=task.task_id,
            config_id=config.config_id,
            config_hash=config.config_hash,
            status="completed",
            question=task.question,
            answer=answer,
            citations=cited_ids,
            cited_chunk_ids=cited_chunk_ids,
            retrieved_candidates=prefusion,
            reranked_candidates=reranked,
            mode_selected="harness" if _used_harness(retrieval) else "normal",
            latency_ms={"total": total_ms, "retrieval": retrieval_latency, **timings_ms},
            usage=usage,
            scores=scores,
            errors=[],
            query_run_id=str(payload.get("query_run_id")) if payload.get("query_run_id") else None,
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
        return _failed_result(task, config, manifest, type(exc).__name__ + ": " + str(exc), total_ms)


def summarize_config(config: EvalConfig, tasks: list[EvalTask], results: list[EvalTaskResult]) -> ConfigSummary:
    task_by_id = {task.task_id: task for task in tasks}
    completed = [result for result in results if result.scores is not None]
    answerable = [result for result in completed if not task_by_id[result.task_id].unanswerable]
    metrics = _aggregate_results(completed, answerable)
    by_family: dict[str, dict[str, float]] = {}
    for family in sorted({task.task_family for task in tasks}):
        family_results = [result for result in completed if task_by_id[result.task_id].task_family == family]
        family_answerable = [result for result in family_results if not task_by_id[result.task_id].unanswerable]
        by_family[str(family)] = _aggregate_results(family_results, family_answerable)
    failed = [
        result.task_id
        for result in results
        if result.status == "failed"
        or result.scores is None
        or _task_failed_threshold(task_by_id.get(result.task_id), result.scores)
    ]
    contract_ids = _summary_contract_ids(results)
    return ConfigSummary(
        config_id=config.config_id,
        config_hash=config.config_hash,
        task_count=len(results),
        metrics=metrics,
        by_family=by_family,
        failed_task_ids=sorted(set(failed)),
        errors=[error for result in results for error in result.errors] + _contract_mix_errors(contract_ids),
        contract_ids=contract_ids,
    )


async def _extract_candidates(
    retrieval: dict[str, Any],
    settings: Settings,
) -> tuple[list[CandidateRef], list[CandidateRef]]:
    events = [dict(item) for item in retrieval.get("events", []) if isinstance(item, dict)]
    prefusion_raw = (
        _stage_candidates(events, "rrf") or _stage_candidates(events, "bm25") or _stage_candidates(events, "dense")
    )
    reranked_raw = _stage_candidates(events, "rerank") or prefusion_raw
    ids = [str(item.get("chunk_id")) for item in [*prefusion_raw, *reranked_raw] if item.get("chunk_id")]
    refs = await load_chunk_refs(ids, settings=settings)
    return _candidate_refs(prefusion_raw, refs, "prefusion"), _candidate_refs(reranked_raw, refs, "rerank")


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


def _stage_candidates(events: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    for event in events:
        if event.get("stage") == stage and isinstance(event.get("candidates"), list):
            return [dict(item) for item in event["candidates"] if isinstance(item, dict)]
    return []


def _cited_chunk_ids(cited_ids: list[str], evidence: list[Any]) -> list[str]:
    by_id = {str(item.get("evidence_id")): str(item.get("chunk_id")) for item in evidence if isinstance(item, dict)}
    return [by_id[item] for item in cited_ids if item in by_id]


def _cited_urls(cited_ids: list[str], evidence: list[Any]) -> list[str]:
    by_id = {str(item.get("evidence_id")): str(item.get("source_url")) for item in evidence if isinstance(item, dict)}
    return [by_id[item] for item in cited_ids if item in by_id and by_id[item]]


def _eval_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "retrieval": {
            "top_k": 20,
            "bm25_top_k": 100,
            "dense_top_k": 100,
            "fusion_top_k": 60,
            "rerank_top_k": 50,
        },
        "postprocess": {"final_evidence_max": 20},
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


def _aggregate_results(results: list[EvalTaskResult], answerable: list[EvalTaskResult]) -> dict[str, float]:
    retrieval_base = answerable or results
    scores = [result.scores for result in results if result.scores is not None]
    retrieval_scores = [result.scores for result in retrieval_base if result.scores is not None]
    latencies = [float(result.latency_ms.get("total", 0)) for result in results]
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
        "exact_match": aggregate(score.exact_match for score in scores),
        "token_f1": aggregate(score.token_f1 for score in scores),
        "unanswerable_accuracy": aggregate(score.unanswerable_accuracy for score in scores),
        "citation_precision": aggregate(score.citation_precision for score in scores),
        "citation_recall": aggregate(score.citation_recall for score in scores),
        "unsupported_claim_rate": aggregate(score.unsupported_claim_rate for score in scores),
        "kiwix_url_ok": aggregate(score.kiwix_url_ok for score in scores),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "model_calls": aggregate(float(result.usage.get("model_calls", 0)) for result in results),
        "tokens": aggregate(float(result.usage.get("total_tokens", 0)) for result in results),
        "openrouter_cost_usd": aggregate(float(result.usage.get("estimated_cost_usd", 0.0)) for result in results),
    }
    metrics.update(_stage_latency_metrics(results))
    return metrics


def _task_failed_threshold(task: EvalTask | None, scores: TaskScores | None) -> bool:
    if task is None or scores is None:
        return True
    if task.unanswerable:
        return scores.unanswerable_accuracy < 1.0
    return scores.page_recall["10"] < 1.0 or scores.chunk_recall["20"] < 1.0 or scores.citation_precision < 1.0


def _failed_result(
    task: EvalTask,
    config: EvalConfig,
    manifest: EvalDatasetManifest,
    error: str,
    latency_ms: int,
) -> EvalTaskResult:
    return EvalTaskResult(
        task_id=task.task_id,
        config_id=config.config_id,
        config_hash=config.config_hash,
        status="failed",
        question=task.question,
        latency_ms={"total": latency_ms},
        errors=[error],
        corpus={
            "snapshot_id": manifest.snapshot_id,
            "index_version": manifest.index_version,
            "zim_checksum": manifest.zim_checksum,
        },
        model_aliases=config.model_aliases,
        contract_ids={},
    )


def _retrieval_latency_ms(retrieval: dict[str, Any]) -> int:
    for event in reversed(list(retrieval.get("events", []))):
        if isinstance(event, dict) and event.get("stage") == "context" and event.get("latency_ms") is not None:
            return int(event["latency_ms"])
    return 0


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


def _contract_ids_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(payload[key])
        for key in ("index_contract_id", "run_contract_id")
        if isinstance(payload.get(key), str) and payload.get(key)
    }


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
    run_dir: Path,
    configs: list[EvalConfig],
    started_at: str,
    processed_task_runs: int = 0,
    completed_task_runs: int = 0,
    failed_task_runs: int = 0,
) -> EvalRunStatus:
    return EvalRunStatus(
        run_id=run_id,
        state="running",
        phase="preparing",
        suite=manifest.dataset_name,
        dataset_hash=manifest.dataset_hash,
        dataset_path=manifest.jsonl_path,
        run_dir=str(run_dir),
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
    last_latency_ms: int | None = None,
) -> EvalRunStatus:
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


def _processed_count(run_dir: Path, configs: list[EvalConfig]) -> int:
    return sum(len(_latest_results(_results_path(run_dir, config))) for config in configs)


def _completed_count(run_dir: Path) -> int:
    return _status_count(run_dir, {"completed", "reused"})


def _failed_count(run_dir: Path) -> int:
    return _status_count(run_dir, {"failed"})


def _status_count(run_dir: Path, statuses: set[str]) -> int:
    return sum(
        1
        for path in (run_dir / "results").glob("*.jsonl")
        for result in _latest_results(path).values()
        if result.status in statuses
    )


def _latest_results(path: Path) -> dict[str, EvalTaskResult]:
    rows = read_jsonl(path, EvalTaskResult)
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
    return 1 + retrieval_calls


def _usage_int(usage: Any, key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key)
    return int(value) if isinstance(value, int | float | str) and str(value).replace(".", "", 1).isdigit() else 0


def _cost(validation: dict[str, Any]) -> float:
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
    return 0.0
