from __future__ import annotations

from pathlib import Path

from wikipediarag.eval.artifacts import ARTIFACT_ROOT, read_json, read_jsonl, write_json
from wikipediarag.eval.retrieval_runner import load_latest_retrieval_run
from wikipediarag.eval.schemas import RetrievalRunManifest, RetrievalTaskResult


def write_retrieval_report(run_manifest: RetrievalRunManifest | None = None) -> tuple[Path, Path]:
    run = run_manifest or load_latest_retrieval_run()
    report_dir = ARTIFACT_ROOT / "retrieval-reports"
    json_path = report_dir / f"{run.run_id}.json"
    md_path = report_dir / f"{run.run_id}.md"
    worst_misses = _worst_misses(run)
    payload = {
        "run": run.model_dump(mode="json"),
        "worst_misses": worst_misses,
    }
    write_json(json_path, payload)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_markdown(run, worst_misses), encoding="utf-8")
    write_json(
        report_dir / "latest.json",
        {"json": str(json_path), "markdown": str(md_path), "run_id": run.run_id},
    )
    return md_path, json_path


def load_latest_retrieval_report() -> dict[str, str]:
    latest = ARTIFACT_ROOT / "retrieval-reports" / "latest.json"
    if not latest.exists():
        raise FileNotFoundError("no retrieval eval report found")
    return {key: str(value) for key, value in read_json(latest).items()}


def _markdown(run: RetrievalRunManifest, worst_misses: list[dict[str, str]]) -> str:
    lines = [
        f"# Retrieval evaluation report: {run.suite}",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Dataset hash: `{run.dataset_hash}`",
        f"- Dataset path: `{run.dataset_path}`",
        f"- Created: `{run.created_at}`",
        f"- Batch size: `{run.batch_size}`",
        "",
        "## Overall",
        "",
        (
            "| Config | Status | Page R@10 | Chunk R@20 | MRR@10 | nDCG@10 | "
            "HardNeg@10 | Latency p95 ms | Errors | Failed |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in run.config_summaries:
        m = summary.metrics
        lines.append(
            (
                "| {config} | {status} | {page:.3f} | {chunk:.3f} | {mrr:.3f} | {ndcg:.3f} | "
                "{hard:.3f} | {lat:.0f} | {errors} | {failed} |"
            ).format(
                config=summary.config_id,
                status=summary.status,
                page=m.get("page_recall_at_10", 0.0),
                chunk=m.get("chunk_recall_at_20", 0.0),
                mrr=m.get("mrr_at_10", 0.0),
                ndcg=m.get("ndcg_at_10", 0.0),
                hard=m.get("hard_negative_page_hit_at_10", 0.0),
                lat=m.get("latency_p95_ms", 0.0),
                errors=len(summary.errors),
                failed=len(summary.failed_task_ids),
            )
        )
    lines.extend(["", "## Contracts", ""])
    contract_rows = _contract_rows(run)
    if not contract_rows:
        lines.append("No contract IDs were recorded.")
    else:
        lines.append("| Config | Contract | IDs |")
        lines.append("|---|---|---|")
        for config_id, name, ids in contract_rows:
            lines.append(f"| {config_id} | {name} | {ids} |")
    lines.extend(["", "## Stage Timings", ""])
    stage_rows = _stage_timing_rows(run)
    if not stage_rows:
        lines.append("No stage timing metrics were recorded.")
    else:
        lines.append("| Config | Stage | p50 ms | p95 ms |")
        lines.append("|---|---|---:|---:|")
        for config_id, stage, p50, p95 in stage_rows:
            lines.append(f"| {config_id} | {stage} | {p50:.0f} | {p95:.0f} |")
    lines.extend(["", "## By Task Family", ""])
    for summary in run.config_summaries:
        lines.extend(
            [
                f"### {summary.config_id}",
                "",
                "| Family | Page R@10 | Chunk R@20 | MRR@10 | HardNeg@10 | Error rate |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for family, metrics in sorted(summary.by_family.items()):
            lines.append(
                "| {family} | {page:.3f} | {chunk:.3f} | {mrr:.3f} | {hard:.3f} | {error:.3f} |".format(
                    family=family,
                    page=metrics.get("page_recall_at_10", 0.0),
                    chunk=metrics.get("chunk_recall_at_20", 0.0),
                    mrr=metrics.get("mrr_at_10", 0.0),
                    hard=metrics.get("hard_negative_page_hit_at_10", 0.0),
                    error=metrics.get("error_rate", 0.0),
                )
            )
        lines.extend(["", "Failed task IDs:", ""])
        lines.append(", ".join(f"`{item}`" for item in summary.failed_task_ids) if summary.failed_task_ids else "None")
        lines.append("")
    lines.extend(["## Worst Misses", ""])
    if not worst_misses:
        lines.append("None")
    else:
        lines.append("| Config | Task | Family | Reason |")
        lines.append("|---|---|---|---|")
        for miss in worst_misses:
            lines.append(
                "| {config} | `{task}` | {family} | {reason} |".format(
                    config=miss["config_id"],
                    task=miss["task_id"],
                    family=miss["task_family"],
                    reason=miss["reason"],
                )
            )
    lines.append("")
    return "\n".join(lines)


def _worst_misses(run: RetrievalRunManifest) -> list[dict[str, str]]:
    misses: list[dict[str, str]] = []
    for path in sorted((Path(run.run_dir) / "results").glob("*.jsonl")):
        for result in read_jsonl(path, RetrievalTaskResult):
            reason = _miss_reason(result)
            if reason:
                misses.append(
                    {
                        "config_id": result.config_id,
                        "task_id": result.task_id,
                        "task_family": result.task_family,
                        "reason": reason,
                    }
                )
            if len(misses) >= 50:
                return misses
    return misses


def _stage_timing_rows(run: RetrievalRunManifest) -> list[tuple[str, str, float, float]]:
    rows: list[tuple[str, str, float, float]] = []
    for summary in run.config_summaries:
        metrics = summary.metrics
        p95_keys = sorted(key for key in metrics if key.startswith("stage_latency_") and key.endswith("_p95_ms"))
        for p95_key in p95_keys:
            stage = p95_key.removeprefix("stage_latency_").removesuffix("_p95_ms")
            p50_key = f"stage_latency_{stage}_p50_ms"
            rows.append((summary.config_id, stage, metrics.get(p50_key, 0.0), metrics.get(p95_key, 0.0)))
    return rows


def _contract_rows(run: RetrievalRunManifest) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for summary in run.config_summaries:
        for name, ids in sorted(summary.contract_ids.items()):
            rows.append((summary.config_id, name, ", ".join(f"`{item}`" for item in ids) if ids else "-"))
    return rows


def _miss_reason(result: RetrievalTaskResult) -> str:
    if result.status == "failed":
        return "; ".join(result.errors) or "API error"
    if result.scores is None:
        return "missing scores"
    if not result.unanswerable and result.scores.chunk_recall["20"] < 1.0:
        return "gold absent from top-20"
    margin = result.scores.gold_vs_hard_negative_rank_margin
    if margin is not None and margin < 0:
        return "hard-negative outranks gold"
    return ""
