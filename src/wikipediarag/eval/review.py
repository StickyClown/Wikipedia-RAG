from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from pydantic import BaseModel, Field, ValidationError

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.artifacts import ARTIFACT_ROOT, read_json, utc_now_iso, write_json, write_jsonl
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.retrieval_runner import run_retrieval_suite
from wikipediarag.eval.runner import run_suite
from wikipediarag.eval.schemas import (
    ConfigSummary,
    EvalDatasetManifest,
    EvalRunManifest,
    EvalRunStatus,
    EvalTask,
    GoldEvidence,
    ReleaseGateStatus,
    RetrievalConfigSummary,
    RetrievalEvalStatus,
    RetrievalRunManifest,
)
from wikipediarag.eval.settings import adapt_eval_settings

ReviewDecision = Literal["AUTO_ACCEPT", "REVIEW", "REJECT"]
ReviewStatus = Literal["unreviewed", "reviewed", "rejected"]
ReviewedSplit = Literal["train", "dev", "test"]

REVIEW_SCHEMA_VERSION = "reviewed_eval_v1"
RELEASE_GATE_CONFIG_ID = "sota_mvp_normal"
QUALITY_REGRESSION_BUDGET = 0.03
LATENCY_REGRESSION_MULTIPLIER = 1.25
RELEASE_GATE_STAGES: tuple[
    Literal["dev_answer", "dev_retrieval", "test_answer", "test_retrieval", "gate_evaluation"],
    ...,
] = ("dev_answer", "dev_retrieval", "test_answer", "test_retrieval", "gate_evaluation")

type ReleaseGateProgressCallback = Callable[[ReleaseGateStatus, str], None]


class ReleaseGateCliReporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def __call__(self, status: ReleaseGateStatus, event: str) -> None:
        print(format_release_gate_progress(status, event), file=self._stream, flush=True)


def format_release_gate_progress(status: ReleaseGateStatus, event: str) -> str:
    child = ""
    if status.child_total_task_runs:
        child = (
            f" child={status.child_run_id}"
            f" processed={status.child_processed_task_runs}/{status.child_total_task_runs}"
            f" failed={status.child_failed_task_runs}"
        )
    timing = _format_stage_timings(status.timings_ms)
    return (
        f"[{_format_elapsed(status.elapsed_seconds)}] release_gate={event}"
        f" suite={status.suite}"
        f" stage={status.current_stage or status.phase}"
        f" stage_index={status.stage_index}/{status.total_stages}"
        f"{child}"
        f" timings_ms={timing}"
    )


def format_release_gate_status(status: ReleaseGateStatus) -> str:
    lines = [
        f"run_id={status.run_id} state={status.state} phase={status.phase}",
        f"updated_at={status.updated_at}",
        f"suite={status.suite} api={status.api}",
        (
            f"stage={status.current_stage or '-'}"
            f" stage_index={status.stage_index}/{status.total_stages}"
            f" elapsed={_format_elapsed(status.elapsed_seconds)}"
        ),
        (
            f"child run_id={status.child_run_id or '-'}"
            f" processed={status.child_processed_task_runs}/{status.child_total_task_runs}"
            f" failed={status.child_failed_task_runs}"
        ),
        f"timings_ms={_format_stage_timings(status.timings_ms)}",
        f"run_dir={status.run_dir}",
    ]
    if status.passed is not None:
        lines.append(f"passed={status.passed} blocking_failures={status.blocking_failures}")
    if status.error_message:
        lines.append(f"error={status.error_message}")
    return "\n".join(lines)


class ReviewedEvalTask(EvalTask):
    decision_status: ReviewDecision = "AUTO_ACCEPT"
    review_status: ReviewStatus = "reviewed"
    split: ReviewedSplit = "train"
    provenance: dict[str, str] = Field(default_factory=dict)
    source_candidate_id: str = ""
    review_notes: list[str] = Field(default_factory=list)


class ReviewPoolManifest(BaseModel):
    suite: str
    schema_version: str = REVIEW_SCHEMA_VERSION
    created_at: str
    dataset_hash: str
    task_count: int
    reviewed_count: int
    unreviewed_count: int
    rejected_count: int
    jsonl_path: str
    manifest_path: str
    provenance: dict[str, list[str]]
    by_decision_status: dict[str, int]
    by_review_status: dict[str, int]


class FreezeReviewedResult(BaseModel):
    suite: str
    dev_manifest: str
    test_manifest: str
    dev_count: int
    test_count: int
    dev_dataset_hash: str
    test_dataset_hash: str
    reused_locked: list[str] = Field(default_factory=list)


class GateFinding(BaseModel):
    split: ReviewedSplit
    source: Literal["answer", "retrieval"]
    config_id: str
    metric: str
    message: str
    blocking: bool


def review_status_for_decision(
    decision: ReviewDecision,
    *,
    current_status: str | None = None,
) -> ReviewStatus:
    if decision == "REJECT":
        return "rejected"
    if current_status == "reviewed":
        return "reviewed"
    if current_status == "rejected":
        return "rejected"
    if decision == "AUTO_ACCEPT":
        return "reviewed"
    return "unreviewed"


def write_review_pool(*, input_path: Path, output_suite: str) -> ReviewPoolManifest:
    candidates = _load_candidate_payloads(input_path)
    tasks = [_reviewed_task_from_candidate(row, source_dataset_hash=stable_json_hash(candidates)) for row in candidates]
    digest = reviewed_dataset_hash(tasks)
    output_dir = ARTIFACT_ROOT / "datasets" / output_suite
    jsonl_path = output_dir / f"{output_suite}-reviewed-pool-{digest[:12]}.jsonl"
    manifest_path = jsonl_path.with_suffix(".manifest.json")
    review_counts = Counter(task.review_status for task in tasks)
    decision_counts = Counter(task.decision_status for task in tasks)
    manifest = ReviewPoolManifest(
        suite=output_suite,
        created_at=utc_now_iso(),
        dataset_hash=digest,
        task_count=len(tasks),
        reviewed_count=review_counts.get("reviewed", 0),
        unreviewed_count=review_counts.get("unreviewed", 0),
        rejected_count=review_counts.get("rejected", 0),
        jsonl_path=str(jsonl_path),
        manifest_path=str(manifest_path),
        provenance=_provenance_values(tasks),
        by_decision_status=dict(sorted(decision_counts.items())),
        by_review_status=dict(sorted(review_counts.items())),
    )
    write_jsonl(jsonl_path, tasks)
    write_json(manifest_path, manifest.model_dump(mode="json"))
    write_json(output_dir / "reviewed-pool.latest.json", manifest.model_dump(mode="json"))
    return manifest


def freeze_reviewed_suite(*, suite: str, dev_count: int, test_count: int) -> FreezeReviewedResult:
    if dev_count < 0 or test_count < 0:
        raise ValueError("dev-count and test-count must be >= 0")
    if dev_count == 0 and test_count == 0:
        raise ValueError("at least one frozen split must be requested")
    pool_manifest = load_review_pool_manifest(suite)
    tasks = _read_reviewed_tasks(Path(pool_manifest.jsonl_path))
    eligible = [task for task in tasks if task.review_status == "reviewed"]
    required = dev_count + test_count
    if len(eligible) < required:
        raise ValueError(f"not enough reviewed rows for freeze: reviewed={len(eligible)} required={required}")
    ordered = sorted(
        eligible,
        key=lambda task: stable_json_hash([pool_manifest.dataset_hash, task.task_id, task.question]),
    )
    dev_tasks = [_with_split(task, "dev") for task in ordered[:dev_count]]
    test_tasks = [_with_split(task, "test") for task in ordered[dev_count:required]]
    reused: list[str] = []
    dev_manifest_path, dev_hash, dev_reused = _write_locked_split(suite, "dev", dev_tasks, pool_manifest)
    test_manifest_path, test_hash, test_reused = _write_locked_split(suite, "test", test_tasks, pool_manifest)
    if dev_reused:
        reused.append("dev")
    if test_reused:
        reused.append("test")
    return FreezeReviewedResult(
        suite=suite,
        dev_manifest=str(dev_manifest_path),
        test_manifest=str(test_manifest_path),
        dev_count=dev_count,
        test_count=test_count,
        dev_dataset_hash=dev_hash,
        test_dataset_hash=test_hash,
        reused_locked=reused,
    )


async def eval_release_gate(
    *,
    suite: str,
    api: str,
    settings: Settings | None = None,
    progress_callback: ReleaseGateProgressCallback | None = None,
) -> dict[str, Any]:
    resolved = adapt_eval_settings(settings or get_settings())
    dev_manifest, dev_payload = load_locked_split_manifest(suite, "dev")
    test_manifest, test_payload = load_locked_split_manifest(suite, "test")
    baseline = dict(dev_payload.get("baseline") or dev_payload.get("release_gate_baseline") or {})
    dev_tasks = load_locked_split_tasks(dev_manifest, "dev")
    test_tasks = load_locked_split_tasks(test_manifest, "test")
    run_id = f"{suite}-release-gate"
    run_dir = _release_gate_run_dir(suite, run_id)
    started_at = utc_now_iso()
    started = time.perf_counter()
    status = ReleaseGateStatus(
        run_id=run_id,
        state="running",
        phase="preparing",
        suite=suite,
        api=api,
        run_dir=str(run_dir),
        total_stages=len(RELEASE_GATE_STAGES),
        started_at=started_at,
        updated_at=started_at,
    )
    _write_release_gate_status(status)
    _log_release_gate_event(run_dir, "run_started", status=status)
    _emit_release_gate(progress_callback, status, "run_started")
    timings: dict[str, int] = {}
    try:
        status, dev_answer = await _timed_stage(
            status,
            "dev_answer",
            started=started,
            timings=timings,
            progress_callback=progress_callback,
            run_coro=lambda callback: run_suite(
                dev_manifest,
                dev_tasks,
                api=api,
                settings=resolved,
                progress_callback=callback,
            ),
        )
        status, dev_retrieval = await _timed_stage(
            status,
            "dev_retrieval",
            started=started,
            timings=timings,
            progress_callback=progress_callback,
            run_coro=lambda callback: run_retrieval_suite(
                dev_manifest,
                dev_tasks,
                api=api,
                batch_size=10,
                run_id=f"{suite}-dev-release-gate",
                settings=resolved,
                progress_callback=callback,
            ),
        )
        status, test_answer = await _timed_stage(
            status,
            "test_answer",
            started=started,
            timings=timings,
            progress_callback=progress_callback,
            run_coro=lambda callback: run_suite(
                test_manifest,
                test_tasks,
                api=api,
                settings=resolved,
                progress_callback=callback,
            ),
        )
        status, test_retrieval = await _timed_stage(
            status,
            "test_retrieval",
            started=started,
            timings=timings,
            progress_callback=progress_callback,
            run_coro=lambda callback: run_retrieval_suite(
                test_manifest,
                test_tasks,
                api=api,
                batch_size=10,
                run_id=f"{suite}-test-release-gate",
                settings=resolved,
                progress_callback=callback,
            ),
        )
        status = _start_release_gate_stage(status, "gate_evaluation", started=started, timings=timings)
        _write_release_gate_status(status)
        _log_release_gate_event(run_dir, "stage_started", status=status)
        _emit_release_gate(progress_callback, status, "stage_started")
        gate_started = time.perf_counter()
        report = evaluate_release_gate(
            suite=suite,
            baseline=baseline,
            dev_answer=dev_answer,
            dev_retrieval=dev_retrieval,
            test_answer=test_answer,
            test_retrieval=test_retrieval,
        )
        timings["gate_evaluation"] = int((time.perf_counter() - gate_started) * 1000)
        timings["total"] = int((time.perf_counter() - started) * 1000)
        report["timings_ms"] = dict(timings)
        report["release_gate_run"] = {
            "run_id": status.run_id,
            "run_dir": status.run_dir,
            "status_path": str(Path(status.run_dir) / "status.json"),
        }
        status = _advance_release_gate_status(status, started=started, timings=timings).model_copy(
            update={
                "state": "completed",
                "phase": "completed",
                "current_stage": "",
                "passed": bool(report.get("passed")),
                "blocking_failures": len(list(report.get("blocking_failures") or [])),
                "updated_at": utc_now_iso(),
            }
        )
        _write_release_gate_status(status)
        _log_release_gate_event(run_dir, "run_completed", status=status)
        _emit_release_gate(progress_callback, status, "run_completed")
        return report
    except Exception as exc:
        timings["total"] = int((time.perf_counter() - started) * 1000)
        status = _advance_release_gate_status(status, started=started, timings=timings).model_copy(
            update={
                "state": "failed",
                "phase": "failed",
                "updated_at": utc_now_iso(),
                "error_message": type(exc).__name__ + ": " + str(exc),
            }
        )
        _write_release_gate_status(status)
        _log_release_gate_event(run_dir, "run_failed", status=status, errors=[status.error_message])
        _emit_release_gate(progress_callback, status, "run_failed")
        raise


def evaluate_release_gate(
    *,
    suite: str,
    baseline: dict[str, Any],
    dev_answer: EvalRunManifest,
    dev_retrieval: RetrievalRunManifest,
    test_answer: EvalRunManifest,
    test_retrieval: RetrievalRunManifest,
) -> dict[str, Any]:
    findings: list[GateFinding] = []
    findings.extend(_gate_findings_for_split("dev", "answer", dev_answer.config_summaries, baseline))
    findings.extend(_gate_findings_for_split("dev", "retrieval", dev_retrieval.config_summaries, baseline))
    findings.extend(_gate_findings_for_split("test", "answer", test_answer.config_summaries, baseline))
    findings.extend(_gate_findings_for_split("test", "retrieval", test_retrieval.config_summaries, baseline))
    blocking = [finding for finding in findings if finding.blocking]
    return {
        "suite": suite,
        "passed": not blocking,
        "blocking_failures": [finding.model_dump(mode="json") for finding in blocking],
        "diagnostic_findings": [finding.model_dump(mode="json") for finding in findings if not finding.blocking],
        "runs": {
            "dev_answer": dev_answer.model_dump(mode="json"),
            "dev_retrieval": dev_retrieval.model_dump(mode="json"),
            "test_answer": test_answer.model_dump(mode="json"),
            "test_retrieval": test_retrieval.model_dump(mode="json"),
        },
    }


def load_review_pool_manifest(suite: str) -> ReviewPoolManifest:
    latest = ARTIFACT_ROOT / "datasets" / suite / "reviewed-pool.latest.json"
    if not latest.exists():
        raise FileNotFoundError(f"no reviewed pool found for suite {suite}")
    return ReviewPoolManifest.model_validate(read_json(latest))


def load_release_gate_status(suite: str) -> ReleaseGateStatus:
    latest = _release_gate_suite_dir(suite) / "latest-status.json"
    if not latest.exists():
        raise FileNotFoundError(f"no release gate status found for suite {suite}")
    return ReleaseGateStatus.model_validate(read_json(latest))


def load_locked_split_manifest(suite: str, split: Literal["dev", "test"]) -> tuple[EvalDatasetManifest, dict[str, Any]]:
    path = _locked_manifest_path(suite, split)
    if not path.exists():
        raise FileNotFoundError(f"no locked {split} manifest found for suite {suite}")
    payload = read_json(path)
    return EvalDatasetManifest.model_validate(payload), payload


def load_locked_split_tasks(manifest: EvalDatasetManifest, split: Literal["dev", "test"]) -> list[EvalTask]:
    payloads = _load_candidate_payloads(Path(manifest.jsonl_path))
    invalid = [
        str(row.get("task_id") or row.get("source_candidate_id") or "<unknown>")
        for row in payloads
        if row.get("split") != split or row.get("review_status") != "reviewed"
    ]
    if invalid:
        raise ValueError(f"locked {split} manifest contains non-reviewed or wrong-split rows: {invalid[:10]}")
    return [EvalTask.model_validate(row) for row in payloads]


def reviewed_dataset_hash(tasks: list[ReviewedEvalTask]) -> str:
    return stable_json_hash([task.model_dump(mode="json") for task in tasks])


def _load_candidate_payloads(input_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in input_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = dict(json.loads(line))
        rows.append(payload)
    return rows


def _reviewed_task_from_candidate(row: dict[str, Any], *, source_dataset_hash: str) -> ReviewedEvalTask:
    decision = _decision(row)
    status = review_status_for_decision(decision, current_status=_raw_review_status(row))
    source = _task_payload(row)
    task = _eval_task_from_payload(source, row)
    provenance = _review_provenance(row, source, task, source_dataset_hash)
    return ReviewedEvalTask(
        **task.model_dump(mode="json"),
        decision_status=decision,
        review_status=status,
        split=_split(row, source),
        provenance=provenance,
        source_candidate_id=str(row.get("candidate_id") or source.get("task_id") or task.task_id),
        review_notes=(
            [str(item) for item in row.get("notes", []) if str(item)] if isinstance(row.get("notes"), list) else []
        ),
    )


def _eval_task_from_payload(source: dict[str, Any], row: dict[str, Any]) -> EvalTask:
    try:
        return EvalTask.model_validate(source)
    except ValidationError:
        source = dict(source)
    provenance = dict(source.get("provenance") or row.get("provenance") or {})
    gold_page_ids = [str(item) for item in source.get("gold_page_ids", [])]
    gold_chunk_ids = [str(item) for item in source.get("gold_chunk_ids", [])]
    titles = [str(item) for item in source.get("gold_titles", [])]
    return EvalTask(
        task_id=str(source.get("task_id") or row.get("candidate_id") or "reviewed-candidate"),
        question=str(source.get("question") or row.get("question") or ""),
        task_family="single_hop_factual",
        reference_answer=str(source.get("reference_answer") or ""),
        accepted_answers=[str(item) for item in source.get("accepted_answers", [])],
        unanswerable=bool(source.get("unanswerable", False)),
        expected_mode="normal_sufficient",
        gold_page_ids=gold_page_ids,
        gold_section_ids=[str(item) for item in source.get("gold_section_ids", [])],
        gold_chunk_ids=gold_chunk_ids,
        gold_evidence=[
            GoldEvidence(
                evidence_id=f"gold-{index}",
                document_id=gold_page_ids[min(index - 1, len(gold_page_ids) - 1)] if gold_page_ids else "",
                section_id="",
                chunk_id=chunk_id,
                quote="",
                title=titles[min(index - 1, len(titles) - 1)] if titles else "",
            )
            for index, chunk_id in enumerate(gold_chunk_ids, start=1)
        ],
        reasoning_path=titles,
        generator_alias=str(source.get("generator_alias") or ""),
        verifier_alias=str(source.get("verifier_alias") or ""),
        zim_checksum=str(provenance.get("zim_checksum") or ""),
        snapshot_id=str(provenance.get("snapshot_id") or ""),
        index_version=str(provenance.get("index_version") or ""),
        retrieval_profile_hash=str(provenance.get("retrieval_profile_hash") or ""),
    )


def _task_payload(row: dict[str, Any]) -> dict[str, Any]:
    trusted_task = row.get("trusted_task")
    if isinstance(trusted_task, dict):
        return dict(trusted_task)
    return row


def _decision(row: dict[str, Any]) -> ReviewDecision:
    raw = str(row.get("decision_status") or row.get("decision") or "AUTO_ACCEPT")
    if raw not in {"AUTO_ACCEPT", "REVIEW", "REJECT"}:
        raise ValueError(f"invalid review decision: {raw}")
    return cast(ReviewDecision, raw)


def _raw_review_status(row: dict[str, Any]) -> str | None:
    value = row.get("review_status")
    return str(value) if value is not None else None


def _split(row: dict[str, Any], source: dict[str, Any]) -> ReviewedSplit:
    raw = str(row.get("split") or source.get("split") or "train")
    if raw not in {"train", "dev", "test"}:
        raise ValueError(f"invalid reviewed split: {raw}")
    return cast(ReviewedSplit, raw)


def _review_provenance(
    row: dict[str, Any],
    source: dict[str, Any],
    task: EvalTask,
    source_dataset_hash: str,
) -> dict[str, str]:
    provenance = {
        key: str(value)
        for payload in (row.get("provenance"), source.get("provenance"))
        if isinstance(payload, dict)
        for key, value in payload.items()
        if value is not None
    }
    for key, value in {
        "snapshot_id": task.snapshot_id,
        "index_version": task.index_version,
        "zim_checksum": task.zim_checksum,
        "retrieval_profile_hash": task.retrieval_profile_hash,
        "dataset_hash": str(row.get("dataset_hash") or source_dataset_hash),
        "index_contract_id": str(row.get("index_contract_id") or provenance.get("index_contract_id") or ""),
        "run_contract_id": str(row.get("run_contract_id") or provenance.get("run_contract_id") or ""),
    }.items():
        if value:
            provenance[key] = value
    return provenance


def _read_reviewed_tasks(path: Path) -> list[ReviewedEvalTask]:
    return [ReviewedEvalTask.model_validate(row) for row in _load_candidate_payloads(path)]


def _with_split(task: ReviewedEvalTask, split: Literal["dev", "test"]) -> ReviewedEvalTask:
    return task.model_copy(update={"split": split})


def _write_locked_split(
    suite: str,
    split: Literal["dev", "test"],
    tasks: list[ReviewedEvalTask],
    pool_manifest: ReviewPoolManifest,
) -> tuple[Path, str, bool]:
    digest = reviewed_dataset_hash(tasks)
    jsonl_path = _locked_jsonl_path(suite, split)
    manifest_path = _locked_manifest_path(suite, split)
    if manifest_path.exists() or jsonl_path.exists():
        if not manifest_path.exists() or not jsonl_path.exists():
            raise FileExistsError(f"locked {split} artifact exists without its matching pair")
        existing = read_json(manifest_path)
        if (
            existing.get("suite") == suite
            and existing.get("split") == split
            and existing.get("dataset_hash") == digest
            and existing.get("snapshot_id") == _single_provenance_value(tasks, "snapshot_id")
        ):
            return manifest_path, digest, True
        raise FileExistsError(f"locked {split} manifest already exists with a different hash or snapshot")
    manifest = EvalDatasetManifest(
        dataset_name=f"{suite}-{split}",
        dataset_version=REVIEW_SCHEMA_VERSION,
        dataset_hash=digest,
        task_count=len(tasks),
        created_at=utc_now_iso(),
        snapshot_id=_single_provenance_value(tasks, "snapshot_id"),
        index_version=_single_provenance_value(tasks, "index_version"),
        zim_checksum=_single_provenance_value(tasks, "zim_checksum"),
        retrieval_profile_hash=_single_provenance_value(tasks, "retrieval_profile_hash"),
        generator_alias="reviewed_pool",
        verifier_alias="reviewed_pool",
        jsonl_path=str(jsonl_path),
    )
    payload = {
        **manifest.model_dump(mode="json"),
        "suite": suite,
        "split": split,
        "locked": True,
        "source_pool_hash": pool_manifest.dataset_hash,
        "source_pool_manifest": pool_manifest.manifest_path,
        "review_status": "reviewed",
        "schema_version": REVIEW_SCHEMA_VERSION,
        "provenance": _provenance_values(tasks),
        "baseline": {},
    }
    write_jsonl(jsonl_path, tasks)
    write_json(manifest_path, payload)
    return manifest_path, digest, False


def _provenance_values(tasks: list[ReviewedEvalTask]) -> dict[str, list[str]]:
    keys = sorted({key for task in tasks for key in task.provenance})
    return {key: sorted({task.provenance[key] for task in tasks if task.provenance.get(key)}) for key in keys}


def _single_provenance_value(tasks: list[ReviewedEvalTask], key: str) -> str:
    values = sorted({task.provenance.get(key, "") for task in tasks if task.provenance.get(key, "")})
    if len(values) > 1:
        raise ValueError(f"cannot freeze mixed provenance for {key}: {values}")
    return values[0] if values else ""


def _locked_jsonl_path(suite: str, split: Literal["dev", "test"]) -> Path:
    return ARTIFACT_ROOT / "datasets" / suite / "locked" / f"{split}.jsonl"


def _locked_manifest_path(suite: str, split: Literal["dev", "test"]) -> Path:
    return ARTIFACT_ROOT / "datasets" / suite / "locked" / f"{split}.manifest.json"


def _gate_findings_for_split(
    split: Literal["dev", "test"],
    source: Literal["answer", "retrieval"],
    summaries: list[ConfigSummary] | list[RetrievalConfigSummary],
    baseline: dict[str, Any],
) -> list[GateFinding]:
    summary = _summary_by_config(summaries, RELEASE_GATE_CONFIG_ID)
    if summary is None:
        return [_finding(split, source, RELEASE_GATE_CONFIG_ID, "config", "sota_mvp_normal summary is missing")]
    findings: list[GateFinding] = []
    contract_ids = dict(getattr(summary, "contract_ids", {}))
    for key, values in contract_ids.items():
        if len(values) > 1:
            findings.append(_finding(split, source, summary.config_id, key, f"mixed contract IDs for {key}: {values}"))
    for error in summary.errors:
        if error.startswith("mixed_contract_ids:"):
            findings.append(_finding(split, source, summary.config_id, "contract_ids", error))
    metrics = summary.metrics
    if source == "answer":
        if metrics.get("citation_precision", 1.0) < 1.0:
            findings.append(
                _finding(split, source, summary.config_id, "citation_precision", "citation_precision < 1.0")
            )
        if metrics.get("unsupported_claim_rate", 0.0) > 0.0:
            findings.append(
                _finding(split, source, summary.config_id, "unsupported_claim_rate", "unsupported_claim_rate > 0")
            )
        if metrics.get("unanswerable_accuracy", 1.0) < 1.0:
            findings.append(
                _finding(split, source, summary.config_id, "unanswerable_accuracy", "unanswerable_accuracy < 1.0")
            )
    if source == "retrieval" and metrics.get("false_positive_evidence_rate", 0.0) > 0.0:
        findings.append(
            _finding(split, source, summary.config_id, "false_positive_evidence_rate", "false positive evidence > 0")
        )
    findings.extend(_regression_findings(split, source, summary, baseline))
    return findings


def _summary_by_config(
    summaries: list[ConfigSummary] | list[RetrievalConfigSummary],
    config_id: str,
) -> ConfigSummary | RetrievalConfigSummary | None:
    for summary in summaries:
        if summary.config_id == config_id:
            return summary
    return None


def _regression_findings(
    split: Literal["dev", "test"],
    source: Literal["answer", "retrieval"],
    summary: ConfigSummary | RetrievalConfigSummary,
    baseline: dict[str, Any],
) -> list[GateFinding]:
    baseline_summary = _baseline_summary(baseline, source, summary.config_id)
    if not baseline_summary:
        return []
    findings: list[GateFinding] = []
    baseline_by_family = dict(baseline_summary.get("by_family") or {})
    for family, baseline_metrics in baseline_by_family.items():
        actual = summary.by_family.get(str(family), {})
        if not isinstance(baseline_metrics, dict):
            continue
        for metric in ("page_recall_at_10", "chunk_recall_at_10", "mrr_at_10", "ndcg_at_10"):
            regression = float(baseline_metrics.get(metric, actual.get(metric, 0.0))) - actual.get(metric, 0.0)
            if metric in baseline_metrics and regression > QUALITY_REGRESSION_BUDGET:
                findings.append(
                    _finding(
                        split,
                        source,
                        summary.config_id,
                        f"{family}.{metric}",
                        f"{metric} regressed by more than {QUALITY_REGRESSION_BUDGET}",
                    )
                )
    baseline_metrics = dict(baseline_summary.get("metrics") or {})
    baseline_p95 = float(baseline_metrics.get("latency_p95_ms", 0.0))
    actual_p95 = summary.metrics.get("latency_p95_ms", 0.0)
    if baseline_p95 > 0 and actual_p95 > baseline_p95 * LATENCY_REGRESSION_MULTIPLIER:
        findings.append(
            _finding(
                split,
                source,
                summary.config_id,
                "latency_p95_ms",
                "p95 latency grew by more than 25%",
            )
        )
    return findings


def _baseline_summary(baseline: dict[str, Any], source: str, config_id: str) -> dict[str, Any]:
    source_payload = baseline.get(source, baseline)
    if not isinstance(source_payload, dict):
        return {}
    config_payload = source_payload.get(config_id, source_payload)
    return dict(config_payload) if isinstance(config_payload, dict) else {}


def _finding(
    split: Literal["dev", "test"],
    source: Literal["answer", "retrieval"],
    config_id: str,
    metric: str,
    message: str,
) -> GateFinding:
    return GateFinding(
        split=split,
        source=source,
        config_id=config_id,
        metric=metric,
        message=message,
        blocking=split == "test",
    )


async def _timed_stage(
    status: ReleaseGateStatus,
    stage: Literal["dev_answer", "dev_retrieval", "test_answer", "test_retrieval"],
    *,
    started: float,
    timings: dict[str, int],
    progress_callback: ReleaseGateProgressCallback | None,
    run_coro: Callable[[Callable[[Any, str], None]], Awaitable[Any]],
) -> tuple[ReleaseGateStatus, Any]:
    run_dir = Path(status.run_dir)
    status = _start_release_gate_stage(status, stage, started=started, timings=timings)
    _write_release_gate_status(status)
    _log_release_gate_event(run_dir, "stage_started", status=status)
    _emit_release_gate(progress_callback, status, "stage_started")
    stage_started = time.perf_counter()

    def child_callback(child_status: Any, event: str) -> None:
        nonlocal status
        status = _release_gate_status_from_child(status, child_status, started=started, timings=timings)
        _write_release_gate_status(status)
        _log_release_gate_event(run_dir, f"child_{event}", status=status)
        _emit_release_gate(progress_callback, status, f"child_{event}")

    result = await run_coro(child_callback)
    timings[stage] = int((time.perf_counter() - stage_started) * 1000)
    status = _release_gate_status_from_manifest(status, result, started=started, timings=timings)
    _write_release_gate_status(status)
    _log_release_gate_event(run_dir, "stage_completed", status=status)
    _emit_release_gate(progress_callback, status, "stage_completed")
    return status, result


def _start_release_gate_stage(
    status: ReleaseGateStatus,
    stage: Literal["dev_answer", "dev_retrieval", "test_answer", "test_retrieval", "gate_evaluation"],
    *,
    started: float,
    timings: dict[str, int],
) -> ReleaseGateStatus:
    return _advance_release_gate_status(status, started=started, timings=timings).model_copy(
        update={
            "phase": stage,
            "current_stage": stage,
            "stage_index": RELEASE_GATE_STAGES.index(stage) + 1,
            "child_run_id": "",
            "child_run_dir": "",
            "child_processed_task_runs": 0,
            "child_total_task_runs": 0,
            "child_failed_task_runs": 0,
            "updated_at": utc_now_iso(),
        }
    )


def _release_gate_status_from_child(
    status: ReleaseGateStatus,
    child_status: EvalRunStatus | RetrievalEvalStatus,
    *,
    started: float,
    timings: dict[str, int],
) -> ReleaseGateStatus:
    return _advance_release_gate_status(status, started=started, timings=timings).model_copy(
        update={
            "child_run_id": child_status.run_id,
            "child_run_dir": child_status.run_dir,
            "child_processed_task_runs": child_status.processed_task_runs,
            "child_total_task_runs": child_status.total_task_runs,
            "child_failed_task_runs": child_status.failed_task_runs,
            "updated_at": utc_now_iso(),
        }
    )


def _release_gate_status_from_manifest(
    status: ReleaseGateStatus,
    child_manifest: EvalRunManifest | RetrievalRunManifest,
    *,
    started: float,
    timings: dict[str, int],
) -> ReleaseGateStatus:
    total = sum(summary.task_count for summary in child_manifest.config_summaries)
    failed = sum(len(summary.failed_task_ids) for summary in child_manifest.config_summaries)
    return _advance_release_gate_status(status, started=started, timings=timings).model_copy(
        update={
            "child_run_id": child_manifest.run_id,
            "child_run_dir": child_manifest.run_dir,
            "child_processed_task_runs": total,
            "child_total_task_runs": total,
            "child_failed_task_runs": failed,
            "updated_at": utc_now_iso(),
        }
    )


def _advance_release_gate_status(
    status: ReleaseGateStatus,
    *,
    started: float,
    timings: dict[str, int],
) -> ReleaseGateStatus:
    return status.model_copy(
        update={
            "elapsed_seconds": max(0.0, time.perf_counter() - started),
            "timings_ms": dict(timings),
            "updated_at": utc_now_iso(),
        }
    )


def _write_release_gate_status(status: ReleaseGateStatus) -> None:
    payload = status.model_dump(mode="json")
    write_json(Path(status.run_dir) / "status.json", payload)
    write_json(_release_gate_suite_dir(status.suite) / "latest-status.json", payload)
    write_json(ARTIFACT_ROOT / "release-gates" / "latest-status.json", payload)


def _log_release_gate_event(
    run_dir: Path,
    event: str,
    *,
    status: ReleaseGateStatus,
    errors: list[str] | None = None,
) -> None:
    from wikipediarag.eval.artifacts import append_jsonl

    append_jsonl(
        run_dir / "logs" / "events.jsonl",
        {
            "event": event,
            "timestamp": utc_now_iso(),
            "run_id": status.run_id,
            "suite": status.suite,
            "stage": status.current_stage,
            "stage_index": status.stage_index,
            "child_run_id": status.child_run_id,
            "child_processed_task_runs": status.child_processed_task_runs,
            "child_total_task_runs": status.child_total_task_runs,
            "child_failed_task_runs": status.child_failed_task_runs,
            "errors": errors or [],
        },
    )


def _emit_release_gate(callback: ReleaseGateProgressCallback | None, status: ReleaseGateStatus, event: str) -> None:
    if callback is not None:
        callback(status, event)


def _release_gate_run_dir(suite: str, run_id: str) -> Path:
    return _release_gate_suite_dir(suite) / run_id


def _release_gate_suite_dir(suite: str) -> Path:
    return ARTIFACT_ROOT / "release-gates" / suite


def _format_stage_timings(timings_ms: dict[str, int]) -> str:
    if not timings_ms:
        return "{}"
    return ",".join(f"{key}:{value}" for key, value in sorted(timings_ms.items()))


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
