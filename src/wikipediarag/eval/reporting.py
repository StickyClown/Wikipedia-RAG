from __future__ import annotations

from pathlib import Path

from wikipediarag.eval.artifacts import ARTIFACT_ROOT, read_json, write_json
from wikipediarag.eval.schemas import EvalRunManifest


def write_report(run_manifest: EvalRunManifest) -> tuple[Path, Path]:
    report_dir = ARTIFACT_ROOT / "reports"
    json_path = report_dir / f"{run_manifest.run_id}.json"
    md_path = report_dir / f"{run_manifest.run_id}.md"
    write_json(json_path, run_manifest.model_dump(mode="json"))
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_markdown(run_manifest), encoding="utf-8")
    write_json(
        report_dir / "latest.json",
        {"json": str(json_path), "markdown": str(md_path), "run_id": run_manifest.run_id},
    )
    return md_path, json_path


def load_latest_run() -> EvalRunManifest:
    latest = ARTIFACT_ROOT / "runs" / "latest.json"
    if not latest.exists():
        raise FileNotFoundError("no eval run manifest found")
    return EvalRunManifest.model_validate(read_json(latest))


def _markdown(run: EvalRunManifest) -> str:
    lines = [
        f"# Evaluation report: {run.suite}",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Dataset hash: `{run.dataset_hash}`",
        f"- Dataset path: `{run.dataset_path}`",
        f"- Created: `{run.created_at}`",
        "",
        "## Overall",
        "",
        (
            "| Config | Page R@10 | Chunk R@20 | MRR@10 | nDCG@10 | Citation P | "
            "Unanswerable Acc | Latency p95 ms | Failed |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in run.config_summaries:
        m = summary.metrics
        lines.append(
            (
                "| {config} | {page:.3f} | {chunk:.3f} | {mrr:.3f} | {ndcg:.3f} | "
                "{cp:.3f} | {ua:.3f} | {lat:.0f} | {failed} |"
            ).format(
                config=summary.config_id,
                page=m.get("page_recall_at_10", 0.0),
                chunk=m.get("chunk_recall_at_20", 0.0),
                mrr=m.get("mrr_at_10", 0.0),
                ndcg=m.get("ndcg_at_10", 0.0),
                cp=m.get("citation_precision", 0.0),
                ua=m.get("unanswerable_accuracy", 0.0),
                lat=m.get("latency_p95_ms", 0.0),
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
                "| Family | Page R@10 | Chunk R@20 | Citation P | Failed |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for family, metrics in sorted(summary.by_family.items()):
            lines.append(
                "| {family} | {page:.3f} | {chunk:.3f} | {cp:.3f} | {failed:.0f} |".format(
                    family=family,
                    page=metrics.get("page_recall_at_10", 0.0),
                    chunk=metrics.get("chunk_recall_at_20", 0.0),
                    cp=metrics.get("citation_precision", 0.0),
                    failed=0,
                )
            )
        lines.extend(["", "Failed task IDs:", ""])
        if summary.failed_task_ids:
            lines.append(", ".join(f"`{item}`" for item in summary.failed_task_ids))
        else:
            lines.append("None")
        lines.append("")
    return "\n".join(lines)


def _stage_timing_rows(run: EvalRunManifest) -> list[tuple[str, str, float, float]]:
    rows: list[tuple[str, str, float, float]] = []
    for summary in run.config_summaries:
        metrics = summary.metrics
        p95_keys = sorted(key for key in metrics if key.startswith("stage_latency_") and key.endswith("_p95_ms"))
        for p95_key in p95_keys:
            stage = p95_key.removeprefix("stage_latency_").removesuffix("_p95_ms")
            p50_key = f"stage_latency_{stage}_p50_ms"
            rows.append((summary.config_id, stage, metrics.get(p50_key, 0.0), metrics.get(p95_key, 0.0)))
    return rows


def _contract_rows(run: EvalRunManifest) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for summary in run.config_summaries:
        for name, ids in sorted(summary.contract_ids.items()):
            rows.append((summary.config_id, name, ", ".join(f"`{item}`" for item in ids) if ids else "-"))
    return rows
