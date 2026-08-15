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
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import httpx

from wikipediarag.answering import ANSWER_JSON_SCHEMA
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
from wikipediarag.eval.retrieval_runner import run_retrieval_task
from wikipediarag.eval.runner import _eval_overrides, run_task
from wikipediarag.eval.schemas import (
    EvalConfig,
    EvalDatasetManifest,
    EvalTask,
    EvalTaskResult,
    RetrievalTaskResult,
    TaskScores,
)
from wikipediarag.reliability import safe_failure_from_exception
from wikipediarag.retrieval_profile import get_retrieval_profile

RRNCB_DATASET = "rrncb-public"
RRNCB_SUITE = "rrncb-public-v3"
RRNCB_REVISION = "a88b57f29165650f47d21e551fb683063acac166"
RRNCB_CSV_URL = (
    f"https://huggingface.co/datasets/FractalGPT/RRNCBPublic/resolve/{RRNCB_REVISION}/rrncb_public_dataset.csv"
)
RRNCB_ARCHIVE_URL = "https://drive.google.com/drive/folders/1B12Y-QX9UfI9RDJDZ8KZkfF7FUz5q3hy?usp=sharing"
RRNCB_MULTILINGUAL_LANGUAGES = ("ru", "en", "uk", "de", "ko")
NO_ANSWER_RE = re.compile(r"не\s+содержится\s+информац|информац\w*\s+отсутств", re.IGNORECASE)
LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
HANGUL_RE = re.compile(r"[가-힣]")


class RRNcBError(RuntimeError):
    def __init__(self, message: str, *, safe_code: str = "RRNCB_ERROR") -> None:
        super().__init__(message)
        self.safe_code = safe_code
        self.retryable = False


class RRNcBIngestionError(RRNcBError):
    def __init__(self, code: str) -> None:
        super().__init__(code, safe_code=code)
        self.code = code


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


def _rrncb_ingestion_paths(paths: RRNcBPaths, ingestion_run_id: str) -> RRNcBPaths:
    """Keep immutable ingestion state separate from prepared and benchmark artifacts."""

    base = paths.base / "ingestions" / ingestion_run_id
    return replace(
        paths,
        base=base,
        mapping=base / "document-mapping.json",
        ingestion_state=base / "ingestion-status.json",
        ingestion_events=base / "ingestion-events.jsonl",
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


def _load_reviewed_translations(path: Path, rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    """Load a complete reviewed translation matrix without trusting generated labels."""

    if not path.is_file():
        raise RRNcBError(f"reviewed translations are missing: {path}")
    source_questions = {f"rrncb-{index:04d}": row["question"] for index, row in enumerate(rows, start=1)}
    translations: dict[tuple[str, str], dict[str, str]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RRNcBError(f"invalid translation JSON on line {line_number}") from exc
        if not isinstance(payload, dict):
            raise RRNcBError(f"translation line {line_number} must be an object")
        base_task_id = str(payload.get("base_task_id") or "")
        language = str(payload.get("language") or "")
        question = str(payload.get("question") or "").strip()
        reviewed_by = str(payload.get("reviewed_by") or "").strip()
        reviewed_at = str(payload.get("reviewed_at") or "").strip()
        if base_task_id not in source_questions or language not in RRNCB_MULTILINGUAL_LANGUAGES[1:]:
            raise RRNcBError(f"invalid translation identity on line {line_number}")
        if not question or not reviewed_by or not reviewed_at:
            raise RRNcBError(f"translation is not reviewed on line {line_number}")
        expected_hash = stable_json_hash(source_questions[base_task_id])
        if str(payload.get("source_question_hash") or "") != expected_hash:
            raise RRNcBError(f"translation source question changed: {base_task_id}")
        source_question = source_questions[base_task_id]
        if (
            unicodedata.normalize("NFKC", question).casefold()
            == unicodedata.normalize("NFKC", source_question).casefold()
        ):
            raise RRNcBError(f"translation repeats the Russian source: {base_task_id}/{language}")
        latin_count = len(LATIN_RE.findall(question))
        cyrillic_count = len(CYRILLIC_RE.findall(question))
        if language in {"en", "de"} and latin_count < max(3, cyrillic_count):
            raise RRNcBError(f"translation script mismatch: {base_task_id}/{language}")
        if language == "uk" and cyrillic_count < 3:
            raise RRNcBError(f"translation script mismatch: {base_task_id}/{language}")
        if language == "ko" and len(HANGUL_RE.findall(question)) < 2:
            raise RRNcBError(f"translation script mismatch: {base_task_id}/{language}")
        key = (base_task_id, language)
        if key in translations:
            raise RRNcBError(f"duplicate translation: {base_task_id}/{language}")
        translations[key] = {
            "question": question,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "review_method": str(payload.get("review_method") or "manual"),
        }
    expected = {
        (base_task_id, language) for base_task_id in source_questions for language in RRNCB_MULTILINGUAL_LANGUAGES[1:]
    }
    missing = sorted(expected - set(translations))
    extra = sorted(set(translations) - expected)
    if missing or extra:
        raise RRNcBError(f"translation matrix mismatch: missing={len(missing)} extra={len(extra)}")
    return translations


def generate_rrncb_translations(
    *,
    csv_path: Path,
    output_path: Path,
    gateway_url: str = "http://localhost:8081",
    batch_size: int = 10,
    model_alias: str = "generator_main",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Create a resumable structured translation matrix for later freeze validation."""

    if batch_size < 1 or batch_size > 10:
        raise ValueError("RRNCB translation batch size must be between 1 and 10")
    rows = _load_csv(csv_path)
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    if output_path.exists():
        for line in output_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                payload = json.loads(line)
                existing[(str(payload["base_task_id"]), str(payload["language"]))] = payload
    owned_client = client is None
    http_client = client or httpx.Client(timeout=180, follow_redirects=True)
    try:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            identities = [f"rrncb-{index:04d}" for index in range(start + 1, start + len(batch) + 1)]
            if all(
                (task_id, language) in existing
                for task_id in identities
                for language in RRNCB_MULTILINGUAL_LANGUAGES[1:]
            ):
                continue
            source_items = [
                {"base_task_id": task_id, "question_ru": row["question"]}
                for task_id, row in zip(identities, batch, strict=True)
            ]
            item_schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["base_task_id", "en", "uk", "de", "ko"],
                "properties": {
                    "base_task_id": {"type": "string"},
                    "en": {"type": "string"},
                    "uk": {"type": "string"},
                    "de": {"type": "string"},
                    "ko": {"type": "string"},
                },
            }
            request = {
                "model": model_alias,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Translate each Russian legal question into English, Ukrainian, German and Korean. "
                            "Preserve legal meaning, negation, numbers, article references and named entities. "
                            "Review every translation before returning it. Return only the requested JSON."
                        ),
                    },
                    {"role": "user", "content": json.dumps(source_items, ensure_ascii=False)},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "rrncb_multilingual_questions",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["translations"],
                            "properties": {
                                "translations": {
                                    "type": "array",
                                    "minItems": len(batch),
                                    "maxItems": len(batch),
                                    "items": item_schema,
                                }
                            },
                        },
                    },
                },
                "thinking": {"mode": "off", "effort": "none", "return_reasoning": False},
                "max_output_tokens": 4096,
                "stream": False,
            }
            response: httpx.Response | None = None
            for attempt in range(5):
                try:
                    response = http_client.post(f"{gateway_url.rstrip('/')}/v1/chat/completions", json=request)
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt == 4:
                        raise
                    time.sleep(min(2**attempt, 15))
                    continue
                if response.status_code not in {429, 502, 503, 504}:
                    break
                if attempt == 4:
                    break
                time.sleep(min(2**attempt, 15))
            if response is None:
                raise RRNcBError("translation request returned no response")
            response.raise_for_status()
            response_payload = response.json()
            choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
            if not isinstance(choices, list) or not choices:
                raise RRNcBError("translation response has no choices")
            content = dict(choices[0].get("message") or {}).get("content")
            translated = json.loads(str(content)) if isinstance(content, str) else content
            items = translated.get("translations") if isinstance(translated, dict) else None
            if not isinstance(items, list) or len(items) != len(batch):
                raise RRNcBError("translation response has an invalid item count")
            by_id = {str(item.get("base_task_id")): item for item in items if isinstance(item, dict)}
            if set(by_id) != set(identities):
                raise RRNcBError("translation response changed task identities")
            reviewed_at = utc_now_iso()
            for task_id, row in zip(identities, batch, strict=True):
                item = by_id[task_id]
                for language in RRNCB_MULTILINGUAL_LANGUAGES[1:]:
                    question = str(item.get(language) or "").strip()
                    if not question:
                        raise RRNcBError(f"translation is empty: {task_id}/{language}")
                    existing[(task_id, language)] = {
                        "base_task_id": task_id,
                        "language": language,
                        "question": question,
                        "source_question_hash": stable_json_hash(row["question"]),
                        "reviewed_by": f"model-assisted:{model_alias}",
                        "reviewed_at": reviewed_at,
                        "review_method": "structured_translation_review_v1",
                    }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(output_path, [existing[key] for key in sorted(existing)])
    finally:
        if owned_client:
            http_client.close()
    return {
        "output_path": str(output_path),
        "base_task_count": len(rows),
        "translation_count": len(existing),
        "languages": list(RRNCB_MULTILINGUAL_LANGUAGES),
    }


def prepare_rrncb(
    *,
    documents_dir: Path,
    suite: str = RRNCB_SUITE,
    csv_path: Path | None = None,
    translations_path: Path | None = None,
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
    translations = _load_reviewed_translations(translations_path, rows) if translations_path else {}
    languages = RRNCB_MULTILINGUAL_LANGUAGES if translations else ("ru",)
    tasks: list[EvalTask] = []
    for index, row in enumerate(rows, start=1):
        source_name = _normal_name(row["document"])
        unanswerable = bool(NO_ANSWER_RE.search(row["answer"]))
        base_task_id = f"rrncb-{index:04d}"
        for language in languages:
            translation = translations.get((base_task_id, language), {})
            task_id = f"{base_task_id}-{language}" if translations else base_task_id
            tasks.append(
                EvalTask(
                    task_id=task_id,
                    question=row["question"] if language == "ru" else translation["question"],
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
                    language=language,
                    language_group=language,
                    tags=[
                        "rrncb",
                        "document_level",
                        "soft_unanswerable" if unanswerable else "answerable",
                        f"base_task:{base_task_id}",
                        f"query_language:{language}",
                        "source_language:ru",
                    ],
                    gold_document_ids=[source_name],
                    evaluation_granularity="document",
                    split=split[index - 1],
                    source_document_name=source_name,
                    reviewed_by="rrncb-public" if language == "ru" else translation["reviewed_by"],
                    reviewed_at=f"source-pinned:{RRNCB_REVISION}" if language == "ru" else translation["reviewed_at"],
                    review_notes=[] if language == "ru" else [f"translation_review:{translation['review_method']}"],
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
            "base_task_count": len(rows),
            "query_languages": list(languages),
            "translations_sha256": _sha256(translations_path.read_bytes()) if translations_path else "",
        },
        evaluation_schema_version="search_quality_eval_v1" if translations else "legacy_eval_v1",
        required_language_counts={language: len(rows) for language in languages},
        review_policy_version="rrncb_multilingual_review_v1" if translations else "",
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
        "task_count": len(tasks),
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


def _mapped_tasks(tasks: list[EvalTask], kb_id: str, mapping: dict[str, str]) -> list[EvalTask]:
    return [
        task.model_copy(
            update={
                "gold_page_ids": [mapping[task.source_document_name]],
                "gold_document_ids": [mapping[task.source_document_name]],
                "knowledge_base_ids": [kb_id],
            }
        )
        for task in tasks
    ]


def _create_or_resume_kb(
    api: _BenchmarkApi,
    suite: str,
    dataset_hash_value: str,
    ingestion_run_id: str,
    state: dict[str, Any],
) -> str:
    if state.get("knowledge_base_id"):
        return str(state["knowledge_base_id"])
    ingestion_suffix = stable_json_hash(ingestion_run_id)[:10]
    name = f"bench-{suite}-{dataset_hash_value[:12]}-{ingestion_suffix}"
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


def _timestamp_epoch(value: object) -> float | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _ingestion_item_failure_code(
    item: dict[str, Any],
    *,
    now: float,
    document_timeout: int,
    heartbeat_timeout: int,
) -> str | None:
    if item.get("job_status") != "running":
        return None
    job_started = _timestamp_epoch(item.get("job_started_at"))
    if job_started is not None and now - job_started > document_timeout:
        return "INGESTION_DOCUMENT_TIMEOUT"
    heartbeat = _timestamp_epoch(item.get("job_last_heartbeat_at"))
    if heartbeat is None or now - heartbeat > heartbeat_timeout:
        return "WORKER_HEARTBEAT_STALE"
    return None


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
    if run_id and resume_run_id:
        raise RRNcBError("RRNCB ingestion accepts either run_id or resume_run_id, not both")
    suite_paths = rrncb_paths(suite, artifacts_dir)
    manifest, _tasks = _load_tasks(suite_paths)
    source = read_json(suite_paths.source_manifest)
    documents = list(source.get("documents") or [])
    if len(documents) != 65:
        raise RRNcBError("source manifest must contain exactly 65 documents")
    requested_run_id = resume_run_id or run_id
    actual_ingestion_run_id = requested_run_id or f"{suite}-ingest-{manifest.dataset_hash[:12]}-{int(time.time())}"
    paths = _rrncb_ingestion_paths(suite_paths, actual_ingestion_run_id)
    state = read_json(paths.ingestion_state) if paths.ingestion_state.exists() else {}
    if resume_run_id and not state:
        raise RRNcBError("resume ingestion run state was not found")
    if run_id and state:
        raise RRNcBError("ingestion run already exists; use --resume-run-id")
    if requested_run_id and state.get("run_id") and state["run_id"] != requested_run_id:
        raise RRNcBError("ingestion run id does not match persisted state")
    if state.get("status") == "failed" and not rerun_failed:
        raise RRNcBError("ingestion has failed documents; rerun requires --rerun-failed")
    prepared_hash = str(manifest.metadata.get("prepared_dataset_hash") or manifest.dataset_hash)
    if state and str(state.get("prepared_dataset_hash") or state.get("dataset_hash") or "") != prepared_hash:
        raise RRNcBError("resume dataset hash does not match prepared suite")
    resolved = settings or get_settings()
    client = _BenchmarkApi(api_url, resolved, timeout=180)
    started_suite = time.perf_counter()
    try:
        try:
            _require_ingestion_ready(api_url)
        except Exception as exc:
            raise RRNcBIngestionError("READINESS_FAILED") from exc
        kb_id = _create_or_resume_kb(client, suite, manifest.dataset_hash, actual_ingestion_run_id, state)
        state.update(
            {
                "suite": suite,
                "dataset_hash": manifest.dataset_hash,
                "prepared_dataset_hash": prepared_hash,
                "started_at": state.get("started_at", utc_now_iso()),
                "knowledge_base_id": kb_id,
                "run_id": actual_ingestion_run_id,
                "ingestion_run_id": actual_ingestion_run_id,
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
                raise RRNcBIngestionError("INGESTION_SUITE_TIMEOUT")
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
                            "rrncb_ingestion_run_id": actual_ingestion_run_id,
                            "rrncb_source_filename": doc["filename"],
                        },
                        "source_ref": {
                            "schema_version": "source_ref_v1",
                            "namespace": f"eval:{suite}:{manifest.dataset_hash}",
                            "external_id": doc["filename"],
                            "source_version": f"sha256:{doc['sha256']}",
                            "attributes": {"original_system_name": doc["filename"]},
                        },
                    }
                    for doc in pending
                ]
                upload_session_created_at = utc_now_iso()
                batch_idempotency_key = stable_json_hash(
                    {
                        "suite": suite,
                        "dataset_hash": manifest.dataset_hash,
                        "ingestion_run_id": actual_ingestion_run_id,
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
                        "metadata": {
                            "rrncb_suite": suite,
                            "rrncb_dataset_hash": manifest.dataset_hash,
                            "rrncb_ingestion_run_id": actual_ingestion_run_id,
                        },
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
                                    [
                                        suite,
                                        manifest.dataset_hash,
                                        actual_ingestion_run_id,
                                        doc["filename"],
                                        doc["sha256"],
                                        "complete",
                                    ]
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
                                "complete_seconds": time.perf_counter() - complete_started,
                                "size_bytes": doc["size_bytes"],
                                "sha256": doc["sha256"],
                                "batch_index": batch_number,
                            }
                        )
                        write_json(paths.ingestion_state, state)
            last_progress_signature: dict[str, str] = {}
            deadline = time.perf_counter() + batch_timeout
            while time.perf_counter() < deadline:
                if time.perf_counter() - started_suite > suite_timeout:
                    raise RRNcBIngestionError("INGESTION_SUITE_TIMEOUT")
                try:
                    _require_ingestion_ready(api_url)
                except Exception as exc:
                    raise RRNcBIngestionError("READINESS_FAILED") from exc
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
                            "job_started_at": item.get("job_started_at"),
                            "job_last_heartbeat_at": item.get("job_last_heartbeat_at"),
                            "item_updated_at": item.get("item_updated_at"),
                            "error_code": item.get("error_code"),
                            "parser_route": (item.get("progress") or {}).get("parser_route"),
                            "parser_queue_wait_ms": (item.get("progress") or {}).get("parser_queue_wait_ms"),
                            "parser_latency_ms": (item.get("progress") or {}).get("parser_latency_ms"),
                            "chunks_published": (item.get("progress") or {}).get("chunks_published"),
                        }
                    )
                    if row.get("job_status") in {"completed", "failed", "cancelled"}:
                        job_started = _timestamp_epoch(row.get("job_started_at"))
                        if job_started is not None:
                            row["elapsed_seconds"] = round(max(0.0, time.time() - job_started), 3)
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
                    if row.get("job_status") == "running":
                        now = time.time()
                        heartbeat_limit = max(30, int(resolved.worker_job_heartbeat_seconds) * 2)
                        failure_code = _ingestion_item_failure_code(
                            row,
                            now=now,
                            document_timeout=document_timeout,
                            heartbeat_timeout=heartbeat_limit,
                        )
                        if failure_code:
                            raise RRNcBIngestionError(failure_code)
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
                raise RRNcBIngestionError("INGESTION_BATCH_TIMEOUT")
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
        state.update(
            {
                "status": "completed",
                "finished_at": utc_now_iso(),
                "dataset_hash": manifest.dataset_hash,
                "prepared_dataset_hash": prepared_hash,
            }
        )
        write_json(paths.ingestion_state, state)
        return {
            "status": "completed",
            "suite": suite,
            "knowledge_base_id": kb_id,
            "ingestion_run_id": actual_ingestion_run_id,
            "documents": 65,
            "dataset_hash": manifest.dataset_hash,
            "elapsed_seconds": round(time.perf_counter() - started_suite, 3),
        }
    except Exception as exc:
        code = exc.code if isinstance(exc, RRNcBIngestionError) else "INGESTION_FAILED"
        state.update(
            {
                "status": "failed",
                "finished_at": utc_now_iso(),
                "failure": {"code": code},
                "run_id": actual_ingestion_run_id,
                "ingestion_run_id": actual_ingestion_run_id,
            }
        )
        write_json(paths.ingestion_state, state)
        if isinstance(exc, RRNcBError):
            raise
        raise RRNcBIngestionError(code) from exc
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


def _require_ingestion_ready(api_url: str) -> dict[str, Any]:
    """Accept parser failover while keeping ingestion's authoritative dependencies strict."""

    response = httpx.get(f"{api_url.rstrip('/')}/ready", timeout=30, follow_redirects=True)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RRNcBError("API readiness payload is invalid")
    components = payload.get("components")
    if not isinstance(components, dict):
        raise RRNcBError(f"API ingestion dependencies are not ready: {payload}")
    required = ("postgres", "worker", "search_projection", "opensearch", "minio")
    failed = [name for name in required if components.get(name) != "ok"]
    parser_statuses = [components.get(name) for name in ("xberg", "docling") if name in components]
    parser_ready = not parser_statuses or any(status == "ok" for status in parser_statuses)
    if failed or not parser_ready:
        raise RRNcBError(f"API ingestion dependencies are not ready: failed={failed}, parser_ready={parser_ready}")
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
    generation_canary: dict[str, Any] = {"status": "failed", "code": "GENERATION_CANARY_NOT_RUN"}
    if smoke_tasks:
        canary = {
            "model": get_retrieval_profile(profile_name, get_settings()).model_aliases.generator_main,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Ответь строго JSON по схеме grounded_answer. Фиксированная dev-проверка: "
                        f"{smoke_tasks[0].question[:500]}"
                    ),
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "rrncb_generation_canary",
                    "strict": True,
                    "schema": ANSWER_JSON_SCHEMA["json_schema"]["schema"],
                },
            },
            "thinking": {"mode": "off", "effort": "none", "return_reasoning": False},
            "max_output_tokens": 4096,
            "stream": False,
        }
        try:
            configured_gateway = str(get_settings().model_gateway_url).rstrip("/")
            gateway_urls = [configured_gateway]
            if "model-gateway" in configured_gateway and "localhost" in api_url:
                gateway_urls.append("http://localhost:8081")
            response = None
            result: Any = None
            last_error: Exception | None = None
            for gateway_url in gateway_urls:
                try:
                    response = await asyncio.to_thread(
                        httpx.post,
                        f"{gateway_url}/v1/chat/completions",
                        json=canary,
                        timeout=120,
                    )
                    response.raise_for_status()
                    result = response.json()
                    break
                except Exception as exc:  # noqa: BLE001 - try the host-equivalent Gateway URL.
                    last_error = exc
            if response is None or result is None:
                raise last_error or RuntimeError("generation gateway is unavailable")
            if not isinstance(result, dict) or not result.get("choices"):
                raise ValueError("generation canary response has no choices")
            generation_canary = {"status": "passed", "code": None}
        except Exception as exc:  # noqa: BLE001 - persist only a stable safe code.
            safe = safe_failure_from_exception(exc, stage="generation")
            generation_canary = {"status": "failed", "code": safe.error_code}
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
        "generation_canary": generation_canary,
        "created_at": utc_now_iso(),
    }
    report["passed"] = bool(report["passed"] and generation_canary.get("status") == "passed")
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
    mapping: dict[str, Any],
    gateway_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = read_json(paths.source_manifest)
    documents = list(source.get("documents") or [])
    document_hashes = {
        filename: checksum
        for filename, checksum in sorted(
            {
                str(item.get("filename")): str(item.get("sha256"))
                for item in documents
                if isinstance(item, dict) and item.get("filename") and item.get("sha256")
            }.items()
        )
    }
    catalog_items = {
        str(item.get("id")): item
        for item in (gateway_catalog or {}).get("data", [])
        if isinstance(item, dict) and item.get("id")
    }
    model_contracts = {
        alias: {
            "provider_model": catalog_items.get(alias, {}).get("resolved_provider_model"),
            "connection_id": catalog_items.get(alias, {}).get("connection_id"),
            "adapter_hash": catalog_items.get(alias, {}).get("adapter_hash"),
        }
        for alias in config.model_aliases.values()
    }
    configuration = (gateway_catalog or {}).get("configuration") or {}
    return {
        "dataset_hash": manifest.dataset_hash,
        "knowledge_base_id": kb_id,
        "ingestion_run_id": str(ingestion_state.get("run_id") or ""),
        "mapping_hash": stable_json_hash(mapping),
        "profile": config.retrieval_profile,
        "config_hash": config.config_hash,
        "model_aliases": config.model_aliases,
        "document_hashes": document_hashes,
        "model_contracts": model_contracts,
        "active_model_revision_id": configuration.get("active_revision_id"),
        "active_model_revision_hash": configuration.get("active_revision_hash"),
    }


def _validate_or_write_run_contract(paths: RRNcBPaths, expected: dict[str, Any]) -> None:
    if paths.run_contract.exists():
        actual = read_json(paths.run_contract)
        if actual != expected:
            raise RRNcBError(
                "RRNCB run contract mismatch; create a new suite for a changed corpus or profile",
                safe_code="RUN_CONTRACT_MISMATCH",
            )
        return
    write_json(paths.run_contract, expected)


def _rrncb_selected_tasks(tasks: list[EvalTask], split: Literal["dev", "test"]) -> list[EvalTask]:
    ordered = sorted(tasks, key=lambda task: (0 if task.split == "dev" else 1, task.task_id))
    return [task for task in ordered if task.split == split]


def _rrncb_result_failure_code(result: EvalTaskResult) -> str | None:
    """Return the safe code that must terminate an immutable baseline run."""

    if result.failure_code == "MODEL_OUTPUT_INVALID":
        return "MODEL_OUTPUT_INVALID"
    if not result.server_terminal_event:
        return "TERMINAL_EVENT_MISSING"
    if result.status != "completed":
        return result.failure_code or "TASK_FAILED"
    return None


def _require_completed_dev_split(
    *,
    run_paths: RRNcBPaths,
    tasks: list[EvalTask],
    results: dict[str, EvalTaskResult],
) -> None:
    if not run_paths.status.exists():
        raise RRNcBError("RRNCB test split requires a completed dev run")
    previous_status = read_json(run_paths.status)
    dev_tasks = _rrncb_selected_tasks(tasks, "dev")
    dev_results = [results.get(task.task_id) for task in dev_tasks]
    if (
        previous_status.get("status") != "completed"
        or previous_status.get("requested_split") != "dev"
        or len(dev_results) != len(dev_tasks)
        or any(result is None or _rrncb_result_failure_code(result) for result in dev_results)
    ):
        raise RRNcBError("RRNCB test split requires a successful completed dev run")


async def run_rrncb(
    *,
    suite: str = RRNCB_SUITE,
    api_url: str = "http://localhost:8000",
    profile_name: str = "upload_sota_mvp",
    batch_size: int = 1,
    question_timeout: int = 300,
    suite_timeout: int = 28800,
    resume: bool = True,
    rerun_failed: bool = False,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    ingestion_run_id: str | None = None,
    split: Literal["dev", "test"] = "dev",
    artifacts_dir: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if batch_size != 1:
        raise RRNcBError("RRNCB baseline requires --batch-size 1 for fail-fast execution")
    if run_id and resume_run_id:
        raise RRNcBError("RRNCB accepts either run_id or resume_run_id, not both")
    if split not in {"dev", "test"}:
        raise RRNcBError(f"unsupported RRNCB split: {split}")
    paths = rrncb_paths(suite, artifacts_dir)
    manifest, prepared_tasks = _load_tasks(paths)
    requested_run_id = resume_run_id or run_id
    if resume_run_id and not (paths.base / "runs" / resume_run_id).exists():
        raise RRNcBError("resume run artifacts were not found for the suite")
    actual_run_id = requested_run_id or f"{suite}-{manifest.dataset_hash[:12]}-{int(time.time())}"
    run_paths = _rrncb_run_paths(paths, actual_run_id)
    persisted_contract = read_json(run_paths.run_contract) if run_paths.run_contract.exists() else {}
    persisted_ingestion_id = str(persisted_contract.get("ingestion_run_id") or "")
    if ingestion_run_id and persisted_ingestion_id and ingestion_run_id != persisted_ingestion_id:
        raise RRNcBError("RRNCB run contract mismatch; ingestion run changed")
    selected_ingestion_id = ingestion_run_id or persisted_ingestion_id
    if not selected_ingestion_id:
        raise RRNcBError("RRNCB run requires --ingestion-run-id")
    ingestion_paths = _rrncb_ingestion_paths(paths, selected_ingestion_id)
    if not ingestion_paths.ingestion_state.exists() or not ingestion_paths.mapping.exists():
        raise RRNcBError("completed ingestion artifacts were not found")
    state = read_json(ingestion_paths.ingestion_state)
    kb_id = str(state.get("knowledge_base_id") or "")
    if not kb_id or state.get("status") != "completed":
        raise RRNcBError("ingestion must complete before running RRNCB RAG")
    mapping_payload = read_json(ingestion_paths.mapping)
    mapping_records = dict(mapping_payload.get("documents") or {})
    mapping = {
        str(filename): str(record.get("document_id") or "")
        for filename, record in mapping_records.items()
        if isinstance(record, dict) and record.get("document_id")
    }
    if len(mapping) != 65:
        raise RRNcBError("ingestion mapping must contain 65 documents")
    tasks = _mapped_tasks(prepared_tasks, kb_id, mapping)
    resolved = settings or get_settings()
    config = _rrncb_config(resolved, profile_name)
    try:
        ready_payload = _require_ready(api_url)
    except Exception as exc:
        write_json(
            run_paths.status,
            {
                "status": "failed",
                "run_id": actual_run_id,
                "suite": suite,
                "failure": {"code": "READINESS_FAILED"},
                "updated_at": utc_now_iso(),
            },
        )
        raise RRNcBError("READINESS_FAILED", safe_code="READINESS_FAILED") from exc
    # The API owns the 300-second operation deadline.  Keep the client read
    # timeout slightly above it so a terminal SSE event is not cut off by a
    # second independent timer.
    client = HttpEvalApiClient.from_settings(resolved, timeout=question_timeout + 15)
    try:
        gateway_catalog_response = await asyncio.to_thread(
            httpx.get, f"{api_url.rstrip('/')}/v1/models", timeout=30, follow_redirects=True
        )
        gateway_catalog_response.raise_for_status()
        gateway_catalog = gateway_catalog_response.json()
        if not isinstance(gateway_catalog, dict):
            gateway_catalog = {}
    except Exception:
        gateway_catalog = {}
    preflight_path = run_paths.base / "preflight.json"
    preflight = read_json(preflight_path) if preflight_path.exists() else {}
    try:
        if not (
            bool(preflight.get("passed"))
            and preflight.get("knowledge_base_id") == kb_id
            and preflight.get("profile") == profile_name
            and preflight.get("config_hash") == config.config_hash
            and preflight.get("dataset_hash") == manifest.dataset_hash
        ):
            await _rrncb_preflight(
                paths=run_paths,
                tasks=tasks,
                kb_id=kb_id,
                profile_name=profile_name,
                client=client,
                api_url=api_url,
                config_hash=config.config_hash,
                dataset_hash_value=manifest.dataset_hash,
            )
            preflight = read_json(preflight_path)
    except Exception as exc:
        write_json(
            run_paths.status,
            {
                "status": "failed",
                "run_id": actual_run_id,
                "suite": suite,
                "failure": {"code": "READINESS_FAILED"},
                "updated_at": utc_now_iso(),
            },
        )
        client.close()

        raise RRNcBError("READINESS_FAILED", safe_code="READINESS_FAILED") from exc
    run_contract = _rrncb_run_contract(
        paths=run_paths,
        manifest=manifest,
        ingestion_state=state,
        kb_id=kb_id,
        config=config,
        mapping=mapping_payload,
        gateway_catalog=gateway_catalog,
    )
    run_contract["run_id"] = actual_run_id
    run_contract["index_contract_ids"] = list(preflight.get("index_contract_ids") or [])
    run_contract["retrieval_profile_hash"] = stable_json_hash(
        get_retrieval_profile(profile_name, resolved).model_dump(mode="json")
    )
    try:
        _validate_or_write_run_contract(run_paths, run_contract)
    except RRNcBError:
        write_json(
            run_paths.status,
            {
                "status": "failed",
                "run_id": actual_run_id,
                "suite": suite,
                "failure": {"code": "RUN_CONTRACT_MISMATCH"},
                "updated_at": utc_now_iso(),
            },
        )
        client.close()
        raise
    old_results = read_jsonl(run_paths.results, EvalTaskResult) if resume and run_paths.results.exists() else []
    incompatible = [
        result
        for result in old_results
        if result.dataset_hash
        and (result.dataset_hash != manifest.dataset_hash or result.config_hash != config.config_hash)
    ]
    if incompatible:
        client.close()
        raise RRNcBError("existing RRNCB results have a different dataset or profile contract")
    existing = {result.task_id: result for result in old_results if result.status == "completed" or not rerun_failed}
    selected_tasks = _rrncb_selected_tasks(tasks, split)
    if split == "test":
        if not resume_run_id:
            client.close()
            raise RRNcBError("RRNCB test split requires --resume-run-id from the dev run")
        try:
            _require_completed_dev_split(run_paths=run_paths, tasks=tasks, results=existing)
        except Exception:
            client.close()
            raise
    cold_start_ids = {task.task_id for task in _rrncb_selected_tasks(tasks, "dev")[:5]}
    started = time.perf_counter()
    selected_ids = {task.task_id for task in selected_tasks}
    existing_selected = {task_id: result for task_id, result in existing.items() if task_id in selected_ids}
    completed_count = sum(result.status == "completed" for result in existing_selected.values())
    status: dict[str, Any] = {
        "status": "running",
        "run_id": actual_run_id,
        "suite": suite,
        "dataset_hash": manifest.dataset_hash,
        "knowledge_base_id": kb_id,
        "ingestion_run_id": selected_ingestion_id,
        "total": len(selected_tasks),
        "completed": completed_count,
        "failed": sum(1 for result in existing_selected.values() if result.status != "completed"),
        "requested_split": split,
        "selected_task_count": len(selected_tasks),
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
                eval_run_id=actual_run_id,
                request_namespace=actual_run_id,
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
    pending_tasks = [task for task in selected_tasks if task.task_id not in existing]
    failure: dict[str, str] | None = None
    for result in existing_selected.values():
        if code := _rrncb_result_failure_code(result):
            failure = {"code": code, "task_id": result.task_id, "stage": "existing_result"}
            break
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        for offset, task in enumerate(pending_tasks, start=1):
            if failure:
                break
            if time.perf_counter() - started > suite_timeout:
                failure = {"code": "SUITE_TIMEOUT", "task_id": task.task_id, "stage": "suite"}
                break
            result = await run_one(task)
            append_jsonl(run_paths.results, result)
            existing[result.task_id] = result
            if result.status == "completed":
                completed_count += 1
            else:
                status["failed"] += 1
            status["completed"] = completed_count
            status["updated_at"] = utc_now_iso()
            status["last_batch"] = offset
            write_json(run_paths.status, status)
            print(
                json.dumps(
                    {
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                        "processed": completed_count + int(status["failed"]),
                        "total": len(selected_tasks),
                        "failed": status["failed"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if code := _rrncb_result_failure_code(result):
                failure = {"code": code, "task_id": result.task_id, "stage": "task"}
                break
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
            "execution": {
                "requested_split": split,
                "selected_task_count": len(selected_tasks),
                "completed_selected": sum(
                    result.status == "completed" for task_id, result in existing.items() if task_id in selected_ids
                ),
                "failed_selected": sum(
                    result.status != "completed" for task_id, result in existing.items() if task_id in selected_ids
                ),
                "status": "failed" if failure else "completed",
                "failure": failure,
                "fail_fast": True,
            },
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
                "status": report["execution"]["status"],
                "finished_at": utc_now_iso(),
                "report": str(run_paths.report),
                "report_markdown": str(run_paths.report_markdown),
                "results_csv": str(run_paths.results_csv),
                "failure": failure,
            }
        )
        write_json(run_paths.status, status)
        if failure:
            raise RRNcBError(
                f"RRNCB baseline stopped: {failure['code']}",
                safe_code=failure["code"],
            )
        return report
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        client.close()


def _rrncb_base_task_id(task: EvalTask) -> str:
    tag = next((item for item in task.tags if item.startswith("base_task:")), "")
    return tag.removeprefix("base_task:") if tag else task.task_id.removesuffix(f"-{task.language_group}")


def _retrieval_metric(result: RetrievalTaskResult, name: str) -> float:
    scores = result.scores
    if scores is None:
        return 0.0
    if name == "recall_at_10":
        return float(scores.document_recall.get("10", 0.0))
    if name == "mrr_at_10":
        return float(scores.document_mrr_at_10)
    if name == "ndcg_at_10":
        return float(scores.document_ndcg_at_10)
    raise ValueError(f"unsupported RRNCB retrieval metric: {name}")


def _rrncb_multilingual_retrieval_report(tasks: list[EvalTask], results: list[RetrievalTaskResult]) -> dict[str, Any]:
    task_by_id = {task.task_id: task for task in tasks}
    latest = {result.task_id: result for result in results if result.task_id in task_by_id}
    completed = [result for result in latest.values() if result.status == "completed" and result.scores is not None]

    def metrics(rows: list[RetrievalTaskResult]) -> dict[str, float]:
        latency = [float(row.latency_ms.get("total") or 0) for row in rows]
        return {
            "task_count": float(len(rows)),
            "recall_at_10": sum(_retrieval_metric(row, "recall_at_10") for row in rows) / len(rows) if rows else 0.0,
            "mrr_at_10": sum(_retrieval_metric(row, "mrr_at_10") for row in rows) / len(rows) if rows else 0.0,
            "ndcg_at_10": sum(_retrieval_metric(row, "ndcg_at_10") for row in rows) / len(rows) if rows else 0.0,
            "latency_p50_ms": percentile(latency, 50) if latency else 0.0,
            "latency_p95_ms": percentile(latency, 95) if latency else 0.0,
        }

    by_language = {
        language: metrics([row for row in completed if task_by_id[row.task_id].language_group == language])
        for language in RRNCB_MULTILINGUAL_LANGUAGES
    }
    by_base_language = {
        (_rrncb_base_task_id(task_by_id[row.task_id]), task_by_id[row.task_id].language_group): row for row in completed
    }
    paired_delta: dict[str, dict[str, float]] = {}
    for language in RRNCB_MULTILINGUAL_LANGUAGES[1:]:
        pairs = [
            (by_base_language[(base_id, "ru")], by_base_language[(base_id, language)])
            for base_id in sorted({_rrncb_base_task_id(task) for task in tasks})
            if (base_id, "ru") in by_base_language and (base_id, language) in by_base_language
        ]
        paired_delta[language] = {
            "pair_count": float(len(pairs)),
            "recall_at_10_delta": sum(
                _retrieval_metric(translated, "recall_at_10") - _retrieval_metric(russian, "recall_at_10")
                for russian, translated in pairs
            )
            / len(pairs)
            if pairs
            else 0.0,
            "mrr_at_10_delta": sum(
                _retrieval_metric(translated, "mrr_at_10") - _retrieval_metric(russian, "mrr_at_10")
                for russian, translated in pairs
            )
            / len(pairs)
            if pairs
            else 0.0,
        }
    comparison_keys = sorted({row.comparison_key for row in completed if row.comparison_key})
    return {
        "task_count": len(tasks),
        "completed": len(completed),
        "failed": len(tasks) - len(completed),
        "metrics": metrics(completed),
        "by_language": by_language,
        "paired_delta_vs_ru": paired_delta,
        "comparison_keys": comparison_keys,
        "compatible": len(comparison_keys) <= 1,
    }


async def run_rrncb_retrieval(
    *,
    suite: str,
    ingestion_run_id: str,
    split: Literal["dev", "test"],
    api_url: str = "http://localhost:8000",
    profile_name: str = "upload_sota_mvp",
    batch_size: int = 5,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    rerun_failed: bool = False,
    artifacts_dir: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the immutable RRNCB retrieval-only baseline without generation coupling."""

    if run_id and resume_run_id:
        raise RRNcBError("RRNCB retrieval accepts either run_id or resume_run_id, not both")
    if batch_size < 1:
        raise RRNcBError("RRNCB retrieval batch size must be positive")
    if split == "test" and not resume_run_id:
        raise RRNcBError("RRNCB retrieval test requires --resume-run-id from dev")
    suite_paths = rrncb_paths(suite, artifacts_dir)
    manifest, prepared_tasks = _load_tasks(suite_paths)
    ingestion_paths = _rrncb_ingestion_paths(suite_paths, ingestion_run_id)
    if not ingestion_paths.ingestion_state.exists() or not ingestion_paths.mapping.exists():
        raise RRNcBError("completed ingestion artifacts were not found")
    ingestion_state = read_json(ingestion_paths.ingestion_state)
    if ingestion_state.get("status") != "completed":
        raise RRNcBError("ingestion must complete before retrieval evaluation")
    kb_id = str(ingestion_state.get("knowledge_base_id") or "")
    mapping_payload = read_json(ingestion_paths.mapping)
    mapping_records = dict(mapping_payload.get("documents") or {})
    mapping = {
        filename: str(record.get("document_id") or "")
        for filename, record in mapping_records.items()
        if isinstance(record, dict) and record.get("document_id")
    }
    if len(mapping) != 65:
        raise RRNcBError("ingestion mapping must contain 65 documents")
    tasks = _mapped_tasks(prepared_tasks, kb_id, mapping)
    selected = _rrncb_selected_tasks(tasks, split)
    actual_run_id = resume_run_id or run_id or f"{suite}-retrieval-{manifest.dataset_hash[:12]}"
    run_paths = _rrncb_run_paths(suite_paths, actual_run_id)
    run_paths.base.mkdir(parents=True, exist_ok=True)
    results_path = run_paths.base / "retrieval.jsonl"
    status_path = run_paths.base / "retrieval-status.json"
    report_path = run_paths.base / "retrieval-report.json"
    dev_marker = run_paths.base / "retrieval-dev.completed.json"
    resolved = settings or get_settings()
    _require_ready(api_url)
    config = _rrncb_config(resolved, profile_name)
    contract = _rrncb_run_contract(
        paths=suite_paths,
        manifest=manifest,
        ingestion_state=ingestion_state,
        kb_id=kb_id,
        config=config,
        mapping=mapping_payload,
    )
    contract["retrieval_only"] = True
    _validate_or_write_run_contract(run_paths, contract)
    if split == "test":
        if not dev_marker.exists():
            raise RRNcBError("RRNCB retrieval test requires a completed dev marker")
        marker = read_json(dev_marker)
        if marker.get("dataset_hash") != manifest.dataset_hash or marker.get("config_hash") != config.config_hash:
            raise RRNcBError("RRNCB retrieval dev/test contract mismatch")
    existing_rows = read_jsonl(results_path, RetrievalTaskResult) if results_path.exists() else []
    existing = {row.task_id: row for row in existing_rows}
    client = HttpEvalApiClient.from_settings(resolved, include_kiwix_urls=False)
    started = time.perf_counter()
    try:
        for index, task in enumerate(selected, start=1):
            previous = existing.get(task.task_id)
            if previous is not None and (previous.status == "completed" or not rerun_failed):
                continue
            result = await run_retrieval_task(
                task,
                config,
                api=api_url,
                manifest=manifest,
                client=client,
                settings=resolved,
                batch_index=(index - 1) // batch_size + 1,
                task_index=index,
            )
            append_jsonl(results_path, result)
            existing[task.task_id] = result
            write_json(
                status_path,
                {
                    "status": "running",
                    "run_id": actual_run_id,
                    "requested_split": split,
                    "processed": sum(task_item.task_id in existing for task_item in selected),
                    "total": len(selected),
                    "current_task_id": task.task_id,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "updated_at": utc_now_iso(),
                },
            )
        evaluated_tasks = [task for task in tasks if task.task_id in existing]
        latest_results = [existing[task.task_id] for task in evaluated_tasks]
        selected_results = [existing.get(task.task_id) for task in selected]
        report = _rrncb_multilingual_retrieval_report(evaluated_tasks, latest_results)
        report["full_suite_task_count"] = len(tasks)
        complete = all(row is not None and row.status == "completed" for row in selected_results)
        if not complete or not report["compatible"]:
            raise RRNcBError("RRNCB retrieval split has failed or incompatible results")
        if split == "dev":
            write_json(
                dev_marker,
                {
                    "dataset_hash": manifest.dataset_hash,
                    "config_hash": config.config_hash,
                    "completed_at": utc_now_iso(),
                },
            )
        write_json(report_path, report)
        write_json(
            status_path,
            {
                "status": "completed",
                "run_id": actual_run_id,
                "requested_split": split,
                "processed": len(selected),
                "total": len(selected),
                "report_path": str(report_path),
                "updated_at": utc_now_iso(),
            },
        )
        return {"run_id": actual_run_id, "status_path": str(status_path), "report_path": str(report_path), **report}
    finally:
        client.close()


def rrncb_status(*, suite: str = RRNCB_SUITE, artifacts_dir: Path | None = None) -> dict[str, Any]:
    paths = rrncb_paths(suite, artifacts_dir)
    run_dirs = sorted((paths.base / "runs").glob("*/latest-status.json"), key=lambda item: item.stat().st_mtime)
    if run_dirs:
        return read_json(run_dirs[-1])
    if not paths.status.exists():
        raise FileNotFoundError(f"no RRNCB status found for suite {suite}")
    return read_json(paths.status)
