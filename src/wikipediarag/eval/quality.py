"""P0.1 search-quality suite preparation and reporting helpers.

The quality suite is deliberately kept beside the existing evaluation runners.
It validates an immutable local corpus, then delegates execution to the public
evaluation clients already used by the Wikipedia and document suites.
"""

from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, cast

from wikipediarag.eval.artifacts import read_json, utc_now_iso, write_json, write_jsonl
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.schemas import (
    CorpusSource,
    EvalDatasetManifest,
    EvalStageRecord,
    EvalTask,
    EvalTaskResult,
    EvaluationSchemaVersion,
    StageStatus,
)
from wikipediarag.eval.source_binding import RuntimeBindingError, build_runtime_binding

QUALITY_SUITE = "p0-search-quality-v1"
QUALITY_SCHEMA_VERSION = "search_quality_eval_v1"
QUALITY_REVIEW_POLICY = "search_quality_review_v1"
QUALITY_FAMILIES: tuple[str, ...] = (
    "exact_identifier",
    "entity_alias",
    "multilingual",
    "cross_source",
    "table_lookup",
    "multi_hop",
    "partial",
    "conflicting",
    "not_found_in_scope",
    "freshness",
    "citation_span",
)
QUALITY_LANGUAGES: tuple[str, ...] = ("ru", "en", "uk", "de", "ko")
QUALITY_TOTAL_TASKS = 220
QUALITY_TASKS_PER_FAMILY = 20
QUALITY_TASKS_PER_LANGUAGE = 44
QUALITY_DEV_TASKS = 44
QUALITY_TEST_TASKS = 176


class QualitySuiteError(ValueError):
    """Safe, actionable validation error for a local quality suite."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class QualitySuite:
    corpus_dir: Path
    source_manifest_path: Path
    task_manifest_path: Path
    sources: tuple[CorpusSource, ...]
    tasks: tuple[EvalTask, ...]
    source_hash: str
    dataset_hash: str
    manifest: EvalDatasetManifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_tasks(path: Path) -> list[EvalTask]:
    if not path.exists():
        raise QualitySuiteError("TASK_MANIFEST_MISSING", f"missing task manifest: {path.name}")
    tasks: list[EvalTask] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            tasks.append(EvalTask.model_validate(json.loads(line)))
        except Exception as exc:
            raise QualitySuiteError("TASK_INVALID", f"line {line_number}: {exc}") from exc
    return tasks


def _resolve_source(corpus_dir: Path, filename: str) -> Path:
    root = corpus_dir.resolve()
    candidate = (corpus_dir / filename).resolve()
    if candidate != root and root not in candidate.parents:
        raise QualitySuiteError("SOURCE_PATH_INVALID", filename)
    return candidate


def _source_text(path: Path) -> str:
    if path.suffix.casefold() == ".pdf":
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _pdf_page_count(path: Path) -> int:
    """Count PDF page objects without adding a parser dependency to the evaluator."""

    data = path.read_bytes()
    return len(re.findall(rb"/Type\s*/Page(?:\s|/|>)", data))


def _validate_locator(evidence: Any, path: Path, task_id: str) -> None:
    if evidence.locator_type == "text":
        text = _source_text(path)
        if evidence.start_offset is not None and evidence.end_offset is not None:
            if evidence.end_offset > len(text):
                raise QualitySuiteError("EVIDENCE_RANGE_OUT_OF_BOUNDS", task_id)
            selected = text[evidence.start_offset : evidence.end_offset]
            if evidence.quote and evidence.quote not in selected:
                raise QualitySuiteError("EVIDENCE_RANGE_QUOTE_MISMATCH", task_id)
    elif evidence.locator_type == "page":
        if evidence.page_number is None or evidence.page_number > _pdf_page_count(path):
            raise QualitySuiteError("EVIDENCE_PAGE_INVALID", task_id)
    elif evidence.locator_type == "table":
        if path.suffix.casefold() != ".csv":
            raise QualitySuiteError("EVIDENCE_TABLE_SOURCE_INVALID", task_id)
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows or evidence.column_name not in rows[0]:
            raise QualitySuiteError("EVIDENCE_TABLE_COLUMN_INVALID", task_id)
        key_name = next(iter(rows[0]), "")
        if not any(str(row.get(key_name, "")) == evidence.row_key for row in rows):
            raise QualitySuiteError("EVIDENCE_TABLE_ROW_INVALID", task_id)


def _validate_claims(task: EvalTask) -> None:
    claims = task.expected_claims
    claim_ids = [claim.claim_id for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise QualitySuiteError("CLAIM_ID_DUPLICATE", task.task_id)
    evidence_ids = {item.evidence_id for item in task.gold_evidence}
    for claim in claims:
        missing = (set(claim.supports_evidence_ids) | set(claim.contradicts_evidence_ids)) - evidence_ids
        if missing:
            raise QualitySuiteError("CLAIM_EVIDENCE_MISSING", f"{task.task_id}: {sorted(missing)}")


def _validate_task(
    task: EvalTask,
    source_by_id: dict[str, CorpusSource],
    source_paths: dict[str, Path],
    *,
    require_reviewed: bool,
) -> None:
    if task.evaluation_schema_version != QUALITY_SCHEMA_VERSION:
        raise QualitySuiteError("SCHEMA_VERSION_INVALID", task.task_id)
    if task.task_family not in QUALITY_FAMILIES:
        raise QualitySuiteError("FAMILY_INVALID", f"{task.task_id}: {task.task_family}")
    if task.language_group not in QUALITY_LANGUAGES:
        raise QualitySuiteError("LANGUAGE_INVALID", task.task_id)
    if task.expected_outcome is None:
        raise QualitySuiteError("OUTCOME_MISSING", task.task_id)
    if task.split not in {"dev", "test"}:
        raise QualitySuiteError("SPLIT_INVALID", task.task_id)
    if require_reviewed and (not task.reviewed_by or not task.reviewed_at):
        raise QualitySuiteError("REVIEW_MISSING", task.task_id)
    expected_sources = set(task.source_ids)
    required_sources = set(task.required_source_ids)
    forbidden_sources = set(task.forbidden_source_ids)
    if required_sources & forbidden_sources:
        raise QualitySuiteError("SOURCE_SCOPE_OVERLAP", task.task_id)
    if required_sources - expected_sources or forbidden_sources - expected_sources:
        raise QualitySuiteError("SOURCE_SCOPE_INVALID", task.task_id)
    for source_id in expected_sources | set(task.required_source_ids) | set(task.forbidden_source_ids):
        if source_id not in source_by_id:
            raise QualitySuiteError("SOURCE_ID_UNKNOWN", f"{task.task_id}: {source_id}")
    if require_reviewed and task.task_family in {"partial", "conflicting", "not_found_in_scope", "freshness"}:
        review = task.scope_review
        if not review.reviewed or not review.reviewed_by or not review.reviewed_at:
            raise QualitySuiteError("SCOPE_REVIEW_MISSING", task.task_id)
        if set(review.source_ids) != expected_sources:
            raise QualitySuiteError("SCOPE_REVIEW_INCOMPLETE", task.task_id)
        if review.checked_source_count != len(expected_sources):
            raise QualitySuiteError("SCOPE_REVIEW_COUNT_INVALID", task.task_id)
    _validate_claims(task)
    evidence_ids = [item.evidence_id for item in task.gold_evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise QualitySuiteError("EVIDENCE_ID_DUPLICATE", task.task_id)
    for evidence in task.gold_evidence:
        source_id = evidence.source_id or next(
            (candidate for candidate in task.source_ids if candidate == evidence.document_id), ""
        )
        if source_id and source_id not in source_by_id:
            raise QualitySuiteError("EVIDENCE_SOURCE_UNKNOWN", f"{task.task_id}: {source_id}")
        source = source_by_id.get(source_id)
        if source is not None:
            if source.allowed_task_ids and task.task_id not in source.allowed_task_ids:
                raise QualitySuiteError("SOURCE_TASK_NOT_ALLOWED", task.task_id)
            if task.task_id in source.forbidden_task_ids:
                raise QualitySuiteError("SOURCE_TASK_FORBIDDEN", task.task_id)
            if evidence.source_sha256 and evidence.source_sha256 != source.sha256:
                raise QualitySuiteError("EVIDENCE_SOURCE_HASH_MISMATCH", task.task_id)
        if evidence.start_offset is not None and evidence.end_offset is not None:
            if evidence.end_offset < evidence.start_offset:
                raise QualitySuiteError("EVIDENCE_RANGE_INVALID", task.task_id)
        if evidence.locator_type == "page" and evidence.page_number is None:
            raise QualitySuiteError("EVIDENCE_PAGE_MISSING", task.task_id)
        if evidence.locator_type == "table" and not (evidence.table_name and evidence.row_key and evidence.column_name):
            raise QualitySuiteError("EVIDENCE_TABLE_LOCATOR_MISSING", task.task_id)
        if evidence.quote and source_id and source_id in source_paths:
            text = _source_text(source_paths[source_id])
            if text and evidence.quote not in text:
                raise QualitySuiteError("EVIDENCE_QUOTE_NOT_FOUND", f"{task.task_id}: {evidence.evidence_id}")
        if source_id and source_id in source_paths:
            _validate_locator(evidence, source_paths[source_id], task.task_id)


def validate_quality_suite(
    corpus_dir: Path,
    *,
    strict_counts: bool = True,
    require_reviewed: bool = True,
) -> QualitySuite:
    """Validate sources/tasks and return hashes used by all later runs."""

    corpus_dir = corpus_dir.resolve()
    source_manifest_path = corpus_dir / "sources.json"
    task_manifest_path = corpus_dir / "tasks.jsonl"
    if not source_manifest_path.exists():
        raise QualitySuiteError("SOURCE_MANIFEST_MISSING", str(source_manifest_path))
    payload = read_json(source_manifest_path)
    raw_sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(raw_sources, list) or not raw_sources:
        raise QualitySuiteError("SOURCE_MANIFEST_INVALID", "sources must be a non-empty list")
    sources = tuple(CorpusSource.model_validate(item) for item in raw_sources)
    source_by_id = {source.source_id: source for source in sources}
    if len(source_by_id) != len(sources):
        raise QualitySuiteError("SOURCE_ID_DUPLICATE", "source_id must be unique")
    source_paths: dict[str, Path] = {}
    for source in sources:
        path = _resolve_source(corpus_dir, source.filename)
        if not path.exists() or not path.is_file():
            raise QualitySuiteError("SOURCE_FILE_MISSING", source.filename)
        actual_hash = _sha256(path)
        if source.sha256 and source.sha256 != actual_hash:
            raise QualitySuiteError("SOURCE_HASH_MISMATCH", source.source_id)
        source_paths[source.source_id] = path
    tasks = _read_tasks(task_manifest_path)
    if require_reviewed and any(task.reviewed_by == "" or task.reviewed_at == "" for task in tasks):
        missing = next(task.task_id for task in tasks if not task.reviewed_by or not task.reviewed_at)
        raise QualitySuiteError("REVIEW_MISSING", missing)
    task_ids = [task.task_id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise QualitySuiteError("TASK_ID_DUPLICATE", "task_id must be unique")
    evidence_ids: set[str] = set()
    for task in tasks:
        _validate_task(task, source_by_id, source_paths, require_reviewed=require_reviewed)
        overlap = evidence_ids & {evidence.evidence_id for evidence in task.gold_evidence}
        if overlap:
            raise QualitySuiteError("EVIDENCE_ID_DUPLICATE", f"{task.task_id}: {sorted(overlap)}")
        evidence_ids.update(evidence.evidence_id for evidence in task.gold_evidence)
    family_counts = Counter(task.task_family for task in tasks)
    language_counts = Counter(task.language_group for task in tasks)
    split_counts = Counter(task.split for task in tasks)
    expected_families = {family: QUALITY_TASKS_PER_FAMILY for family in QUALITY_FAMILIES}
    expected_languages = {language: QUALITY_TASKS_PER_LANGUAGE for language in QUALITY_LANGUAGES}
    if strict_counts:
        if len(tasks) != QUALITY_TOTAL_TASKS:
            raise QualitySuiteError("TASK_COUNT_INVALID", f"expected {QUALITY_TOTAL_TASKS}, got {len(tasks)}")
        if dict(family_counts) != expected_families:
            raise QualitySuiteError("FAMILY_COUNTS_INVALID", str(dict(family_counts)))
        if dict(language_counts) != expected_languages:
            raise QualitySuiteError("LANGUAGE_COUNTS_INVALID", str(dict(language_counts)))
        if dict(split_counts) != {"dev": QUALITY_DEV_TASKS, "test": QUALITY_TEST_TASKS}:
            raise QualitySuiteError("SPLIT_COUNTS_INVALID", str(dict(split_counts)))
    source_hash = stable_json_hash([source.model_dump(mode="json") for source in sources])
    dataset_hash = stable_json_hash(
        {
            "schema": QUALITY_SCHEMA_VERSION,
            "source_hash": source_hash,
            "tasks": [task.model_dump(mode="json") for task in tasks],
        }
    )
    manifest = EvalDatasetManifest(
        dataset_name=QUALITY_SUITE,
        dataset_version=QUALITY_SCHEMA_VERSION,
        dataset_hash=dataset_hash,
        task_count=len(tasks),
        created_at=utc_now_iso(),
        snapshot_id="quality-corpus",
        index_version="",
        zim_checksum="",
        retrieval_profile_hash="",
        generator_alias="",
        verifier_alias="",
        jsonl_path=str(task_manifest_path),
        metadata={"source_kinds": sorted({source.source_kind for source in sources})},
        evaluation_schema_version=cast(EvaluationSchemaVersion, QUALITY_SCHEMA_VERSION),
        corpus_manifest_hash=source_hash,
        source_count=len(sources),
        required_family_counts=expected_families,
        required_language_counts=expected_languages,
        review_policy_version=QUALITY_REVIEW_POLICY,
        source_manifest_path=str(source_manifest_path),
        task_manifest_path=str(task_manifest_path),
    )
    return QualitySuite(
        corpus_dir=corpus_dir,
        source_manifest_path=source_manifest_path,
        task_manifest_path=task_manifest_path,
        sources=sources,
        tasks=tuple(tasks),
        source_hash=source_hash,
        dataset_hash=dataset_hash,
        manifest=manifest,
    )


def prepare_quality_suite(corpus_dir: Path, *, strict_counts: bool = True) -> EvalDatasetManifest:
    suite = validate_quality_suite(corpus_dir, strict_counts=strict_counts, require_reviewed=False)
    manifest_path = suite.corpus_dir / "manifest.json"
    if manifest_path.exists():
        existing = read_json(manifest_path)
        if existing.get("dataset_hash") != suite.dataset_hash:
            raise QualitySuiteError("SUITE_IMMUTABLE", "manifest already exists with another hash")
        return EvalDatasetManifest.model_validate(existing)
    write_json(manifest_path, suite.manifest.model_dump(mode="json"))
    return suite.manifest


def apply_quality_review(corpus_dir: Path, decisions_path: Path) -> dict[str, Any]:
    """Apply an explicit reviewer decision file before the suite is frozen."""

    suite = validate_quality_suite(corpus_dir, strict_counts=False, require_reviewed=False)
    locked_dir = suite.corpus_dir / "locked"
    if locked_dir.exists() and any(locked_dir.iterdir()):
        raise QualitySuiteError("SUITE_FROZEN", "review decisions cannot change a locked suite")
    if not decisions_path.exists():
        raise QualitySuiteError("REVIEW_FILE_MISSING", str(decisions_path))
    decisions: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(decisions_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QualitySuiteError("REVIEW_FILE_INVALID", f"line {line_number}") from exc
        if not isinstance(row, dict) or not row.get("task_id"):
            raise QualitySuiteError("REVIEW_ROW_INVALID", f"line {line_number}")
        task_id = str(row["task_id"])
        if task_id in decisions:
            raise QualitySuiteError("REVIEW_TASK_DUPLICATE", task_id)
        decisions[task_id] = row
    task_by_id = {task.task_id: task for task in suite.tasks}
    unknown = set(decisions) - set(task_by_id)
    if unknown:
        raise QualitySuiteError("REVIEW_TASK_UNKNOWN", ",".join(sorted(unknown)))
    updated: list[EvalTask] = []
    for task in suite.tasks:
        row = decisions.get(task.task_id)
        if row is None:
            updated.append(task)
            continue
        reviewer = str(row.get("reviewed_by") or "")
        reviewed_at = str(row.get("reviewed_at") or "")
        if not reviewer or not reviewed_at:
            raise QualitySuiteError("REVIEW_FIELDS_MISSING", task.task_id)
        scope_review = task.scope_review
        if isinstance(row.get("scope_review"), dict):
            scope_review = scope_review.model_copy(update=dict(row["scope_review"]))
        updated.append(
            task.model_copy(
                update={
                    "reviewed_by": reviewer,
                    "reviewed_at": reviewed_at,
                    "review_notes": [str(item) for item in row.get("review_notes", task.review_notes)],
                    "scope_review": scope_review,
                }
            )
        )
    write_jsonl(suite.task_manifest_path, updated)
    checked = validate_quality_suite(suite.corpus_dir, strict_counts=False, require_reviewed=False)
    manifest_path = checked.corpus_dir / "manifest.json"
    if manifest_path.exists():
        write_json(manifest_path, checked.manifest.model_dump(mode="json"))
    scope_families = {"partial", "conflicting", "not_found_in_scope", "freshness"}
    pending = [
        task.task_id
        for task in checked.tasks
        if not task.reviewed_by
        or not task.reviewed_at
        or (task.task_family in scope_families and not task.scope_review.reviewed)
    ]
    return {
        "suite": QUALITY_SUITE,
        "decisions_count": len(decisions),
        "reviewed_count": len(checked.tasks) - len(pending),
        "pending_task_ids": pending,
    }


def freeze_quality_suite(corpus_dir: Path) -> dict[str, Any]:
    suite = validate_quality_suite(corpus_dir, strict_counts=True)
    locked_dir = suite.corpus_dir / "locked"
    locked_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"suite": QUALITY_SUITE, "dataset_hash": suite.dataset_hash, "splits": {}}
    for split in ("dev", "test"):
        tasks = [task for task in suite.tasks if task.split == split]
        path = locked_dir / f"{split}.jsonl"
        manifest_path = locked_dir / f"{split}.manifest.json"
        split_hash = stable_json_hash([task.model_dump(mode="json") for task in tasks])
        if path.exists() or manifest_path.exists():
            if (
                not path.exists()
                or not manifest_path.exists()
                or read_json(manifest_path).get("dataset_hash") != split_hash
            ):
                raise QualitySuiteError("SPLIT_IMMUTABLE", split)
        else:
            write_jsonl(path, tasks)
            write_json(
                manifest_path,
                {
                    **suite.manifest.model_dump(mode="json"),
                    "dataset_name": f"{QUALITY_SUITE}-{split}",
                    "dataset_hash": split_hash,
                    "task_count": len(tasks),
                    "split": split,
                    "locked": True,
                    "source_pool_hash": suite.dataset_hash,
                },
            )
        result["splits"][split] = {"count": len(tasks), "dataset_hash": split_hash, "path": str(path)}
    return result


def ingest_quality_suite(
    corpus_dir: Path,
    *,
    api_url: str = "http://localhost:8000",
    batch_size: int = 5,
    upload_concurrency: int = 2,
    timeout_seconds: int = 900,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Upload the immutable corpus through the existing document upload path."""

    if batch_size < 1 or upload_concurrency < 1:
        raise ValueError("batch_size and upload_concurrency must be >= 1")
    if run_id and resume_run_id:
        raise QualitySuiteError("RUN_ID_CONFLICT", "run_id and resume_run_id are mutually exclusive")
    suite = validate_quality_suite(corpus_dir, strict_counts=True)
    resolved = settings
    if resolved is None:
        from wikipediarag.config import get_settings

        resolved = get_settings()
    from wikipediarag.eval.document_benchmark import _BenchmarkApi, _create_or_resume_kb, _upload_one

    ingestion_root = suite.corpus_dir / "ingestion"
    ingestion_root.mkdir(parents=True, exist_ok=True)
    actual_run_id = resume_run_id or run_id or f"{QUALITY_SUITE}-ingest-{suite.dataset_hash[:12]}"
    state_path = ingestion_root / f"{actual_run_id}.json"
    state = read_json(state_path) if state_path.exists() else {}
    if resume_run_id and not state:
        raise QualitySuiteError("INGESTION_STATE_MISSING", actual_run_id)
    if run_id and state:
        raise QualitySuiteError("INGESTION_RUN_EXISTS", "use resume_run_id")
    if state.get("status") == "failed" and not rerun_failed:
        raise QualitySuiteError("INGESTION_FAILED", "rerun_failed is required")
    client = _BenchmarkApi(api_url, resolved, timeout=180)
    started = time.perf_counter()
    try:
        ready = client.request("GET", "/ready").json()
        if not isinstance(ready, dict) or ready.get("status") not in {"ok", "degraded"}:
            raise QualitySuiteError("READINESS_FAILED", "API is not ready")
        state.setdefault("languages", {})
        state.update(
            {
                "suite": QUALITY_SUITE,
                "run_id": actual_run_id,
                "dataset_hash": suite.dataset_hash,
                "status": "running",
                "started_at": state.get("started_at", utc_now_iso()),
            }
        )
        state.setdefault("items", {})
        state.setdefault("batches", [])
        write_json(state_path, state)
        retry_state = {"used": 0}
        retry_lock = Lock()
        knowledge_base_ids: dict[str, str] = {}
        batch_plan: list[tuple[str, list[CorpusSource]]] = []
        for language in QUALITY_LANGUAGES:
            language_state = state["languages"].setdefault(language, {})
            kb_id = _create_or_resume_kb(
                client,
                f"{QUALITY_SUITE}-{language}",
                suite.dataset_hash,
                f"{actual_run_id}-{language}",
                language_state,
            )
            knowledge_base_ids[language] = kb_id
            language_sources = [source for source in suite.sources if source.language_group == language]
            for offset in range(0, len(language_sources), batch_size):
                batch_plan.append((kb_id, language_sources[offset : offset + batch_size]))
        for kb_id, batch_sources in batch_plan:
            pending = [
                source
                for source in batch_sources
                if not (
                    isinstance(state["items"].get(source.source_id), dict)
                    and state["items"][source.source_id].get("status") == "completed"
                )
            ]
            if not pending:
                continue
            items = []
            for source in pending:
                path = _resolve_source(suite.corpus_dir, source.filename)
                items.append(
                    {
                        "filename": source.filename,
                        "content_type": mimetypes.guess_type(source.filename)[0] or "application/octet-stream",
                        "size_bytes": path.stat().st_size,
                        "checksum_sha256": source.sha256,
                        "parser_profile": "standard",
                        "metadata": {
                            "quality_suite": QUALITY_SUITE,
                            "quality_dataset_hash": suite.dataset_hash,
                            "quality_source_id": source.source_id,
                        },
                        "source_ref": {
                            "schema_version": "source_ref_v1",
                            "namespace": f"eval:{QUALITY_SUITE}:{suite.dataset_hash}",
                            "external_id": source.source_id,
                            "source_version": source.revision or f"sha256:{source.sha256}",
                            "attributes": {"original_system_name": source.filename},
                        },
                    }
                )
            accepted = client.request(
                "POST",
                "/api/v1/uploads/batches",
                headers={"Idempotency-Key": stable_json_hash([QUALITY_SUITE, actual_run_id, items])},
                json={
                    "knowledge_base_id": kb_id,
                    "metadata": {"quality_suite": QUALITY_SUITE, "quality_dataset_hash": suite.dataset_hash},
                    "items": items,
                },
            ).json()
            batch_id = str(accepted.get("batch_id") or "")
            accepted_by_name = {
                str(item.get("filename")): item for item in list(accepted.get("items") or []) if isinstance(item, dict)
            }
            if not batch_id or len(accepted_by_name) != len(pending):
                raise QualitySuiteError("UPLOAD_BATCH_INVALID", "upload batch omitted a source")
            for source in pending:
                item = accepted_by_name[source.filename]
                state["items"][source.source_id] = {
                    "filename": source.filename,
                    "knowledge_base_id": kb_id,
                    "status": "uploading",
                    "upload_session_id": item.get("upload_session_id"),
                    "sha256": source.sha256,
                }
            write_json(state_path, state)
            with ThreadPoolExecutor(max_workers=upload_concurrency) as executor:
                futures = {
                    source.source_id: executor.submit(
                        _upload_one,
                        str(accepted_by_name[source.filename].get("upload_url") or ""),
                        _resolve_source(suite.corpus_dir, source.filename).read_bytes(),
                        dict(accepted_by_name[source.filename].get("required_headers") or {}),
                        retry_state,
                        retry_lock,
                    )
                    for source in pending
                }
                for source in pending:
                    futures[source.source_id].result()
                    item = accepted_by_name[source.filename]
                    completed = client.request(
                        "POST",
                        f"/api/v1/uploads/sessions/{item['upload_session_id']}:complete",
                        headers={
                            "Idempotency-Key": stable_json_hash(
                                [QUALITY_SUITE, actual_run_id, source.source_id, "complete"]
                            )
                        },
                        json={"metadata": {"quality_source_id": source.source_id}},
                    ).json()
                    state["items"][source.source_id].update(
                        {
                            "status": "queued",
                            "job_id": completed.get("job_id"),
                            "document_id": completed.get("document_id"),
                            "document_version_id": completed.get("document_version_id"),
                        }
                    )
            deadline = time.perf_counter() + timeout_seconds
            while time.perf_counter() < deadline:
                payload = client.request("GET", f"/api/v1/uploads/batches/{batch_id}").json()
                by_name = {
                    str(item.get("filename")): item
                    for item in list(payload.get("items") or [])
                    if isinstance(item, dict)
                }
                failed = [item for item in by_name.values() if str(item.get("job_status") or "") in {"failed", "error"}]
                if failed:
                    raise QualitySuiteError("INGESTION_DOCUMENT_FAILED", "a document failed")
                done = True
                for source in pending:
                    item = by_name.get(source.filename, {})
                    status = str(item.get("job_status") or "")
                    if status not in {"completed", "published", "succeeded"}:
                        done = False
                    if item.get("document_id"):
                        state["items"][source.source_id]["document_id"] = item["document_id"]
                    state["items"][source.source_id]["job_status"] = status
                write_json(state_path, state)
                if done:
                    for source in pending:
                        state["items"][source.source_id]["status"] = "completed"
                    break
                time.sleep(3)
            else:
                raise QualitySuiteError("INGESTION_TIMEOUT", batch_id)
            write_json(state_path, state)
        mapping = {
            source_id: str(item.get("document_id") or "")
            for source_id, item in state["items"].items()
            if isinstance(item, dict) and item.get("document_id")
        }
        source_mappings = {
            source_id: {
                "knowledge_base_id": str(item.get("knowledge_base_id") or ""),
                "document_id": str(item.get("document_id") or ""),
                "document_version_id": str(item.get("document_version_id") or ""),
            }
            for source_id, item in state["items"].items()
            if isinstance(item, dict) and item.get("document_id")
        }
        state.update(
            {
                "status": "completed",
                "knowledge_base_ids": knowledge_base_ids,
                "source_document_ids": mapping,
                "source_mappings": source_mappings,
                "updated_at": utc_now_iso(),
            }
        )
        signing_key = str(getattr(resolved, "eval_binding_signing_key", "") or "")
        if signing_key:
            namespace = f"eval:{QUALITY_SUITE}:{suite.dataset_hash}"

            def fetch_context(document_id: str) -> list[dict[str, Any]]:
                chunks: list[dict[str, Any]] = []
                for offset in range(0, 501, 200):
                    response = client.request(
                        "GET",
                        f"/api/v1/documents/{document_id}/context",
                        params={"limit": 200, "offset": offset},
                    ).json()
                    page = [item for item in response.get("chunks") or [] if isinstance(item, dict)]
                    chunks.extend(page)
                    if len(page) < 200:
                        return chunks
                raise QualitySuiteError("RUNTIME_BINDING_STALE", f"document has too many chunks: {document_id}")

            try:
                binding = build_runtime_binding(
                    suite=QUALITY_SUITE,
                    dataset_hash=suite.dataset_hash,
                    material_hash=suite.source_hash,
                    namespace=namespace,
                    source_mappings=source_mappings,
                    tasks=[task.model_dump(mode="json") for task in suite.tasks],
                    fetch_context=fetch_context,
                    signing_key=signing_key,
                )
            except RuntimeBindingError as exc:
                raise QualitySuiteError(exc.code, str(exc)) from exc
            binding_path = ingestion_root / f"{actual_run_id}.binding.json"
            write_json(binding_path, binding)
            state["binding"] = {
                "binding_hash": binding["binding_hash"],
                "path": str(binding_path),
                "status": "completed",
            }
        else:
            state["binding"] = {"status": "signing_key_missing"}
        write_json(state_path, state)
        return {
            "suite": QUALITY_SUITE,
            "run_id": actual_run_id,
            "knowledge_base_ids": knowledge_base_ids,
            "source_document_ids": mapping,
            "source_mappings": source_mappings,
            "binding": dict(state["binding"]),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "state_path": str(state_path),
        }
    except Exception:
        state["status"] = "failed"
        state["updated_at"] = utc_now_iso()
        write_json(state_path, state)
        raise
    finally:
        client.close()


def comparison_key(
    *,
    dataset_hash: str,
    config_hash: str,
    contract_ids: dict[str, str],
    model_aliases: dict[str, str],
) -> str:
    return stable_json_hash(
        {
            "dataset_hash": dataset_hash,
            "config_hash": config_hash,
            "contract_ids": dict(sorted(contract_ids.items())),
            "model_aliases": dict(sorted(model_aliases.items())),
        }
    )


def normalize_stage_records(
    events: Iterable[dict[str, Any]],
    *,
    model_aliases: dict[str, str] | None = None,
    failure_code: str = "",
) -> list[EvalStageRecord]:
    """Keep only safe counters and identifiers from retrieval events."""

    aliases = model_aliases or {}
    records: list[EvalStageRecord] = []
    for ordinal, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or event.get("name") or "unknown")
        candidates = [item for item in event.get("candidates", []) if isinstance(item, dict)]
        candidate_ids = [str(item.get("chunk_id")) for item in candidates if item.get("chunk_id")][:50]
        candidate_ranks: list[int] = []
        candidate_scores: list[float] = []
        for item in candidates[:50]:
            rank = item.get("rank") or item.get("position")
            if isinstance(rank, int):
                candidate_ranks.append(rank)
            score = item.get("score")
            if isinstance(score, int | float):
                candidate_scores.append(float(score))
        reasons = [str(event.get(key)) for key in ("reason", "stop_reason", "error_code") if event.get(key)]
        if failure_code and stage == "failure":
            reasons.append(failure_code)
        status: StageStatus = "failed" if event.get("status") == "failed" or event.get("error_code") else "completed"
        records.append(
            EvalStageRecord(
                stage=stage,
                ordinal=ordinal,
                status=status,
                latency_ms=max(0, int(event.get("latency_ms") or event.get("stage_latency_ms") or 0)),
                input_count=int(event["input_count"]) if isinstance(event.get("input_count"), int) else None,
                output_count=(
                    int(event["output_count"]) if isinstance(event.get("output_count"), int) else len(candidates)
                ),
                discarded_count=(
                    int(event["discarded_count"]) if isinstance(event.get("discarded_count"), int) else None
                ),
                candidate_ids=candidate_ids,
                candidate_ranks=candidate_ranks,
                candidate_scores=candidate_scores,
                reason_codes=reasons[:10],
                model_alias=str(event.get("model_alias") or aliases.get(stage, "")),
                model_calls=int(event["model_calls"]) if isinstance(event.get("model_calls"), int) else None,
                input_tokens=int(event["input_tokens"]) if isinstance(event.get("input_tokens"), int) else None,
                output_tokens=int(event["output_tokens"]) if isinstance(event.get("output_tokens"), int) else None,
                attempts=max(0, int(event.get("attempts") or 0)),
            )
        )
    if not records:
        records.append(EvalStageRecord(stage="unknown", ordinal=1, status="not_observed"))
    return records


def _result_value(result: Any, key: str, default: Any = None) -> Any:
    if hasattr(result, key):
        return getattr(result, key, default)
    if isinstance(result, dict):
        return result.get(key, default)
    return default


def predicted_outcome(result: EvalTaskResult | dict[str, Any]) -> str | None:
    explicit = _result_value(result, "predicted_outcome")
    if explicit:
        return str(explicit)
    usage = _result_value(result, "usage", {}) or {}
    status = usage.get("answerability_status") if isinstance(usage, dict) else None
    return {
        "answerable": "answered",
        "partial": "partial",
        "conflicting": "conflicting",
        "unanswerable": "not_found_in_scope",
    }.get(str(status), None)


def macro_f1(expected: Iterable[str], predicted: Iterable[str], labels: Iterable[str]) -> float:
    expected_values = list(expected)
    predicted_values = list(predicted)
    scores: list[float] = []
    for label in labels:
        tp = sum(
            actual == label and guess == label for actual, guess in zip(expected_values, predicted_values, strict=False)
        )
        fp = sum(
            actual != label and guess == label for actual, guess in zip(expected_values, predicted_values, strict=False)
        )
        fn = sum(
            actual == label and guess != label for actual, guess in zip(expected_values, predicted_values, strict=False)
        )
        if tp == 0 and fp == 0 and fn == 0:
            continue
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def build_quality_report(
    tasks: Iterable[EvalTask],
    results: Iterable[EvalTaskResult | dict[str, Any]],
    *,
    retrieval_results: Iterable[Any] = (),
    source_by_id: dict[str, CorpusSource] | None = None,
) -> dict[str, Any]:
    task_list = list(tasks)
    result_list = list(results)
    task_by_id = {task.task_id: task for task in task_list}
    completed = [result for result in result_list if _result_value(result, "status") == "completed"]
    expected = [
        str(task.expected_outcome)
        for result in completed
        if (task := task_by_id.get(str(_result_value(result, "task_id")))) is not None and task.expected_outcome
    ]
    predicted = [str(predicted_outcome(result) or "unknown") for result in completed]
    absent_tasks = [task for task in task_list if task.expected_outcome == "not_found_in_scope"]
    false_absent = sum(
        predicted_outcome(result) not in {"not_found_in_scope", None}
        for result in completed
        if task_by_id.get(str(_result_value(result, "task_id")), None) in absent_tasks
    )
    source_by_id = source_by_id or {}
    retrieval_list = list(retrieval_results)
    all_results: list[Any] = [*result_list, *retrieval_list]

    def comparison_state(items: list[Any]) -> tuple[set[str], int, bool]:
        keys = {str(_result_value(item, "comparison_key")) for item in items if _result_value(item, "comparison_key")}
        missing = sum(1 for item in items if not _result_value(item, "comparison_key"))
        return keys, missing, len(keys) > 1 or (bool(keys) and missing > 0)

    answer_keys, answer_missing, answer_incompatible = comparison_state(result_list)
    retrieval_keys, retrieval_missing, retrieval_incompatible = comparison_state(retrieval_list)
    comparison_keys = answer_keys | retrieval_keys
    missing_comparison_key_count = answer_missing + retrieval_missing
    incompatible_results = answer_incompatible or retrieval_incompatible
    incompatible_fields: set[str] = set()
    for items in (result_list, retrieval_list):
        for field in ("config_hash", "config_id", "comparison_key"):
            values = {str(_result_value(item, field)) for item in items if _result_value(item, field)}
            if len(values) > 1:
                incompatible_fields.add(field)
        contract_values: dict[str, set[str]] = defaultdict(set)
        for item in items:
            contracts = _result_value(item, "contract_ids", {}) or {}
            if isinstance(contracts, dict):
                for name, value in contracts.items():
                    if value:
                        contract_values[str(name)].add(str(value))
        incompatible_fields.update(
            f"contract_ids.{name}" for name, values in contract_values.items() if len(values) > 1
        )
    groups: dict[str, list[EvalTaskResult | dict[str, Any]]] = defaultdict(list)
    for result in result_list:
        task = task_by_id.get(str(_result_value(result, "task_id")))
        if task is None:
            continue
        key_prefix = f"comparison:{_result_value(result, 'comparison_key')}:" if incompatible_results else ""
        groups[f"{key_prefix}family:{task.task_family}"].append(result)
        groups[f"{key_prefix}language:{task.language_group or task.language}"].append(result)
        groups[f"{key_prefix}split:{task.split}"].append(result)
        kinds = sorted(
            {source_by_id[source_id].source_kind for source_id in task.source_ids if source_id in source_by_id}
        )
        groups[f"{key_prefix}source:{','.join(kinds) or 'unknown'}"].append(result)

    def aggregate(items: list[EvalTaskResult | dict[str, Any]]) -> dict[str, float]:
        values: dict[str, list[float]] = defaultdict(list)
        for item in items:
            scores = _result_value(item, "scores")
            if scores is None:
                continue
            if hasattr(scores, "model_dump"):
                scores = scores.model_dump(mode="json")
            for key in (
                "citation_precision",
                "citation_recall",
                "unsupported_claim_rate",
                "answer_groundedness",
                "mrr_at_10",
                "ndcg_at_10",
                "full_hop_recall",
                "path_completion",
                "reranker_gold_delta",
            ):
                value = scores.get(key) if isinstance(scores, dict) else None
                if value is None and key == "answer_groundedness" and isinstance(scores, dict):
                    unsupported = scores.get("unsupported_claim_rate")
                    value = 1.0 - float(unsupported) if unsupported is not None else None
                if isinstance(value, int | float):
                    values[key].append(float(value))
            latency = _result_value(item, "latency_ms", {}) or {}
            for key in ("total", "retrieval"):
                value = latency.get(key) if isinstance(latency, dict) else None
                if isinstance(value, int | float):
                    values[f"latency_{key}_ms"].append(float(value))
            if isinstance(scores, dict):
                page_recall = scores.get("page_recall")
                if isinstance(page_recall, dict):
                    for place in ("1", "5", "10", "20"):
                        value = page_recall.get(place)
                        if isinstance(value, int | float):
                            values[f"page_recall_at_{place}"].append(float(value))
        result: dict[str, float] = {key: sum(items) / len(items) for key, items in values.items() if items}
        result["completed"] = float(len([item for item in items if _result_value(item, "status") == "completed"]))
        result["failed"] = float(len(items) - int(result["completed"]))
        return result

    completed_latencies: dict[str, list[float]] = defaultdict(list)
    for result in completed:
        latency = _result_value(result, "latency_ms", {}) or {}
        if isinstance(latency, dict):
            for name, value in latency.items():
                if isinstance(value, int | float):
                    completed_latencies[str(name)].append(float(value))

    def percentile_value(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
        return ordered[index]

    retrieval_metrics = aggregate(retrieval_list)
    split_summary: dict[str, dict[str, float]] = {}
    for split in ("dev", "test"):
        split_task_ids = {task.task_id for task in task_list if task.split == split}
        split_results = [result for result in result_list if str(_result_value(result, "task_id")) in split_task_ids]
        split_summary[split] = {
            "tasks": float(len(split_task_ids)),
            "results": float(len(split_results)),
            "completed": float(sum(_result_value(result, "status") == "completed" for result in split_results)),
            "failed": float(sum(_result_value(result, "status") != "completed" for result in split_results)),
        }
    report: dict[str, Any] = {
        "suite": QUALITY_SUITE,
        "task_count": len(task_list),
        "result_count": len(result_list),
        "completed_count": len(completed),
        "failed_count": len(result_list) - len(completed),
        "completion_rate": len(completed) / len(task_list) if task_list else 0.0,
        "answerability_macro_f1": macro_f1(
            expected,
            predicted,
            ("answered", "partial", "conflicting", "not_found_in_scope"),
        ),
        "false_answer_rate_not_found_in_scope": false_absent / len(absent_tasks) if absent_tasks else 0.0,
        "metrics": {} if answer_incompatible else aggregate(result_list),
        "retrieval_result_count": len(retrieval_list),
        "retrieval_metrics": {} if retrieval_incompatible else retrieval_metrics,
        "latency_summary_ms": {
            name: {
                "median": percentile_value(values, 0.5),
                "p95": percentile_value(values, 0.95),
            }
            for name, values in sorted(completed_latencies.items())
        },
        "comparison_status": (
            "incompatible_results"
            if incompatible_results
            else ("not_observed" if not comparison_keys else "comparable")
        ),
        "state": "incompatible_results"
        if incompatible_results
        else ("complete" if len(completed) == len(task_list) else "incomplete"),
        "comparison_keys": sorted(comparison_keys),
        "missing_comparison_key_count": missing_comparison_key_count,
        "incompatible_fields": sorted(incompatible_fields),
        "split_summary": split_summary,
        "by_group": {name: aggregate(items) for name, items in sorted(groups.items())},
        "stage_error_counts": dict(
            Counter(
                record.reason_codes[0]
                for result in all_results
                for record in (_result_value(result, "stage_records", []) or [])
                if getattr(record, "status", "") == "failed" and getattr(record, "reason_codes", [])
            )
        ),
    }
    return report


def write_quality_report(path: Path, report: dict[str, Any]) -> None:
    write_json(path, report)
