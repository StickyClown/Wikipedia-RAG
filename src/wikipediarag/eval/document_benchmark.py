from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import httpx

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.api_client import HttpEvalApiClient
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    append_jsonl,
    dataset_hash,
    read_json,
    read_jsonl,
    utc_now_iso,
    write_json,
    write_jsonl,
)
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.metrics import aggregate, percentile
from wikipediarag.eval.runner import _eval_overrides, run_task
from wikipediarag.eval.schemas import EvalConfig, EvalDatasetManifest, EvalTask, EvalTaskResult, TaskScores
from wikipediarag.reliability import safe_failure_from_exception
from wikipediarag.retrieval_profile import get_retrieval_profile

RRNCB_DATASET = "rrncb-public"
RRNCB_SUITE = "rrncb-public-v1"
RRNCB_REVISION = "a88b57f29165650f47d21e551fb683063acac166"
RRNCB_CSV_URL = (
    f"https://huggingface.co/datasets/FractalGPT/RRNCBPublic/resolve/{RRNCB_REVISION}/rrncb_public_dataset.csv"
)
RRNCB_ARCHIVE_URL = "https://drive.google.com/drive/folders/1B12Y-QX9UfI9RDJDZ8KZkfF7FUz5q3hy?usp=sharing"
NO_ANSWER_RE = re.compile(r"не\s+содержится\s+информац|информац\w*\s+отсутств", re.IGNORECASE)


class RRNcBError(RuntimeError):
    pass


@dataclass(frozen=True)
class RRNcBPaths:
    base: Path
    tasks: Path
    manifest: Path
    source_manifest: Path
    mapping: Path
    ingestion_state: Path
    ingestion_events: Path
    results: Path
    report: Path
    report_markdown: Path
    results_csv: Path
    status: Path
    run_contract: Path


def rrncb_paths(suite: str = RRNCB_SUITE, artifacts_dir: Path | None = None) -> RRNcBPaths:
    root = artifacts_dir or ARTIFACT_ROOT
    base = root / RRNCB_DATASET / suite
    return RRNcBPaths(
        base=base,
        tasks=base / "tasks.jsonl",
        manifest=base / "tasks.manifest.json",
        source_manifest=base / "source-manifest.json",
        mapping=base / "document-mapping.json",
        ingestion_state=base / "ingestion-status.json",
        ingestion_events=base / "ingestion-events.jsonl",
        results=base / "results.jsonl",
        report=base / "report.json",
        report_markdown=base / "report.md",
        results_csv=base / "results.csv",
        status=base / "latest-status.json",
        run_contract=base / "run-contract.json",
    )


def _rrncb_run_paths(paths: RRNcBPaths, run_id: str) -> RRNcBPaths:
    """Keep immutable per-run results separate from the reusable suite/ingestion state."""

    base = paths.base / "runs" / run_id
    return replace(
        paths,
        base=base,
        results=base / "results.jsonl",
        report=base / "report.json",
        report_markdown=base / "report.md",
        results_csv=base / "results.csv",
        status=base / "latest-status.json",
        run_contract=base / "run-contract.json",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normal_name(value: str) -> str:
    return unicodedata.normalize("NFC", Path(value).name).strip()


def _load_csv(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [{str(key): str(value or "").strip() for key, value in row.items()} for row in csv.DictReader(handle)]
    if not rows:
        raise RRNcBError("RRNCB CSV is empty")
    required = {"question", "answer", "document"}
    if not required <= set(rows[0]):
        raise RRNcBError(f"RRNCB CSV must contain {sorted(required)}")
    if len(rows) != 200:
        raise RRNcBError(f"expected 200 RRNCB rows, got {len(rows)}")
    if any(not all(row.get(field) for field in required) for row in rows):
        raise RRNcBError("RRNCB rows must have non-empty question, answer and document")
    return rows


def _fetch_csv(path: Path) -> None:
    if path.exists() and path.stat().st_size:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    response = httpx.get(RRNCB_CSV_URL, timeout=60, follow_redirects=True)
    response.raise_for_status()
    path.write_bytes(response.content)


def _stable_split(rows: list[dict[str, str]]) -> dict[int, Literal["dev", "test"]]:
    groups: dict[tuple[str, bool], list[tuple[str, int]]] = {}
    for index, row in enumerate(rows):
        cohort = "technical" if _normal_name(row["document"]).startswith("Data") else "legal"
        no_answer = bool(NO_ANSWER_RE.search(row["answer"]))
        groups.setdefault((cohort, no_answer), []).append((stable_json_hash(row["question"]), index))
    selected: set[int] = set()
    for values in groups.values():
        values.sort()
        selected.update(index for _, index in values[: max(1, round(len(values) * 0.2))])
    # Force the documented 40/160 split without changing deterministic order.
    ordered = sorted(selected)
    if len(ordered) > 40:
        selected = set(ordered[:40])
    elif len(ordered) < 40:
        remaining = sorted(
            set(range(len(rows))) - selected, key=lambda index: stable_json_hash(rows[index]["question"])
        )
        selected.update(remaining[: 40 - len(selected)])
    return {index: ("dev" if index in selected else "test") for index in range(len(rows))}


def prepare_rrncb(
    *,
    documents_dir: Path,
    suite: str = RRNCB_SUITE,
    csv_path: Path | None = None,
    artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    paths = rrncb_paths(suite, artifacts_dir)
    cache_dir = (artifacts_dir or ARTIFACT_ROOT) / "external" / RRNCB_DATASET / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    input_csv = csv_path or cache_dir / "rrncb_public_dataset.csv"
    _fetch_csv(input_csv)
    rows = _load_csv(input_csv)
    pdfs = sorted(
        (item for item in documents_dir.iterdir() if item.is_file() and item.suffix.casefold() == ".pdf"),
        key=lambda item: item.name,
    )
    if len(pdfs) != 65:
        raise RRNcBError(f"expected exactly 65 PDF files, got {len(pdfs)}")
    by_name: dict[str, Path] = {}
    for pdf in pdfs:
        name = _normal_name(pdf.name)
        if name in by_name:
            raise RRNcBError(f"duplicate normalized PDF basename: {name}")
        cached_pdf = cache_dir / name
        if pdf.resolve() != cached_pdf.resolve():
            cached_pdf.write_bytes(pdf.read_bytes())
        by_name[name] = cached_pdf
    counts: dict[str, int] = {}
    for row in rows:
        name = _normal_name(row["document"])
        if name not in by_name:
            raise RRNcBError(f"CSV document is missing from PDF directory: {name}")
        counts[name] = counts.get(name, 0) + 1
    if len(counts) != 53:
        raise RRNcBError(f"expected exactly 53 unique CSV documents, got {len(counts)}")
    no_answer_count = sum(bool(NO_ANSWER_RE.search(row["answer"])) for row in rows)
    if no_answer_count != 15:
        raise RRNcBError(f"expected exactly 15 RRNCB no-answer rows, got {no_answer_count}")
    split = _stable_split(rows)
    tasks: list[EvalTask] = []
    for index, row in enumerate(rows, start=1):
        source_name = _normal_name(row["document"])
        unanswerable = bool(NO_ANSWER_RE.search(row["answer"]))
        tasks.append(
            EvalTask(
                task_id=f"rrncb-{index:04d}",
                question=row["question"],
                task_family="unanswerable" if unanswerable else "single_hop_factual",
                reference_answer=row["answer"],
                accepted_answers=[row["answer"]],
                unanswerable=unanswerable,
                expected_mode="unanswerable" if unanswerable else "normal_sufficient",
                gold_page_ids=[source_name],
                gold_section_ids=[],
                gold_chunk_ids=[],
                gold_evidence=[],
                reasoning_path=[],
                generator_alias="generator_main",
                verifier_alias="verifier",
                zim_checksum="",
                snapshot_id="",
                index_version="upload",
                retrieval_profile_hash="",
                language="ru",
                tags=["rrncb", "document_level", "soft_unanswerable" if unanswerable else "answerable"],
                gold_document_ids=[source_name],
                evaluation_granularity="document",
                split=split[index - 1],
                source_document_name=source_name,
            )
        )
    paths.base.mkdir(parents=True, exist_ok=True)
    digest = dataset_hash(tasks)
    manifest = EvalDatasetManifest(
        dataset_name=suite,
        dataset_version=f"rrncb-public@{RRNCB_REVISION}",
        dataset_hash=digest,
        task_count=len(tasks),
        created_at=utc_now_iso(),
        snapshot_id="",
        index_version="upload",
        zim_checksum=_sha256(input_csv.read_bytes()),
        retrieval_profile_hash="",
        generator_alias="generator_main",
        verifier_alias="verifier",
        jsonl_path=str(paths.tasks),
        metadata={
            "source_dataset": RRNCB_DATASET,
            "source_revision": RRNCB_REVISION,
            "license": "MIT",
            "source_csv_url": RRNCB_CSV_URL,
            "source_archive_url": RRNCB_ARCHIVE_URL,
            "documents_total": len(pdfs),
            "referenced_documents": len(counts),
            "unreferenced_documents": len(pdfs) - len(counts),
            "dev_count": sum(task.split == "dev" for task in tasks),
            "test_count": sum(task.split == "test" for task in tasks),
        },
    )
    write_jsonl(paths.tasks, tasks)
    write_json(paths.manifest, manifest.model_dump(mode="json"))
    source_documents = []
    for name in sorted(by_name, key=lambda item: (-counts.get(item, 0), item)):
        data = by_name[name].read_bytes()
        source_documents.append(
            {
                "filename": name,
                "path": str(by_name[name]),
                "sha256": _sha256(data),
                "size_bytes": len(data),
                "question_count": counts.get(name, 0),
                "referenced": name in counts,
            }
        )
    write_json(
        paths.source_manifest,
        {
            "dataset": manifest.model_dump(mode="json"),
            "csv_sha256": _sha256(input_csv.read_bytes()),
            "documents": source_documents,
        },
    )
    return {
        "suite": suite,
        "manifest": str(paths.manifest),
        "source_manifest": str(paths.source_manifest),
        "task_count": 200,
        "dataset_hash": digest,
    }


class _BenchmarkApi:
    def __init__(self, api: str, settings: Settings, timeout: float = 180.0) -> None:
        self.api = api.rstrip("/")
        self.settings = settings
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)
        self.csrf = ""
        self._logged_in = False

    def login(self) -> None:
        if self._logged_in or self.settings.eval_auth_mode != "local":
            return
        response = self.client.post(
            f"{self.api}/api/v1/auth/local/login",
            json={"username": self.settings.eval_auth_username, "password": self.settings.eval_auth_password},
        )
        response.raise_for_status()
        session = self.client.get(f"{self.api}/api/v1/auth/session")
        session.raise_for_status()
        self.csrf = str(session.json().get("csrf_token") or "")
        self._logged_in = True

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.login()
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        response = self.client.request(method, f"{self.api}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self.client.close()


def _load_tasks(paths: RRNcBPaths) -> tuple[EvalDatasetManifest, list[EvalTask]]:
    manifest = EvalDatasetManifest.model_validate(read_json(paths.manifest))
    return manifest, read_jsonl(paths.tasks, EvalTask)


def _remap_tasks(
    paths: RRNcBPaths, manifest: EvalDatasetManifest, tasks: list[EvalTask], kb_id: str, mapping: dict[str, str]
) -> EvalDatasetManifest:
    remapped = [
        task.model_copy(
            update={
                "gold_page_ids": [mapping[task.source_document_name]],
                "gold_document_ids": [mapping[task.source_document_name]],
                "knowledge_base_ids": [kb_id],
            }
        )
        for task in tasks
    ]
    updated = manifest.model_copy(
        update={
            "metadata": {
                **manifest.metadata,
                "prepared_dataset_hash": manifest.metadata.get("prepared_dataset_hash", manifest.dataset_hash),
                "knowledge_base_id": kb_id,
            },
        }
    )
    write_jsonl(paths.tasks, remapped)
    write_json(paths.manifest, updated.model_dump(mode="json"))
    return updated


def _create_or_resume_kb(api: _BenchmarkApi, suite: str, dataset_hash_value: str, state: dict[str, Any]) -> str:
    if state.get("knowledge_base_id"):
        return str(state["knowledge_base_id"])
    name = f"bench-{RRNCB_DATASET}-{dataset_hash_value[:12]}"
    visible = api.request("GET", "/api/v1/knowledge-bases").json()
    if isinstance(visible, list):
        existing = next((item for item in visible if isinstance(item, dict) and item.get("name") == name), None)
        if isinstance(existing, dict) and existing.get("id"):
            return str(existing["id"])
    payload = api.request("POST", "/api/v1/knowledge-bases", json={"name": name}).json()
    kb_id = str(payload.get("id") or "")
    if not kb_id:
        raise RRNcBError("knowledge-base creation returned no id")
    state.update({"knowledge_base_id": kb_id, "knowledge_base_name": name, "dataset_hash": dataset_hash_value})
    return kb_id


def _upload_one(
    url: str,
    content: bytes,
    headers: dict[str, str],
    retry_state: dict[str, int],
    retry_lock: Lock,
) -> float:
    started = time.perf_counter()
    for attempt in range(2):
        try:
            response = httpx.put(url, content=content, headers=headers, timeout=180, follow_redirects=True)
            response.raise_for_status()
            return time.perf_counter() - started
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            retryable = isinstance(exc, httpx.RequestError) or (
                isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {429, 502, 503, 504}
            )
            if not retryable or attempt == 1:
                raise
            with retry_lock:
                if retry_state["used"] >= 10:
                    raise
                retry_state["used"] += 1
            time.sleep(0.5)
    raise RuntimeError("upload did not return a result")


def ingest_rrncb(
    *,
    suite: str = RRNCB_SUITE,
    api_url: str = "http://localhost:8000",
    batch_size: int = 5,
    upload_concurrency: int = 2,
    document_timeout: int = 900,
    batch_timeout: int = 1800,
    suite_timeout: int = 21600,
    resume: bool = True,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    artifacts_dir: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if batch_size != 5 or upload_concurrency != 2:
        raise ValueError("RRNCB ingestion requires --batch-size 5 and --upload-concurrency 2")
    paths = rrncb_paths(suite, artifacts_dir)
    manifest, tasks = _load_tasks(paths)
    source = read_json(paths.source_manifest)
    documents = list(source.get("documents") or [])
    if len(documents) != 65:
        raise RRNcBError("source manifest must contain exactly 65 documents")
    state = read_json(paths.ingestion_state) if paths.ingestion_state.exists() else {}
    requested_run_id = resume_run_id or run_id
    if resume_run_id and not state:
        raise RRNcBError("resume run state was not found for the suite")
    if requested_run_id and state.get("run_id") and state["run_id"] != requested_run_id:
        raise RRNcBError("run id does not match the prepared ingestion suite")
    if state.get("status") == "failed" and not rerun_failed:
        raise RRNcBError("ingestion has failed documents; rerun requires --rerun-failed")
    prepared_hash = str(manifest.metadata.get("prepared_dataset_hash") or manifest.dataset_hash)
    if state and str(state.get("prepared_dataset_hash") or state.get("dataset_hash") or "") != prepared_hash:
        raise RRNcBError("resume dataset hash does not match prepared suite")
    resolved = settings or get_settings()
    client = _BenchmarkApi(api_url, resolved, timeout=180)
    started_suite = time.perf_counter()
    try:
        kb_id = _create_or_resume_kb(client, suite, manifest.dataset_hash, state)
        state.update(
            {
                "suite": suite,
                "dataset_hash": manifest.dataset_hash,
                "prepared_dataset_hash": prepared_hash,
                "started_at": state.get("started_at", utc_now_iso()),
                "knowledge_base_id": kb_id,
                "run_id": state.get("run_id") or requested_run_id or suite,
            }
        )
        state.setdefault("items", {})
        state.setdefault("batches", [])
        state.setdefault("active_batches", {})
        write_json(paths.ingestion_state, state)
        mapping: dict[str, str] = {
            str(key): str(value["document_id"])
            for key, value in state["items"].items()
            if isinstance(value, dict) and value.get("document_id")
        }
        retry_state = {"used": 0}
        retry_lock = Lock()
        for batch_index in range(0, len(documents), batch_size):
            if time.perf_counter() - started_suite > suite_timeout:
                raise TimeoutError("RRNCB ingestion suite timeout")
            batch_docs = documents[batch_index : batch_index + batch_size]
            batch_number = batch_index // batch_size + 1
            pending = [
                doc
                for doc in batch_docs
                if not (
                    isinstance(state["items"].get(doc["filename"]), dict)
                    and state["items"][doc["filename"]].get("status") == "completed"
                )
            ]
            if not pending:
                continue
            batch_started = time.perf_counter()
            active_batch = state["active_batches"].get(str(batch_number))
            existing_batch_id = str(active_batch.get("batch_id") or "") if isinstance(active_batch, dict) else ""
            resume_existing_batch = bool(
                existing_batch_id
                and all(
                    isinstance(state["items"].get(doc["filename"]), dict)
                    and state["items"][doc["filename"]].get("status") in {"queued", "completed"}
                    for doc in pending
                )
            )
            append_jsonl(
                paths.ingestion_events,
                {
                    "event": "batch_started",
                    "batch_index": batch_index // batch_size + 1,
                    "documents": [doc["filename"] for doc in batch_docs],
                    "started_at": utc_now_iso(),
                },
            )
            if resume_existing_batch:
                batch_id = existing_batch_id
            else:
                items = [
                    {
                        "filename": doc["filename"],
                        "content_type": "application/pdf",
                        "size_bytes": int(doc["size_bytes"]),
                        "checksum_sha256": doc["sha256"],
                        "parser_profile": "standard",
                        "metadata": {
                            "rrncb_suite": suite,
                            "rrncb_dataset_hash": manifest.dataset_hash,
                            "rrncb_source_filename": doc["filename"],
                        },
                    }
                    for doc in pending
                ]
                upload_session_created_at = utc_now_iso()
                batch_idempotency_key = stable_json_hash(
                    {
                        "suite": suite,
                        "dataset_hash": manifest.dataset_hash,
                        "batch_number": batch_number,
                        "items": [(doc["filename"], doc["sha256"]) for doc in pending],
                    }
                )
                accepted = client.request(
                    "POST",
                    "/api/v1/uploads/batches",
                    headers={"Idempotency-Key": batch_idempotency_key},
                    json={
                        "knowledge_base_id": kb_id,
                        "metadata": {"rrncb_suite": suite, "rrncb_dataset_hash": manifest.dataset_hash},
                        "items": items,
                    },
                ).json()
                accepted_items = list(accepted.get("items") or [])
                by_name = {str(item.get("filename")): item for item in accepted_items if isinstance(item, dict)}
                batch_id = str(accepted.get("batch_id") or "")
                if not batch_id:
                    raise RRNcBError("upload batch did not return batch_id")
                state["active_batches"][str(batch_number)] = {
                    "batch_id": batch_id,
                    "documents": [doc["filename"] for doc in batch_docs],
                    "started_at": utc_now_iso(),
                    "idempotency_key": batch_idempotency_key,
                }
                for doc in pending:
                    accepted_item = by_name.get(doc["filename"])
                    if not accepted_item:
                        raise RRNcBError(f"upload batch omitted {doc['filename']}")
                    state["items"][doc["filename"]] = {
                        "status": "uploading",
                        "upload_session_id": accepted_item["upload_session_id"],
                        "upload_session_created_at": upload_session_created_at,
                        "size_bytes": doc["size_bytes"],
                        "sha256": doc["sha256"],
                        "batch_index": batch_number,
                    }
                # Persist the batch and session IDs before any object-storage
                # PUT.  A restart can now reissue the same idempotent batch.
                write_json(paths.ingestion_state, state)
                upload_futures: dict[str, Any] = {}
                with ThreadPoolExecutor(max_workers=upload_concurrency) as executor:
                    for doc in pending:
                        accepted_item = by_name.get(doc["filename"])
                        if not accepted_item:
                            raise RRNcBError(f"upload batch omitted {doc['filename']}")
                        content = Path(doc["path"]).read_bytes()
                        upload_futures[doc["filename"]] = executor.submit(
                            _upload_one,
                            str(accepted_item["upload_url"]),
                            content,
                            dict(accepted_item.get("required_headers") or {}),
                            retry_state,
                            retry_lock,
                        )
                    for doc in pending:
                        accepted_item = by_name.get(doc["filename"])
                        if not accepted_item:
                            raise RRNcBError(f"upload batch omitted {doc['filename']}")
                        upload_seconds = float(upload_futures[doc["filename"]].result())
                        complete_started = time.perf_counter()
                        completed = client.request(
                            "POST",
                            f"/api/v1/uploads/sessions/{accepted_item['upload_session_id']}:complete",
                            headers={
                                "Idempotency-Key": stable_json_hash(
                                    [suite, manifest.dataset_hash, doc["filename"], doc["sha256"], "complete"]
                                )
                            },
                            json={"metadata": {"rrncb_completed_at": utc_now_iso()}},
                        ).json()
                        state["items"][doc["filename"]].update(
                            {
                                "status": "queued",
                                "upload_session_id": accepted_item["upload_session_id"],
                                "job_id": completed.get("job_id"),
                                "document_id": completed.get("document_id"),
                                "document_version_id": completed.get("document_version_id"),
                                "upload_seconds": upload_seconds,
                                "upload_session_created_at": upload_session_created_at,
                                "upload_transferred_at": utc_now_iso(),
                                "upload_completed_at": utc_now_iso(),
                                "ingestion_started_at_unix": time.time(),
                                "complete_seconds": time.perf_counter() - complete_started,
                                "size_bytes": doc["size_bytes"],
                                "sha256": doc["sha256"],
                                "batch_index": batch_number,
                            }
                        )
                        write_json(paths.ingestion_state, state)
            item_started = {
                doc["filename"]: float(
                    dict(state["items"].get(doc["filename"]) or {}).get("ingestion_started_at_unix") or time.time()
                )
                for doc in pending
            }
            last_progress_signature: dict[str, str] = {}
            deadline = time.perf_counter() + batch_timeout
            while time.perf_counter() < deadline:
                status_payload = client.request("GET", f"/api/v1/uploads/batches/{batch_id}").json()
                for item in list(status_payload.get("items") or []):
                    if not isinstance(item, dict):
                        continue
                    row = state["items"].get(str(item.get("filename")), {})
                    row.update(
                        {
                            "job_status": item.get("job_status"),
                            "progress": item.get("progress") or {},
                            "document_id": item.get("document_id") or row.get("document_id"),
                            "document_version_id": item.get("document_version_id") or row.get("document_version_id"),
                            "job_id": item.get("job_id") or row.get("job_id"),
                            "error_code": item.get("error_code"),
                            "parser_route": (item.get("progress") or {}).get("parser_route"),
                            "parser_queue_wait_ms": (item.get("progress") or {}).get("parser_queue_wait_ms"),
                            "parser_latency_ms": (item.get("progress") or {}).get("parser_latency_ms"),
                            "chunks_published": (item.get("progress") or {}).get("chunks_published"),
                        }
                    )
                    if row.get("job_status") in {"completed", "failed", "cancelled"}:
                        row["elapsed_seconds"] = round(
                            time.time() - item_started.get(str(item.get("filename")), time.time()), 3
                        )
                    state["items"][str(item.get("filename"))] = row
                    progress = row.get("progress") or {}
                    filename = str(item.get("filename") or "")
                    signature = f"{row.get('job_status')}:{progress.get('stage')}"
                    if signature != last_progress_signature.get(filename):
                        append_jsonl(
                            paths.ingestion_events,
                            {
                                "event": "document_stage",
                                "filename": filename,
                                "stage": progress.get("stage"),
                                "job_status": row.get("job_status"),
                                "updated_at": utc_now_iso(),
                            },
                        )
                        last_progress_signature[filename] = signature
                    if (
                        row.get("job_status") not in {"completed", "failed", "cancelled"}
                        and time.time() - item_started.get(str(item.get("filename")), time.time()) > document_timeout
                    ):
                        raise TimeoutError(f"RRNCB document timeout: {item.get('filename')}")
                completed_count = sum(
                    1
                    for item in state["items"].values()
                    if item.get("batch_index") == batch_index // batch_size + 1
                    and item.get("job_status") == "completed"
                )
                failed_count = sum(
                    1
                    for item in state["items"].values()
                    if item.get("batch_index") == batch_index // batch_size + 1
                    and item.get("job_status") in {"failed", "cancelled"}
                )
                elapsed = time.perf_counter() - started_suite
                print(
                    json.dumps(
                        {
                            "elapsed_seconds": round(elapsed, 3),
                            "batch": batch_index // batch_size + 1,
                            "total_batches": math.ceil(len(documents) / batch_size),
                            "documents_completed": completed_count,
                            "documents_total": len(batch_docs),
                            "failed": failed_count,
                            "status": status_payload.get("status"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                write_json(paths.ingestion_state, state)
                if failed_count:
                    state["status"] = "failed"
                    write_json(paths.ingestion_state, state)
                    raise RRNcBError(f"RRNCB batch {batch_index // batch_size + 1} has failed documents")
                if completed_count == len(batch_docs):
                    break
                time.sleep(3)
            else:
                raise TimeoutError(f"RRNCB batch {batch_index // batch_size + 1} timeout")
            batch_record = {
                "batch_index": batch_index // batch_size + 1,
                "batch_id": batch_id,
                "documents": [doc["filename"] for doc in batch_docs],
                "elapsed_seconds": round(time.perf_counter() - batch_started, 3),
                "documents_per_minute": round(
                    len(batch_docs) / max(time.perf_counter() - batch_started, 0.001) * 60, 3
                ),
                "mib_per_minute": round(
                    sum(float(doc["size_bytes"]) for doc in batch_docs)
                    / (1024 * 1024)
                    / max(time.perf_counter() - batch_started, 0.001)
                    * 60,
                    3,
                ),
                "pdf_time_p50_seconds": percentile(
                    [float(state["items"][doc["filename"]].get("elapsed_seconds", 0.0)) for doc in batch_docs], 50
                ),
                "pdf_time_p95_seconds": percentile(
                    [float(state["items"][doc["filename"]].get("elapsed_seconds", 0.0)) for doc in batch_docs], 95
                ),
                "completed_at": utc_now_iso(),
            }
            elapsed_suite = time.perf_counter() - started_suite
            completed_batches = batch_index // batch_size + 1
            batch_record["eta_seconds"] = round(
                elapsed_suite / completed_batches * (math.ceil(len(documents) / batch_size) - completed_batches), 3
            )
            state["batches"].append(batch_record)
            append_jsonl(paths.ingestion_events, {"event": "batch_completed", **batch_record})
            for doc in batch_docs:
                row = state["items"][doc["filename"]]
                if row.get("job_status") != "completed":
                    raise RRNcBError(f"document did not publish: {doc['filename']}")
                row["status"] = "completed"
                mapping[doc["filename"]] = str(row["document_id"])
            write_json(paths.ingestion_state, state)
        if len(mapping) != 65:
            raise RRNcBError(f"expected 65 published documents, got {len(mapping)}")
        write_json(
            paths.mapping,
            {
                "knowledge_base_id": kb_id,
                "dataset_hash": prepared_hash,
                "documents": {
                    filename: {
                        "document_id": str(state["items"][filename].get("document_id") or ""),
                        "document_version_id": str(state["items"][filename].get("document_version_id") or ""),
                        "sha256": str(state["items"][filename].get("sha256") or ""),
                    }
                    for filename in sorted(mapping)
                },
            },
        )
        updated_manifest = _remap_tasks(paths, manifest, tasks, kb_id, mapping)
        state.update(
            {
                "status": "completed",
                "finished_at": utc_now_iso(),
                "dataset_hash": updated_manifest.dataset_hash,
                "prepared_dataset_hash": prepared_hash,
            }
        )
        write_json(paths.ingestion_state, state)
        return {
            "status": "completed",
            "suite": suite,
            "knowledge_base_id": kb_id,
            "documents": 65,
            "dataset_hash": updated_manifest.dataset_hash,
            "elapsed_seconds": round(time.perf_counter() - started_suite, 3),
        }
    finally:
        client.close()


def _rrncb_config(settings: Settings, profile_name: str) -> EvalConfig:
    profile = get_retrieval_profile(profile_name, settings)
    overrides = _eval_overrides({"postprocess": {"extended_search": "off"}})
    payload = {
        "profile": profile_name,
        "overrides": overrides,
        "mode": "normal",
        "model_aliases": profile.model_aliases.model_dump(),
    }
    return EvalConfig(
        config_id=f"rrncb_{profile_name}",
        retrieval_profile=profile_name,
        retrieval_overrides=overrides,
        mode="normal",
        config_hash=stable_json_hash(payload),
        model_aliases=profile.model_aliases.model_dump(),
    )


def _result_summary(tasks: list[EvalTask], results: list[EvalTaskResult]) -> dict[str, Any]:
    by_task = {task.task_id: task for task in tasks}
    completed = [result for result in results if result.status == "completed" and result.scores]
    answerable = [result for result in completed if not by_task[result.task_id].unanswerable]
    unanswerable = [result for result in completed if by_task[result.task_id].unanswerable]
    all_answerable = [task for task in tasks if not task.unanswerable]
    all_unanswerable = [task for task in tasks if task.unanswerable]
    scores = [result.scores for result in completed if result.scores]
    retrieval_scores: list[TaskScores] = []
    for result in results:
        score = result.retrieval_scores or result.scores
        if score is not None:
            retrieval_scores.append(score)
    steady_state = [result for result in completed if not result.cold_start] or completed
    denominator = len(tasks)
    result_rows = [result for result in results if result.status in {"completed", "failed", "reused"}]
    server_terminal_rows = [result for result in result_rows if result.server_terminal_event]
    conditional: dict[str, Any] = {
        "task_count": len(tasks),
        "completed": len(completed),
        "failed": len(tasks) - len(completed),
        "result_row_terminal_rate": len(result_rows) / denominator if denominator else 0.0,
        "server_terminal_event_rate": len(server_terminal_rows) / denominator if denominator else 0.0,
        "document_recall_at_1": aggregate(score.document_recall.get("1", 0.0) for score in scores),
        "document_recall_at_5": aggregate(score.document_recall.get("5", 0.0) for score in scores),
        "document_recall_at_10": aggregate(score.document_recall.get("10", 0.0) for score in scores),
        "document_mrr_at_10": aggregate(score.document_mrr_at_10 for score in scores),
        "document_ndcg_at_10": aggregate(score.document_ndcg_at_10 for score in scores),
        "document_reranker_gold_delta": aggregate(score.document_reranker_gold_delta or 0.0 for score in scores),
        "document_citation_precision": aggregate(score.document_citation_precision for score in scores),
        "document_citation_recall": aggregate(score.document_citation_recall for score in scores),
        "gold_document_citation_hit": aggregate(score.gold_document_citation_hit for score in scores),
        "exact_match": aggregate(score.exact_match for score in scores),
        "token_f1": aggregate(result.scores.token_f1 for result in answerable if result.scores),
        "rouge_l": aggregate(result.scores.rouge_l for result in answerable if result.scores),
        "soft_unanswerable_accuracy": aggregate(
            result.scores.unanswerable_accuracy for result in unanswerable if result.scores
        ),
        "cold_start_count": sum(result.cold_start for result in completed),
        "latency_p50_ms": percentile([float(result.latency_ms.get("total", 0)) for result in steady_state], 50),
        "latency_p95_ms": percentile([float(result.latency_ms.get("total", 0)) for result in steady_state], 95),
        "model_calls": sum(
            float(result.usage["model_calls"])
            for result in completed
            if isinstance(result.usage.get("model_calls"), int | float)
        ),
        "tokens": sum(
            float(result.usage["total_tokens"])
            for result in completed
            if isinstance(result.usage.get("total_tokens"), int | float)
        ),
        "error_rate": (len(tasks) - len(completed)) / len(tasks) if tasks else 0.0,
    }
    conditional_metric_names = (
        "document_recall_at_1",
        "document_recall_at_5",
        "document_recall_at_10",
        "document_mrr_at_10",
        "document_ndcg_at_10",
        "document_citation_precision",
        "document_citation_recall",
        "gold_document_citation_hit",
        "exact_match",
        "token_f1",
        "rouge_l",
        "soft_unanswerable_accuracy",
    )
    end_to_end: dict[str, Any] = {
        name: (float(conditional[name]) * len(completed) / denominator if denominator else 0.0)
        for name in conditional_metric_names
    }
    end_to_end["document_recall_at_1"] = aggregate(score.document_recall.get("1", 0.0) for score in retrieval_scores)
    end_to_end["document_recall_at_5"] = aggregate(score.document_recall.get("5", 0.0) for score in retrieval_scores)
    end_to_end["document_recall_at_10"] = aggregate(score.document_recall.get("10", 0.0) for score in retrieval_scores)
    end_to_end["document_mrr_at_10"] = aggregate(score.document_mrr_at_10 for score in retrieval_scores)
    end_to_end["document_ndcg_at_10"] = aggregate(score.document_ndcg_at_10 for score in retrieval_scores)
    end_to_end["token_f1"] = (
        sum(result.scores.token_f1 for result in answerable if result.scores) / len(all_answerable)
        if all_answerable
        else 0.0
    )
    end_to_end["rouge_l"] = (
        sum(result.scores.rouge_l for result in answerable if result.scores) / len(all_answerable)
        if all_answerable
        else 0.0
    )
    end_to_end["soft_unanswerable_accuracy"] = (
        sum(result.scores.unanswerable_accuracy for result in unanswerable if result.scores) / len(all_unanswerable)
        if all_unanswerable
        else 0.0
    )
    end_to_end["task_count"] = denominator
    end_to_end["successful_completion_rate"] = len(completed) / denominator if denominator else 0.0
    end_to_end["result_row_terminal_rate"] = len(result_rows) / denominator if denominator else 0.0
    end_to_end["server_terminal_event_rate"] = len(server_terminal_rows) / denominator if denominator else 0.0
    end_to_end["technical_error_rate"] = 1.0 - end_to_end["successful_completion_rate"]
    end_to_end["error_taxonomy"] = dict(conditional["error_taxonomy"])
    end_to_end["actual_retries"] = conditional["actual_retries"]
    end_to_end["latency_p50_ms"] = percentile([float(result.latency_ms.get("total", 0)) for result in results], 50)
    end_to_end["latency_p95_ms"] = percentile([float(result.latency_ms.get("total", 0)) for result in results], 95)
    end_to_end["retrieval_latency_p50_ms"] = percentile(
        [float(result.latency_ms.get("retrieval", 0)) for result in results], 50
    )
    end_to_end["retrieval_latency_p95_ms"] = percentile(
        [float(result.latency_ms.get("retrieval", 0)) for result in results], 95
    )
    end_to_end["soft_unanswerable_completion_rate"] = (
        len(unanswerable) / len(all_unanswerable) if all_unanswerable else 0.0
    )
    # Keep the old key as a compatibility alias; it now clearly means a
    # successful terminal completion, not merely a result row being written.
    end_to_end["terminal_completion_rate"] = end_to_end["successful_completion_rate"]
    conditional["successful_completion_rate"] = end_to_end["successful_completion_rate"]
    conditional["terminal_completion_rate"] = end_to_end["successful_completion_rate"]
    conditional["retrieval_latency_p50_ms"] = percentile(
        [float(result.latency_ms.get("retrieval", 0)) for result in steady_state], 50
    )
    conditional["retrieval_latency_p95_ms"] = percentile(
        [float(result.latency_ms.get("retrieval", 0)) for result in steady_state], 95
    )
    conditional["generation_latency_p50_ms"] = percentile(
        [float(result.latency_ms.get("model_chat", 0)) for result in steady_state], 50
    )
    conditional["generation_latency_p95_ms"] = percentile(
        [float(result.latency_ms.get("model_chat", 0)) for result in steady_state], 95
    )
    conditional["empty_evidence_rate"] = aggregate(float(not result.reranked_candidates) for result in completed)
    error_codes: dict[str, int] = {}
    for result in results:
        if result.status == "completed":
            continue
        failure = dict(result.usage.get("failure") or {})
        fallback = result.failure_code or (result.errors[0] if result.errors else "unknown")
        code = str(failure.get("code") or fallback)
        error_codes[code] = error_codes.get(code, 0) + 1
    conditional["error_taxonomy"] = dict(sorted(error_codes.items()))
    conditional["actual_retries"] = sum(max(0, int(result.attempts) - 1) for result in results)
    return {"conditional": conditional, "end_to_end": end_to_end, **conditional}


def _search_document_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        candidates = event.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("document_id"):
                ids.append(str(candidate["document_id"]))
    for evidence in payload.get("evidence", []):
        if isinstance(evidence, dict) and evidence.get("document_id"):
            ids.append(str(evidence["document_id"]))
    return ids


def _require_ready(api_url: str) -> dict[str, Any]:
    response = httpx.get(f"{api_url.rstrip('/')}/ready", timeout=30, follow_redirects=True)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RRNcBError(f"API is not ready: {payload}")
    return payload


async def _rrncb_preflight(
    *,
    paths: RRNcBPaths,
    tasks: list[EvalTask],
    kb_id: str,
    profile_name: str,
    client: HttpEvalApiClient,
    api_url: str,
    config_hash: str,
    dataset_hash_value: str,
) -> dict[str, Any]:
    smoke_tasks = tasks[:10]
    hits = 0
    failures: list[str] = []
    contract_ids: set[str] = set()
    for task in smoke_tasks:
        kwargs: dict[str, Any] = {
            "api": api_url,
            "top_k": 20,
            "retrieval_profile": profile_name,
            "retrieval_overrides": _eval_overrides({"postprocess": {"extended_search": "off"}}),
            "knowledge_base_ids": [kb_id],
        }
        try:
            payload = await asyncio.to_thread(client.run_search_debug, task.question, **kwargs)
        except Exception as exc:
            failure = safe_failure_from_exception(exc, stage="retrieval")
            failures.append(f"{task.task_id}:{failure.error_code}")
            continue
        contract = payload.get("index_contract_id")
        if isinstance(contract, str) and contract:
            contract_ids.add(contract)
        if set(_search_document_ids(payload)[:10]) & set(task.gold_document_ids):
            hits += 1
    report = {
        "total": len(smoke_tasks),
        "hits_at_10": hits,
        "threshold": 8,
        "failures": failures,
        "index_contract_ids": sorted(contract_ids),
        "knowledge_base_id": kb_id,
        "profile": profile_name,
        "config_hash": config_hash,
        "dataset_hash": dataset_hash_value,
        "passed": not failures and hits >= 8 and len(contract_ids) <= 1,
        "created_at": utc_now_iso(),
    }
    write_json(paths.base / "preflight.json", report)
    if not report["passed"]:
        raise RRNcBError(f"RRNCB preflight failed: {report}")
    return report


def _rrncb_run_contract(
    *,
    paths: RRNcBPaths,
    manifest: EvalDatasetManifest,
    ingestion_state: dict[str, Any],
    kb_id: str,
    config: EvalConfig,
) -> dict[str, Any]:
    source = read_json(paths.source_manifest)
    documents = list(source.get("documents") or [])
    document_hashes = sorted(
        {
            str(item.get("filename")): str(item.get("sha256"))
            for item in documents
            if isinstance(item, dict) and item.get("filename") and item.get("sha256")
        }.items()
    )
    return {
        "dataset_hash": manifest.dataset_hash,
        "knowledge_base_id": kb_id,
        "ingestion_run_id": str(ingestion_state.get("run_id") or ""),
        "profile": config.retrieval_profile,
        "config_hash": config.config_hash,
        "model_aliases": config.model_aliases,
        "document_hashes": document_hashes,
    }


def _validate_or_write_run_contract(paths: RRNcBPaths, expected: dict[str, Any]) -> None:
    if paths.run_contract.exists():
        actual = read_json(paths.run_contract)
        if actual != expected:
            raise RRNcBError("RRNCB run contract mismatch; create a new suite for a changed corpus or profile")
        return
    write_json(paths.run_contract, expected)


async def run_rrncb(
    *,
    suite: str = RRNCB_SUITE,
    api_url: str = "http://localhost:8000",
    profile_name: str = "upload_sota_mvp",
    batch_size: int = 2,
    question_timeout: int = 300,
    suite_timeout: int = 28800,
    resume: bool = True,
    rerun_failed: bool = False,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    artifacts_dir: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    paths = rrncb_paths(suite, artifacts_dir)
    manifest, tasks = _load_tasks(paths)
    state = read_json(paths.ingestion_state)
    kb_id = str(state.get("knowledge_base_id") or "")
    if not kb_id or state.get("status") != "completed":
        raise RRNcBError("ingestion must complete before running RRNCB RAG")
    requested_run_id = resume_run_id or run_id
    if resume_run_id and not (paths.base / "runs" / resume_run_id).exists():
        raise RRNcBError("resume run artifacts were not found for the suite")
    actual_run_id = requested_run_id or f"{suite}-{manifest.dataset_hash[:12]}-{int(time.time())}"
    run_paths = _rrncb_run_paths(paths, actual_run_id)
    resolved = settings or get_settings()
    config = _rrncb_config(resolved, profile_name)
    ready_payload = _require_ready(api_url)
    # The API owns the 300-second operation deadline.  Keep the client read
    # timeout slightly above it so a terminal SSE event is not cut off by a
    # second independent timer.
    client = HttpEvalApiClient.from_settings(resolved, timeout=question_timeout + 15)
    preflight_path = paths.base / "preflight.json"
    preflight = read_json(preflight_path) if preflight_path.exists() else {}
    if not (
        bool(preflight.get("passed"))
        and preflight.get("knowledge_base_id") == kb_id
        and preflight.get("profile") == profile_name
        and preflight.get("config_hash") == config.config_hash
        and preflight.get("dataset_hash") == manifest.dataset_hash
    ):
        await _rrncb_preflight(
            paths=paths,
            tasks=tasks,
            kb_id=kb_id,
            profile_name=profile_name,
            client=client,
            api_url=api_url,
            config_hash=config.config_hash,
            dataset_hash_value=manifest.dataset_hash,
        )
        preflight = read_json(preflight_path)
    run_contract = _rrncb_run_contract(
        paths=run_paths,
        manifest=manifest,
        ingestion_state=state,
        kb_id=kb_id,
        config=config,
    )
    run_contract["run_id"] = actual_run_id
    run_contract["index_contract_ids"] = list(preflight.get("index_contract_ids") or [])
    run_contract["retrieval_profile_hash"] = stable_json_hash(
        get_retrieval_profile(profile_name, resolved).model_dump(mode="json")
    )
    _validate_or_write_run_contract(run_paths, run_contract)
    old_results = read_jsonl(run_paths.results, EvalTaskResult) if resume and run_paths.results.exists() else []
    incompatible = [
        result
        for result in old_results
        if result.dataset_hash
        and (result.dataset_hash != manifest.dataset_hash or result.config_hash != config.config_hash)
    ]
    if incompatible:
        raise RRNcBError("existing RRNCB results have a different dataset or profile contract")
    existing = {result.task_id: result for result in old_results if result.status == "completed" or not rerun_failed}
    ordered = sorted(tasks, key=lambda task: (0 if task.split == "dev" else 1, task.task_id))
    cold_start_ids = {task.task_id for task in [item for item in ordered if item.split == "dev"][:5]}
    started = time.perf_counter()
    completed_count = len(existing)
    status: dict[str, Any] = {
        "status": "running",
        "run_id": actual_run_id,
        "suite": suite,
        "dataset_hash": manifest.dataset_hash,
        "knowledge_base_id": kb_id,
        "total": len(tasks),
        "completed": completed_count,
        "failed": sum(1 for result in existing.values() if result.status != "completed"),
        "started_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "retry_budget": 20,
        "retries_used": 0,
    }
    write_json(run_paths.status, status)

    async def run_one(task: EvalTask) -> EvalTaskResult:
        task = task.model_copy(update={"knowledge_base_ids": [kb_id]})

        async def acquire_retry_slot(_failure: dict[str, Any]) -> bool:
            async with retry_budget_lock:
                if retry_budget_used["count"] >= 20:
                    return False
                retry_budget_used["count"] += 1
                status["retries_used"] = retry_budget_used["count"]
                return True

        try:
            result = await run_task(
                task,
                config,
                api=api_url,
                manifest=manifest,
                client=client,
                settings=resolved,
                max_attempts=2,
                retry_slot_acquire=acquire_retry_slot,
            )
            return result.model_copy(update={"cold_start": task.task_id in cold_start_ids})
        except Exception as exc:
            failure = safe_failure_from_exception(exc, stage="api_request", attempt=1)
            return EvalTaskResult(
                task_id=task.task_id,
                config_id=config.config_id,
                config_hash=config.config_hash,
                status="failed",
                question=task.question,
                cold_start=task.task_id in cold_start_ids,
                failure_stage=failure.stage,
                failure_code=failure.error_code,
                failure_retryable=failure.retryable,
                last_successful_stage="",
                usage={"failure": failure.model_dump()},
                errors=[failure.error_code],
                attempts=1,
            )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(15)
            status["updated_at"] = utc_now_iso()
            status["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            write_json(run_paths.status, status)
            print(
                json.dumps(
                    {
                        "elapsed_seconds": status["elapsed_seconds"],
                        "processed": status["completed"],
                        "total": status["total"],
                        "failed": status["failed"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    retry_budget_used = {"count": 0}
    retry_budget_lock = asyncio.Lock()
    pending_tasks = [task for task in ordered if task.task_id not in existing]
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        for offset in range(0, len(pending_tasks), batch_size):
            if time.perf_counter() - started > suite_timeout:
                raise TimeoutError("RRNCB RAG suite timeout")
            batch = pending_tasks[offset : offset + batch_size]
            results = await asyncio.gather(*(run_one(task) for task in batch))
            for result in results:
                append_jsonl(run_paths.results, result)
                existing[result.task_id] = result
                if result.status == "completed":
                    completed_count += 1
                else:
                    status["failed"] += 1
            status["completed"] = completed_count
            status["updated_at"] = utc_now_iso()
            status["last_batch"] = offset // batch_size + 1
            write_json(run_paths.status, status)
            print(
                json.dumps(
                    {
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                        "processed": completed_count + int(status["failed"]),
                        "total": len(tasks),
                        "failed": status["failed"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        final_results = [existing[task.task_id] for task in tasks if task.task_id in existing]
        task_by_id = {task.task_id: task for task in tasks}
        dev_tasks = [task for task in tasks if task.split == "dev"]
        test_tasks = [task for task in tasks if task.split == "test"]
        dev_results = [result for result in final_results if task_by_id[result.task_id].split == "dev"]
        test_results = [result for result in final_results if task_by_id[result.task_id].split == "test"]
        report: dict[str, Any] = {
            "suite": suite,
            "dataset_hash": manifest.dataset_hash,
            "knowledge_base_id": kb_id,
            "config": config.model_dump(mode="json"),
            "ready": ready_payload,
            "all": _result_summary(tasks, final_results),
            "dev": _result_summary(dev_tasks, dev_results),
            "test": _result_summary(test_tasks, test_results),
            "created_at": utc_now_iso(),
        }
        write_json(run_paths.report, report)
        run_paths.report_markdown.write_text(
            "\n".join(
                [
                    f"# RRNCBPublic benchmark: {suite}",
                    "",
                    f"Dataset hash: `{manifest.dataset_hash}`",
                    f"Knowledge base: `{kb_id}`",
                    "",
                    "| Split | Completed | Terminal rate | Conditional R@10 | E2E R@10 | ROUGE-L |",
                    "|---|---:|---:|---:|---:|---:|",
                    *[
                        (
                            "| {split} | {completed} | {terminal:.3f} | {recall10:.3f} | "
                            "{e2e_recall10:.3f} | {rouge:.3f} |"
                        ).format(
                            split=split,
                            completed=report[split]["completed"],
                            terminal=report[split]["terminal_completion_rate"],
                            recall10=report[split]["document_recall_at_10"],
                            e2e_recall10=report[split]["end_to_end"]["document_recall_at_10"],
                            rouge=report[split]["rouge_l"],
                        )
                        for split in ("dev", "test", "all")
                    ],
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        with run_paths.results_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "task_id",
                    "split",
                    "status",
                    "cold_start",
                    "query_run_id",
                    "failure_stage",
                    "failure_code",
                    "failure_retryable",
                    "latency_total_ms",
                    "latency_retrieval_ms",
                    "latency_generation_ms",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "model_aliases",
                    "cited_document_ids",
                    "document_recall_at_10",
                    "document_citation_precision",
                    "document_citation_recall",
                    "gold_document_citation_hit",
                    "rouge_l",
                ],
            )
            writer.writeheader()
            for result in final_results:
                task = task_by_id[result.task_id]
                writer.writerow(
                    {
                        "task_id": result.task_id,
                        "split": task.split,
                        "status": result.status,
                        "cold_start": result.cold_start,
                        "query_run_id": result.query_run_id or "",
                        "failure_stage": result.failure_stage,
                        "failure_code": result.failure_code,
                        "failure_retryable": result.failure_retryable,
                        "latency_total_ms": result.latency_ms.get("total", 0),
                        "latency_retrieval_ms": result.latency_ms.get("retrieval", 0),
                        "latency_generation_ms": result.latency_ms.get("model_chat", 0),
                        "input_tokens": result.usage.get("input_tokens"),
                        "output_tokens": result.usage.get("output_tokens"),
                        "total_tokens": result.usage.get("total_tokens"),
                        "model_aliases": json.dumps(result.model_aliases, ensure_ascii=False, sort_keys=True),
                        "cited_document_ids": ";".join(result.cited_document_ids),
                        "document_recall_at_10": (
                            result.scores.document_recall.get("10", 0.0) if result.scores else 0.0
                        ),
                        "document_citation_precision": (
                            result.scores.document_citation_precision if result.scores else 0.0
                        ),
                        "document_citation_recall": (result.scores.document_citation_recall if result.scores else 0.0),
                        "gold_document_citation_hit": (
                            result.scores.gold_document_citation_hit if result.scores else 0.0
                        ),
                        "rouge_l": result.scores.rouge_l if result.scores else 0.0,
                    }
                )
        status.update(
            {
                "status": "completed" if len(final_results) == len(tasks) and not status["failed"] else "failed",
                "finished_at": utc_now_iso(),
                "report": str(run_paths.report),
                "report_markdown": str(run_paths.report_markdown),
                "results_csv": str(run_paths.results_csv),
            }
        )
        write_json(run_paths.status, status)
        return report
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


def rrncb_status(*, suite: str = RRNCB_SUITE, artifacts_dir: Path | None = None) -> dict[str, Any]:
    paths = rrncb_paths(suite, artifacts_dir)
    run_dirs = sorted((paths.base / "runs").glob("*/latest-status.json"), key=lambda item: item.stat().st_mtime)
    if run_dirs:
        return read_json(run_dirs[-1])
    if not paths.status.exists():
        raise FileNotFoundError(f"no RRNCB status found for suite {suite}")
    return read_json(paths.status)
