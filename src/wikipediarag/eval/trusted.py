from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TextIO

import httpx
from pydantic import BaseModel, Field

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    append_jsonl,
    dataset_paths,
    read_json,
    read_jsonl,
    utc_now_iso,
    write_json,
    write_json_atomic,
    write_jsonl,
)
from wikipediarag.eval.corpus import CorpusChunk, CorpusSnapshot, load_alias_chunks, load_candidate_chunks
from wikipediarag.eval.corpus import load_corpus_snapshot as default_load_corpus_snapshot
from wikipediarag.eval.generate_runs import resolve_chat_model_alias, resolve_generate_concurrency
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.retrieval_runner import RetrievalProgressCallback
from wikipediarag.eval.schemas import (
    EvalDatasetManifest,
    EvalGenerateModelRef,
    EvalTask,
    GoldEvidence,
    TaskFamily,
)
from wikipediarag.eval.settings import adapt_eval_settings
from wikipediarag.model_client import chat_completion
from wikipediarag.retrieval_profile import get_retrieval_profile

TRUSTED_DATASET_NAME = "trusted-wikipedia-v2"
TRUSTED_DATASET_VERSION = "2026.07.1"
TRUSTED_RUNS_ROOT = ARTIFACT_ROOT / "trusted-runs"
TRUSTED_REPORTS_ROOT = ARTIFACT_ROOT / "trusted-reports"
TRUSTED_CATALOG_ROOT = ARTIFACT_ROOT / "trusted-catalog"
LATEST_TRUSTED_RUN = TRUSTED_RUNS_ROOT / "latest.json"
MAX_ATTEMPTS_MULTIPLIER = 8
TRUSTED_PROVIDER_RETRY_LIMIT = 2
TRUSTED_RETRY_BASE_DELAY_SECONDS = 1.0
DEFAULT_TRUSTED_REJECTION_BUDGET = 30

TrustedFamily = Literal[
    "single_hop_prose",
    "deep_section_fact",
    "redirect_alias_rare",
    "structured_fact",
    "bridge_multi_hop",
    "comparison_multi_hop",
    "unanswerable",
    "hard_negative",
]
TrustedGenerateState = Literal["running", "completed", "failed"]
TrustedGeneratePhase = Literal[
    "preparing",
    "catalog",
    "family_generation",
    "writing_dataset",
    "completed",
    "failed",
]
TrustedReviewStatus = Literal["unreviewed"]
TrustedSplit = Literal["train"]
AnswerType = Literal["span", "number", "date", "yes_no", "list", "comparison", "unanswerable"]

TRUSTED_FAMILY_WEIGHTS: dict[TrustedFamily, float] = {
    "single_hop_prose": 90.0,
    "deep_section_fact": 40.0,
    "redirect_alias_rare": 35.0,
    "structured_fact": 35.0,
    "bridge_multi_hop": 25.0,
    "comparison_multi_hop": 25.0,
    "unanswerable": 25.0,
    "hard_negative": 25.0,
}
TRUSTED_FAMILY_ORDER: tuple[TrustedFamily, ...] = tuple(TRUSTED_FAMILY_WEIGHTS)


class TrustedSourceSpan(BaseModel):
    span_id: str
    document_id: str
    section_id: str
    chunk_id: str
    title: str
    source_url: str
    section_path: list[str]
    structural_element: str
    text: str
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    sentence: str = ""


class TrustedNegativeCandidate(BaseModel):
    document_id: str
    chunk_id: str = ""
    title: str = ""
    reason: str
    source: str
    contains_answer: bool = False


class TrustedVerificationResult(BaseModel):
    check: str
    passed: bool
    details: str = ""


class TrustedProvenance(BaseModel):
    source: str = "local_zim"
    zim_checksum: str
    snapshot_id: str
    index_version: str
    retrieval_profile_hash: str
    parser_version: str = "zim_libzim_html_v1"
    chunker_version: str = "sota_mvp_parent_child_v1"
    generator_version: str = TRUSTED_DATASET_VERSION
    verifier_version: str = "local_deterministic_v1"


class TrustedEvalTask(EvalTask):
    trusted_family: TrustedFamily
    source_spans: list[TrustedSourceSpan] = Field(default_factory=list)
    structural_element: str = "prose"
    answer_type: AnswerType = "span"
    verification_results: list[TrustedVerificationResult] = Field(default_factory=list)
    negative_candidates: list[TrustedNegativeCandidate] = Field(default_factory=list)
    provenance: TrustedProvenance
    split: TrustedSplit = "train"
    review_status: TrustedReviewStatus = "unreviewed"


class TrustedCatalogItem(BaseModel):
    chunk_id: str
    document_id: str
    section_id: str
    title: str
    source_url: str
    section_path: list[str]
    content: str
    structural_element: str
    zim_entry_path: str = ""
    alias: str = ""
    redirect_target: str = ""
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TrustedCatalogManifest(BaseModel):
    catalog_name: str = "trusted-wikipedia-catalog"
    created_at: str
    snapshot_id: str
    index_version: str
    zim_checksum: str
    retrieval_profile_hash: str
    item_count: int
    by_structural_element: dict[str, int]
    jsonl_path: str


class TrustedGenerateRuntimeConfig(BaseModel):
    run_id: str
    count: int = Field(ge=1)
    concurrency: int = Field(ge=1, le=128)
    rejection_budget: int = Field(default=DEFAULT_TRUSTED_REJECTION_BUDGET, ge=1)
    generator: EvalGenerateModelRef
    verifier: EvalGenerateModelRef
    family_weights: dict[TrustedFamily, float]
    family_targets: dict[TrustedFamily, int]


class TrustedGenerateStats(BaseModel):
    accepted: int = 0
    rejected: int = 0
    errors: int = 0
    retries: int = 0
    family_accepted: dict[TrustedFamily, int] = Field(default_factory=dict)
    family_targets: dict[TrustedFamily, int] = Field(default_factory=dict)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    family_rejection_reasons: dict[TrustedFamily, dict[str, int]] = Field(default_factory=dict)


class TrustedGenerateRunStatus(BaseModel):
    run_id: str
    state: TrustedGenerateState
    phase: TrustedGeneratePhase
    started_at: str
    updated_at: str
    count_target: int
    active_family: TrustedFamily | None = None
    current_attempt: int | None = Field(default=None, ge=1)
    config: TrustedGenerateRuntimeConfig
    snapshot_id: str
    index_version: str
    zim_checksum: str
    retrieval_profile_hash: str
    stats: TrustedGenerateStats
    family_attempts_started: dict[TrustedFamily, int] = Field(default_factory=dict)
    dataset_name: str = TRUSTED_DATASET_NAME
    dataset_hash: str = ""
    manifest_path: str = ""
    error_message: str = ""


class TrustedRunPaths(BaseModel):
    run_id: str
    base: Path
    status: Path
    events: Path
    accepted_partial: Path
    rejected: Path
    lock: Path

    model_config = {"arbitrary_types_allowed": True}


type TrustedProgressCallback = Callable[[TrustedGenerateRunStatus, str, dict[str, Any]], None]


def trusted_run_paths(run_id: str) -> TrustedRunPaths:
    base = TRUSTED_RUNS_ROOT / run_id
    return TrustedRunPaths(
        run_id=run_id,
        base=base,
        status=base / "status.json",
        events=base / "events.jsonl",
        accepted_partial=base / "accepted.partial.jsonl",
        rejected=base / "rejected.jsonl",
        lock=base / "run.lock",
    )


def trusted_dataset_hash(tasks: list[TrustedEvalTask]) -> str:
    return stable_json_hash([task.model_dump(mode="json") for task in tasks])


def trusted_family_targets(count: int, weights: dict[TrustedFamily, float] | None = None) -> dict[TrustedFamily, int]:
    if count < 1:
        raise ValueError("eval-trusted-generate count must be >= 1")
    source = weights or TRUSTED_FAMILY_WEIGHTS
    normalized = {family: max(0.0, float(source.get(family, 0.0))) for family in TRUSTED_FAMILY_ORDER}
    if all(weight == 0.0 for weight in normalized.values()):
        raise ValueError("at least one trusted family weight must be > 0")
    total = sum(normalized.values())
    raw = {family: count * normalized[family] / total for family in TRUSTED_FAMILY_ORDER}
    targets = {family: int(raw[family]) for family in TRUSTED_FAMILY_ORDER}
    while sum(targets.values()) < count:
        family = max(
            TRUSTED_FAMILY_ORDER,
            key=lambda item: (raw[item] - targets[item], -TRUSTED_FAMILY_ORDER.index(item)),
        )
        targets[family] += 1
    return targets


def parse_trusted_family_weight_specs(specs: list[str]) -> dict[TrustedFamily, float] | None:
    if not specs:
        return None
    known = set(TRUSTED_FAMILY_ORDER)
    weights: dict[TrustedFamily, float] = {family: 0.0 for family in TRUSTED_FAMILY_ORDER}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"invalid --family-weight value: {spec}")
        family, raw_weight = spec.split("=", 1)
        name = family.strip()
        if name not in known:
            raise ValueError(f"unknown trusted family in --family-weight: {name}")
        weight = float(raw_weight.strip())
        if weight < 0:
            raise ValueError(f"family weight must be >= 0 for {name}")
        weights[name] = weight
    return weights


async def write_trusted_catalog(*, settings: Settings | None = None) -> TrustedCatalogManifest:
    resolved = adapt_eval_settings(settings or get_settings())
    snapshot = await default_load_corpus_snapshot(resolved)
    chunks = await load_candidate_chunks(limit=5000, settings=resolved)
    aliases = await load_alias_chunks(limit=1000, settings=resolved)
    catalog = _catalog_items(chunks, aliases)
    by_structural = Counter(item.structural_element for item in catalog)
    # Index versions include colon-delimited provenance and cannot be used verbatim in Windows paths.
    catalog_key = stable_json_hash({"snapshot_id": snapshot.snapshot_id, "index_version": snapshot.index_version})[:16]
    jsonl_path = TRUSTED_CATALOG_ROOT / f"catalog-{snapshot.snapshot_id}-{catalog_key}.jsonl"
    manifest = TrustedCatalogManifest(
        created_at=utc_now_iso(),
        snapshot_id=snapshot.snapshot_id,
        index_version=snapshot.index_version,
        zim_checksum=snapshot.zim_checksum,
        retrieval_profile_hash=snapshot.retrieval_profile_hash,
        item_count=len(catalog),
        by_structural_element=dict(sorted(by_structural.items())),
        jsonl_path=str(jsonl_path),
    )
    write_jsonl(jsonl_path, catalog)
    write_json(jsonl_path.with_suffix(".manifest.json"), manifest.model_dump(mode="json"))
    write_json(TRUSTED_CATALOG_ROOT / "latest.json", manifest.model_dump(mode="json"))
    return manifest


async def generate_trusted_dataset(
    *,
    count: int | None = None,
    concurrency: int | None = None,
    rejection_budget: int = DEFAULT_TRUSTED_REJECTION_BUDGET,
    generator_alias: str | None = None,
    verifier_alias: str | None = None,
    family_weights: dict[TrustedFamily, float] | None = None,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    takeover_stale_run: bool = False,
    settings: Settings | None = None,
    progress_callback: TrustedProgressCallback | None = None,
) -> EvalDatasetManifest:
    resolved = adapt_eval_settings(settings or get_settings())
    snapshot = await default_load_corpus_snapshot(resolved)
    lock: _TrustedRunLock | None = None
    if resume_run_id is not None:
        status = load_trusted_status(resume_run_id)
        requested_count = status.config.count if count is None else count
        runtime = _resolve_resume_runtime(
            status,
            snapshot,
            count=requested_count,
            concurrency=concurrency,
            rejection_budget=rejection_budget,
            generator_alias=generator_alias,
            verifier_alias=verifier_alias,
            family_weights=family_weights,
            settings=resolved,
        )
        lock = _TrustedRunLock.acquire(runtime.run_id, takeover_stale=takeover_stale_run)
        tasks = load_trusted_partial_tasks(runtime.run_id)
        tracker = _TrustedTracker.from_status(runtime, snapshot, status, tasks, callback=progress_callback)
    else:
        requested_count = 300 if count is None else count
        runtime = _resolve_trusted_runtime(
            snapshot,
            count=requested_count,
            concurrency=concurrency,
            rejection_budget=rejection_budget,
            generator_alias=generator_alias,
            verifier_alias=verifier_alias,
            family_weights=family_weights,
            run_id=run_id,
            settings=resolved,
        )
        paths = trusted_run_paths(runtime.run_id)
        if paths.status.exists() or paths.accepted_partial.exists() or paths.rejected.exists() or paths.events.exists():
            raise FileExistsError(f"trusted generate run directory already exists for {runtime.run_id}")
        lock = _TrustedRunLock.acquire(runtime.run_id, takeover_stale=takeover_stale_run)
        tasks = []
        tracker = _TrustedTracker(runtime, snapshot, callback=progress_callback)

    try:
        await tracker.emit("run_started", phase="preparing")
        await tracker.emit("catalog_started", phase="catalog")
        chunks = await load_candidate_chunks(limit=max(1800, runtime.count * 12), settings=resolved)
        aliases = await load_alias_chunks(limit=max(500, runtime.count * 4), settings=resolved)
        pools = _build_pools(_catalog_items(chunks, aliases))
        await tracker.emit(
            "catalog_completed",
            phase="catalog",
            payload={"pool_sizes": {key: len(value) for key, value in pools.items()}},
        )
        seen_questions = {task.question for task in tasks}
        for family in TRUSTED_FAMILY_ORDER:
            target = runtime.family_targets.get(family, 0)
            if target <= tracker.family_accepted.get(family, 0):
                continue
            await tracker.emit("family_started", phase="family_generation", family=family)
            await _generate_trusted_family(
                family,
                target,
                pools,
                tasks,
                seen_questions,
                snapshot,
                runtime,
                resolved,
                tracker,
            )
            await tracker.emit("family_completed", phase="family_generation", family=family)
        await tracker.emit("dataset_writing", phase="writing_dataset")
        if len(tasks) != runtime.count:
            raise RuntimeError(f"trusted dataset accepted {len(tasks)}/{runtime.count} tasks before publication")
        _validate_trusted_tasks(tasks)
        tasks.sort(
            key=lambda item: (
                TRUSTED_FAMILY_ORDER.index(item.trusted_family),
                item.generation_seed,
                item.question,
            )
        )
        for index, task in enumerate(tasks, start=1):
            task.task_id = f"trusted-wiki-{index:06d}"
        digest = trusted_dataset_hash(tasks)
        dataset_base = ARTIFACT_ROOT / "datasets" / TRUSTED_DATASET_NAME
        jsonl_path = dataset_base / f"{TRUSTED_DATASET_NAME}-{snapshot.snapshot_id}-{digest[:12]}.jsonl"
        manifest = EvalDatasetManifest(
            dataset_name=TRUSTED_DATASET_NAME,
            dataset_version=TRUSTED_DATASET_VERSION,
            dataset_hash=digest,
            task_count=len(tasks),
            created_at=utc_now_iso(),
            snapshot_id=snapshot.snapshot_id,
            index_version=snapshot.index_version,
            zim_checksum=snapshot.zim_checksum,
            retrieval_profile_hash=snapshot.retrieval_profile_hash,
            generator_alias=runtime.generator.alias,
            verifier_alias=runtime.verifier.alias,
            jsonl_path=str(jsonl_path),
        )
        _write_trusted_dataset(tasks, manifest, runtime)
        await tracker.completed(manifest)
        return manifest
    except Exception as exc:
        await tracker.failed(str(exc))
        raise
    finally:
        if lock is not None:
            lock.release()


async def _generate_trusted_family(
    family: TrustedFamily,
    target: int,
    pools: dict[TrustedFamily, list[Any]],
    tasks: list[TrustedEvalTask],
    seen_questions: set[str],
    snapshot: CorpusSnapshot,
    runtime: TrustedGenerateRuntimeConfig,
    settings: Settings,
    tracker: _TrustedTracker,
) -> None:
    pool = pools.get(family, [])
    if not pool:
        raise RuntimeError(f"no corpus candidates for trusted family {family}")
    attempts_started = tracker.family_attempts_started.get(family, 0)
    pending: dict[asyncio.Task[TrustedEvalTask], int] = {}
    try:
        while tracker.family_accepted.get(family, 0) < target and (not tracker.rejection_budget_exhausted() or pending):
            while (
                not tracker.rejection_budget_exhausted()
                and tracker.family_accepted.get(family, 0) + len(pending) < target
                and len(pending) < runtime.concurrency
                and len(pending) < tracker.rejection_budget_remaining()
            ):
                attempts_started += 1
                tracker.family_attempts_started[family] = attempts_started
                await tracker.emit(
                    "attempt_started",
                    phase="family_generation",
                    family=family,
                    attempt=attempts_started,
                )
                packet = _select_packet(family, pool, attempts_started)
                future = asyncio.create_task(
                    _build_trusted_task(family, packet, attempts_started, snapshot, runtime, settings, tracker)
                )
                pending[future] = attempts_started
            if not pending:
                break
            done, _pending = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                attempt = pending.pop(finished)
                try:
                    generated_task = await finished
                except json.JSONDecodeError:
                    await tracker.reject(family, attempt, reason="invalid_generator_json")
                    continue
                except Exception:
                    tracker.errors += 1
                    await tracker.reject(family, attempt, reason="provider_error")
                    continue
                rejection_reason = _trusted_task_rejection_reason(generated_task, seen_questions)
                if rejection_reason:
                    await tracker.reject(
                        family,
                        attempt,
                        reason=rejection_reason,
                        question=generated_task.question,
                    )
                    continue
                tasks.append(generated_task)
                seen_questions.add(generated_task.question)
                await tracker.accept(family, attempt, generated_task)
    finally:
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending.keys(), return_exceptions=True)
    if tracker.family_accepted.get(family, 0) != target:
        if tracker.rejection_budget_exhausted():
            raise RuntimeError(
                "rejection_budget_exhausted: "
                f"accepted={tracker.accepted}/{runtime.count} rejected={tracker.rejected}/{runtime.rejection_budget} "
                f"family={family} family_accepted={tracker.family_accepted.get(family, 0)}/{target} "
                f"reasons={tracker.rejection_reasons}"
            )
        raise RuntimeError(f"could generate only {tracker.family_accepted.get(family, 0)}/{target} tasks for {family}")


async def _build_trusted_task(
    family: TrustedFamily,
    packet: list[TrustedCatalogItem],
    attempt: int,
    snapshot: CorpusSnapshot,
    runtime: TrustedGenerateRuntimeConfig,
    settings: Settings,
    tracker: _TrustedTracker,
) -> TrustedEvalTask:
    seed = int(stable_json_hash([family, attempt, snapshot.index_version], 16)[:8], 16)
    candidate = await _trusted_candidate(family, packet, runtime.generator.alias, settings, tracker, attempt)
    await tracker.emit(
        "candidate_generated",
        phase="family_generation",
        family=family,
        attempt=attempt,
        payload={"question": str(candidate.get("question") or "").strip()},
    )
    return _task_from_trusted_candidate(family, packet, candidate, attempt, seed, snapshot, runtime)


async def _trusted_candidate(
    family: TrustedFamily,
    packet: list[TrustedCatalogItem],
    generator_alias: str,
    settings: Settings,
    tracker: _TrustedTracker,
    attempt: int,
) -> dict[str, Any]:
    if generator_alias.startswith("mock_"):
        return _deterministic_trusted_candidate(family, packet)
    hard_negative_instruction = ""
    if family == "hard_negative":
        hard_negative_instruction = (
            " Для hard_negative: E1 является единственным gold evidence. "
            "E2 является только distractor/negative candidate. "
            "Вопрос должен отвечаться по E1, не раскрывать ответ в тексте вопроса "
            "и не требовать факт из E2."
        )
    prompt = (
        "Создай train-задачу для локального Wikipedia RAG датасета. "
        "Используй только EVIDENCE PACKET, не используй внешнюю память. "
        "Верни JSON с ключами question, reference_answer, accepted_answers, answer_type, reasoning_path. "
        "Вопрос должен быть конкретным и не должен копировать длинные фразы evidence."
        f"{hard_negative_instruction}\n\n"
        f"trusted_family: {family}\n"
        f"EVIDENCE PACKET:\n{_format_packet(packet)}"
    )
    messages = [
        {"role": "system", "content": "Ты генератор закрытых train задач для parser-aware RAG evaluation."},
        {"role": "user", "content": prompt},
    ]
    payload: dict[str, Any] | None = None
    for retry_index in range(TRUSTED_PROVIDER_RETRY_LIMIT + 1):
        if retry_index > 0:
            tracker.note_retry()
            await tracker.emit(
                "provider_retry",
                phase="family_generation",
                family=family,
                attempt=attempt,
                payload={"retry": retry_index},
            )
            await asyncio.sleep(TRUSTED_RETRY_BASE_DELAY_SECONDS * (2 ** (retry_index - 1)))
        try:
            payload = await chat_completion(
                messages,
                settings,
                alias=generator_alias,
                response_format={"type": "json_object"},
                max_provider_attempts=1,
            )
            break
        except Exception as exc:
            if retry_index == TRUSTED_PROVIDER_RETRY_LIMIT or not _is_transient_provider_error(exc):
                raise
    if payload is None:
        raise RuntimeError("provider returned no payload")
    content = str(payload["choices"][0]["message"]["content"])
    return dict(json.loads(content))


def _task_from_trusted_candidate(
    family: TrustedFamily,
    packet: list[TrustedCatalogItem],
    candidate: dict[str, Any],
    attempt: int,
    seed: int,
    snapshot: CorpusSnapshot,
    runtime: TrustedGenerateRuntimeConfig,
) -> TrustedEvalTask:
    unanswerable = family == "unanswerable"
    task_family = _eval_task_family(family)
    source_items = [] if unanswerable else ([packet[0]] if family == "hard_negative" else packet)
    spans = [_source_span(index, item) for index, item in enumerate(source_items, start=1)]
    gold_items = source_items
    negative_candidates = _negative_candidates(family, packet)
    reference = str(candidate.get("reference_answer") or "").strip()
    accepted_answers = _string_list(candidate.get("accepted_answers"))
    if reference and reference not in accepted_answers:
        accepted_answers.insert(0, reference)
    if unanswerable:
        reference = "Недостаточно evidence"
        accepted_answers = ["Недостаточно evidence"]
    answer_type = _answer_type(candidate.get("answer_type"), family)
    structural = packet[0].structural_element if packet else "prose"
    provenance = TrustedProvenance(
        zim_checksum=snapshot.zim_checksum,
        snapshot_id=snapshot.snapshot_id,
        index_version=snapshot.index_version,
        retrieval_profile_hash=snapshot.retrieval_profile_hash,
    )
    verification = _local_verification_results(
        spans,
        negative_candidates,
        unanswerable=unanswerable,
        multi_hop=len(gold_items) > 1,
    )
    return TrustedEvalTask(
        task_id=f"pending-trusted-{family}-{attempt:06d}",
        question=str(candidate.get("question") or "").strip(),
        task_family=task_family,
        trusted_family=family,
        reference_answer=reference or ("Недостаточно evidence" if unanswerable else _short_answer(packet[0].content)),
        accepted_answers=accepted_answers or [reference],
        unanswerable=unanswerable,
        expected_mode=(
            "unanswerable" if unanswerable else ("extended_beneficial" if len(gold_items) > 1 else "normal_sufficient")
        ),
        gold_page_ids=sorted({item.document_id for item in gold_items}),
        gold_section_ids=sorted({item.section_id for item in gold_items}),
        gold_chunk_ids=[item.chunk_id for item in gold_items],
        gold_evidence=[
            GoldEvidence(
                evidence_id=f"e{index}",
                document_id=item.document_id,
                section_id=item.section_id,
                chunk_id=item.chunk_id,
                quote=span.text,
                supports_claim_ids=[f"c{index}"],
                hop=index,
                title=item.title,
                source_url=item.source_url,
            )
            for index, (item, span) in enumerate(zip(gold_items, spans, strict=False), start=1)
        ],
        reasoning_path=_string_list(candidate.get("reasoning_path")) or [item.title for item in packet],
        generator_alias=runtime.generator.alias,
        verifier_alias=runtime.verifier.alias,
        zim_checksum=snapshot.zim_checksum,
        snapshot_id=snapshot.snapshot_id,
        index_version=snapshot.index_version,
        retrieval_profile_hash=snapshot.retrieval_profile_hash,
        language="ru",
        tags=["wikipedia", "trusted-v2", family, structural],
        generation_seed=seed,
        hard_negative_page_ids=[item.document_id for item in negative_candidates if item.document_id],
        source_spans=spans,
        structural_element=structural,
        answer_type=answer_type,
        verification_results=verification,
        negative_candidates=negative_candidates,
        provenance=provenance,
        split="train",
        review_status="unreviewed",
    )


def load_trusted_status(run_id: str) -> TrustedGenerateRunStatus:
    return TrustedGenerateRunStatus.model_validate(read_json(trusted_run_paths(run_id).status))


def load_latest_trusted_status() -> TrustedGenerateRunStatus:
    if not LATEST_TRUSTED_RUN.exists():
        raise FileNotFoundError("no trusted generate status is available yet")
    payload = read_json(LATEST_TRUSTED_RUN)
    return load_trusted_status(str(payload["run_id"]))


def load_trusted_partial_tasks(run_id: str) -> list[TrustedEvalTask]:
    return read_jsonl(trusted_run_paths(run_id).accepted_partial, TrustedEvalTask)


def format_trusted_status(status: TrustedGenerateRunStatus) -> str:
    family_progress = ", ".join(
        f"{family}:{status.stats.family_accepted.get(family, 0)}/{target}"
        for family, target in status.stats.family_targets.items()
    )
    lines = [
        f"run_id={status.run_id} state={status.state} phase={status.phase}",
        f"updated_at={status.updated_at}",
        (
            f"progress total={status.stats.accepted}/{status.count_target}"
            f" rejected={status.stats.rejected}/{status.config.rejection_budget}"
            f" errors={status.stats.errors} retries={status.stats.retries}"
        ),
        f"models generator={status.config.generator.alias} verifier={status.config.verifier.alias}",
        f"families {family_progress}",
    ]
    if status.active_family:
        lines.append(f"active_family={status.active_family} current_attempt={status.current_attempt or '-'}")
    if status.manifest_path:
        lines.append(f"manifest={status.manifest_path}")
    if status.error_message:
        lines.append(f"error={status.error_message}")
    if status.stats.rejection_reasons:
        reasons = ", ".join(f"{reason}:{count}" for reason, count in sorted(status.stats.rejection_reasons.items()))
        lines.append(f"rejection_reasons {reasons}")
    return "\n".join(lines)


class TrustedGenerateCliReporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def __call__(self, status: TrustedGenerateRunStatus, event: str, payload: dict[str, Any]) -> None:
        try:
            print(format_trusted_progress_event(status, event, payload), file=self._stream, flush=True)
        except OSError:
            return


def format_trusted_progress_event(status: TrustedGenerateRunStatus, event: str, payload: dict[str, Any]) -> str:
    elapsed = _format_elapsed(float(payload.get("elapsed_seconds") or 0.0))
    prefix = f"[{elapsed}]"
    progress = _format_trusted_progress(status)
    attempt = f" attempt={status.current_attempt}" if status.current_attempt is not None else ""
    reason = f" reason={payload.get('reason')}" if payload.get("reason") else ""
    retry = f" retry={payload.get('retry')}" if payload.get("retry") else ""
    question = f' question="{payload.get("question")}"' if payload.get("question") else ""

    if event == "run_started":
        return (
            f"{prefix} start run_id={status.run_id} dataset={status.dataset_name}"
            f" total={status.stats.accepted}/{status.count_target}"
            f" rejected={status.stats.rejected}/{status.config.rejection_budget}"
            f" snapshot={status.snapshot_id} index={status.index_version}"
        )
    if event == "catalog_started":
        return f"{prefix} {progress} state=catalog_started"
    if event == "catalog_completed":
        return f"{prefix} {progress} state=catalog_completed pools={payload.get('pool_sizes')}"
    if event == "family_started":
        return f"{prefix} {progress} state=family_started"
    if event == "attempt_started":
        return f"{prefix} {progress}{attempt} state=attempt_started"
    if event == "candidate_generated":
        return f"{prefix} {progress}{attempt} state=candidate_generated{question}"
    if event == "provider_retry":
        return f"{prefix} {progress}{attempt} state=provider_retry{retry}"
    if event == "candidate_rejected":
        return f"{prefix} {progress}{attempt} state=rejected{reason}{question}"
    if event == "task_accepted":
        return f"{prefix} {progress}{attempt} state=accepted{question}"
    if event == "family_completed":
        return f"{prefix} {progress} state=family_completed"
    if event == "run_completed":
        return f"{prefix} state=completed {_format_trusted_stats(status)}"
    if event == "run_failed":
        return f"{prefix} state=failed {_format_trusted_stats(status)} error={status.error_message}"
    return f"{prefix} {progress} event={event}"


def _format_trusted_progress(status: TrustedGenerateRunStatus) -> str:
    base = (
        f"total={status.stats.accepted}/{status.count_target}"
        f" rejected={status.stats.rejected}/{status.config.rejection_budget}"
    )
    if status.active_family is None:
        return base
    family_target = status.stats.family_targets.get(status.active_family, 0)
    family_accepted = status.stats.family_accepted.get(status.active_family, 0)
    return f"family={status.active_family} family_progress={family_accepted}/{family_target} {base}"


def _format_trusted_stats(status: TrustedGenerateRunStatus) -> str:
    families = ", ".join(
        f"{family}:{status.stats.family_accepted.get(family, 0)}/{target}"
        for family, target in status.stats.family_targets.items()
    )
    reasons = ",".join(f"{reason}:{count}" for reason, count in sorted(status.stats.rejection_reasons.items()))
    return (
        f"total={status.stats.accepted}/{status.count_target}"
        f" rejected={status.stats.rejected}/{status.config.rejection_budget}"
        f" errors={status.stats.errors}"
        f" retries={status.stats.retries}"
        f" reasons={reasons or '-'}"
        f" families={families}"
    )


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


async def pool_trusted_dataset(
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
    from wikipediarag.eval.commands import eval_retrieval_run

    return await eval_retrieval_run(
        suite=suite,
        api=api,
        batch_size=batch_size,
        run_id=run_id,
        resume_run_id=resume_run_id,
        rerun_failed=rerun_failed,
        settings=settings,
        progress_callback=progress_callback,
    )


def write_trusted_report(*, suite: str = TRUSTED_DATASET_NAME) -> dict[str, str]:
    latest = dataset_paths(suite)["latest"]
    if not latest.exists():
        raise FileNotFoundError(f"no trusted dataset found for suite {suite}")
    manifest = EvalDatasetManifest.model_validate(read_json(latest))
    tasks = read_jsonl(Path(manifest.jsonl_path), TrustedEvalTask)
    by_family = Counter(task.trusted_family for task in tasks)
    by_structural = Counter(task.structural_element for task in tasks)
    by_answer_type = Counter(task.answer_type for task in tasks)
    failures = [
        {
            "task_id": task.task_id,
            "failed_checks": [check.check for check in task.verification_results if not check.passed],
        }
        for task in tasks
        if any(not check.passed for check in task.verification_results)
    ]
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "coverage": {
            "by_trusted_family": dict(sorted(by_family.items())),
            "by_structural_element": dict(sorted(by_structural.items())),
            "by_answer_type": dict(sorted(by_answer_type.items())),
            "split": {"train": len(tasks)},
            "review_status": {"unreviewed": len(tasks)},
        },
        "failed_local_checks": failures,
    }
    report_id = f"{suite}-{manifest.dataset_hash[:12]}"
    json_path = TRUSTED_REPORTS_ROOT / f"{report_id}.json"
    md_path = TRUSTED_REPORTS_ROOT / f"{report_id}.md"
    write_json(json_path, payload)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_trusted_report_markdown(manifest, payload), encoding="utf-8")
    write_json(TRUSTED_REPORTS_ROOT / "latest.json", {"json": str(json_path), "markdown": str(md_path), "suite": suite})
    return {"json": str(json_path), "markdown": str(md_path), "suite": suite}


async def map_miracl_ru(*, input_path: Path, settings: Settings | None = None) -> dict[str, str]:
    from wikipediarag.eval.external import transfer_miracl_ru

    return await transfer_miracl_ru(input_path=input_path, settings=settings)


class _TrustedRunLock:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.path = trusted_run_paths(run_id).lock
        self.acquired = False

    @classmethod
    def acquire(cls, run_id: str, *, takeover_stale: bool) -> _TrustedRunLock:
        lock = cls(run_id)
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "owner_pid": os.getpid(),
            "started_at": utc_now_iso(),
        }
        try:
            descriptor = os.open(lock.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            lock_payload = _read_lock_payload(lock.path)
            owner_pid = int(lock_payload.get("owner_pid") or 0)
            if owner_pid and _pid_is_running(owner_pid):
                raise RuntimeError(
                    f"trusted generate run {run_id} is already locked by active pid {owner_pid}; "
                    "use a different --run-id or wait for it to finish"
                ) from None
            if not takeover_stale:
                raise RuntimeError(
                    f"trusted generate run {run_id} has a stale lock; rerun with --takeover-stale-run "
                    "after confirming no generator process is active"
                ) from None
            lock.path.unlink(missing_ok=True)
            descriptor = os.open(lock.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        lock.acquired = True
        return lock

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            payload = _read_lock_payload(self.path)
            if int(payload.get("owner_pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False


def _read_lock_payload(path: Path) -> dict[str, Any]:
    try:
        return read_json(path)
    except Exception:
        return {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class _TrustedTracker:
    def __init__(
        self,
        runtime: TrustedGenerateRuntimeConfig,
        snapshot: CorpusSnapshot,
        *,
        callback: TrustedProgressCallback | None = None,
    ) -> None:
        self.runtime = runtime
        self.snapshot = snapshot
        self.callback = callback
        self.started_at = utc_now_iso()
        self.started_perf = time.perf_counter()
        self.state: TrustedGenerateState = "running"
        self.phase: TrustedGeneratePhase = "preparing"
        self.active_family: TrustedFamily | None = None
        self.current_attempt: int | None = None
        self.accepted = 0
        self.rejected = 0
        self.errors = 0
        self.retries = 0
        self.family_accepted: dict[TrustedFamily, int] = {family: 0 for family in runtime.family_targets}
        self.family_attempts_started: dict[TrustedFamily, int] = {family: 0 for family in runtime.family_targets}
        self.rejection_reasons: dict[str, int] = {}
        self.family_rejection_reasons: dict[TrustedFamily, dict[str, int]] = {
            family: {} for family in runtime.family_targets
        }
        self.dataset_hash = ""
        self.manifest_path = ""
        self.error_message = ""

    @classmethod
    def from_status(
        cls,
        runtime: TrustedGenerateRuntimeConfig,
        snapshot: CorpusSnapshot,
        status: TrustedGenerateRunStatus,
        tasks: list[TrustedEvalTask],
        *,
        callback: TrustedProgressCallback | None = None,
    ) -> _TrustedTracker:
        tracker = cls(runtime, snapshot, callback=callback)
        tracker.started_at = status.started_at
        tracker.phase = status.phase if status.phase not in {"completed", "failed"} else "preparing"
        tracker.accepted = len(tasks)
        tracker.rejected = status.stats.rejected
        tracker.errors = status.stats.errors
        tracker.retries = status.stats.retries
        tracker.family_accepted = {family: 0 for family in runtime.family_targets}
        for task in tasks:
            tracker.family_accepted[task.trusted_family] = tracker.family_accepted.get(task.trusted_family, 0) + 1
        tracker.family_attempts_started = dict(status.family_attempts_started)
        tracker.rejection_reasons = dict(status.stats.rejection_reasons)
        tracker.family_rejection_reasons = {
            family: dict(status.stats.family_rejection_reasons.get(family, {})) for family in runtime.family_targets
        }
        return tracker

    async def emit(
        self,
        event: str,
        *,
        phase: TrustedGeneratePhase,
        family: TrustedFamily | None = None,
        attempt: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.phase = phase
        self.active_family = family
        self.current_attempt = attempt
        status = self._status()
        elapsed = round(time.perf_counter() - self.started_perf, 3)
        _write_trusted_status(status)
        append_jsonl(
            trusted_run_paths(self.runtime.run_id).events,
            {
                "event": event,
                "elapsed_seconds": elapsed,
                "family": family,
                "attempt": attempt,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "errors": self.errors,
                "retries": self.retries,
                "payload": payload or {},
                "updated_at": status.updated_at,
            },
        )
        if self.callback is not None:
            self.callback(status, event, {"elapsed_seconds": elapsed, **(payload or {})})

    async def accept(self, family: TrustedFamily, attempt: int, task: TrustedEvalTask) -> None:
        self.accepted += 1
        self.family_accepted[family] = self.family_accepted.get(family, 0) + 1
        append_jsonl(trusted_run_paths(self.runtime.run_id).accepted_partial, task)
        await self.emit(
            "task_accepted",
            phase="family_generation",
            family=family,
            attempt=attempt,
            payload={"question": task.question},
        )

    async def reject(self, family: TrustedFamily, attempt: int, *, reason: str, question: str = "") -> None:
        self.rejected += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1
        family_reasons = self.family_rejection_reasons.setdefault(family, {})
        family_reasons[reason] = family_reasons.get(reason, 0) + 1
        append_jsonl(
            trusted_run_paths(self.runtime.run_id).rejected,
            {"family": family, "attempt": attempt, "reason": reason, "question": question, "updated_at": utc_now_iso()},
        )
        await self.emit(
            "candidate_rejected",
            phase="family_generation",
            family=family,
            attempt=attempt,
            payload={"reason": reason, "question": question},
        )

    def note_retry(self) -> None:
        self.retries += 1

    def rejection_budget_remaining(self) -> int:
        return max(0, self.runtime.rejection_budget - self.rejected)

    def rejection_budget_exhausted(self) -> bool:
        return self.rejected >= self.runtime.rejection_budget

    async def completed(self, manifest: EvalDatasetManifest) -> None:
        self.state = "completed"
        self.phase = "completed"
        self.dataset_hash = manifest.dataset_hash
        self.manifest_path = str(Path(manifest.jsonl_path).with_suffix(".manifest.json"))
        await self.emit("run_completed", phase="completed")

    async def failed(self, error_message: str) -> None:
        self.state = "failed"
        self.phase = "failed"
        self.error_message = error_message
        await self.emit("run_failed", phase="failed", payload={"error": error_message})

    def _status(self) -> TrustedGenerateRunStatus:
        return TrustedGenerateRunStatus(
            run_id=self.runtime.run_id,
            state=self.state,
            phase=self.phase,
            started_at=self.started_at,
            updated_at=utc_now_iso(),
            count_target=self.runtime.count,
            active_family=self.active_family,
            current_attempt=self.current_attempt,
            config=self.runtime,
            snapshot_id=self.snapshot.snapshot_id,
            index_version=self.snapshot.index_version,
            zim_checksum=self.snapshot.zim_checksum,
            retrieval_profile_hash=self.snapshot.retrieval_profile_hash,
            stats=TrustedGenerateStats(
                accepted=self.accepted,
                rejected=self.rejected,
                errors=self.errors,
                retries=self.retries,
                family_accepted=dict(self.family_accepted),
                family_targets=dict(self.runtime.family_targets),
                rejection_reasons=dict(self.rejection_reasons),
                family_rejection_reasons={
                    family: dict(reasons) for family, reasons in self.family_rejection_reasons.items()
                },
            ),
            family_attempts_started=dict(self.family_attempts_started),
            dataset_hash=self.dataset_hash,
            manifest_path=self.manifest_path,
            error_message=self.error_message,
        )


def _write_trusted_status(status: TrustedGenerateRunStatus) -> None:
    write_json_atomic(trusted_run_paths(status.run_id).status, status.model_dump(mode="json"))
    write_json_atomic(LATEST_TRUSTED_RUN, {"run_id": status.run_id, "updated_at": status.updated_at})


def _resolve_trusted_runtime(
    snapshot: CorpusSnapshot,
    *,
    count: int,
    concurrency: int | None,
    rejection_budget: int,
    generator_alias: str | None,
    verifier_alias: str | None,
    family_weights: dict[TrustedFamily, float] | None,
    run_id: str | None,
    settings: Settings,
) -> TrustedGenerateRuntimeConfig:
    profile = get_retrieval_profile(snapshot.retrieval_profile, settings)
    generator_name = generator_alias or profile.model_aliases.generator_main
    verifier_name = verifier_alias or profile.model_aliases.verifier
    weights = (
        {family: float(family_weights.get(family, 0.0)) for family in TRUSTED_FAMILY_ORDER}
        if family_weights is not None
        else dict(TRUSTED_FAMILY_WEIGHTS)
    )
    return TrustedGenerateRuntimeConfig(
        run_id=run_id or f"trusted-generate-{utc_now_iso().replace(':', '').replace('-', '')}-{secrets.token_hex(4)}",
        count=count,
        concurrency=resolve_generate_concurrency(concurrency),
        rejection_budget=rejection_budget,
        generator=resolve_chat_model_alias(generator_name, settings),
        verifier=resolve_chat_model_alias(verifier_name, settings),
        family_weights=weights,
        family_targets=trusted_family_targets(count, family_weights),
    )


def _resolve_resume_runtime(
    status: TrustedGenerateRunStatus,
    snapshot: CorpusSnapshot,
    *,
    count: int,
    concurrency: int | None,
    rejection_budget: int,
    generator_alias: str | None,
    verifier_alias: str | None,
    family_weights: dict[TrustedFamily, float] | None,
    settings: Settings,
) -> TrustedGenerateRuntimeConfig:
    if status.state == "completed":
        raise ValueError(f"trusted generate run {status.run_id} is already completed")
    expected = {
        "snapshot_id": status.snapshot_id,
        "index_version": status.index_version,
        "zim_checksum": status.zim_checksum,
        "retrieval_profile_hash": status.retrieval_profile_hash,
    }
    current = {
        "snapshot_id": snapshot.snapshot_id,
        "index_version": snapshot.index_version,
        "zim_checksum": snapshot.zim_checksum,
        "retrieval_profile_hash": snapshot.retrieval_profile_hash,
    }
    mismatches = {key: (expected[key], current[key]) for key in expected if expected[key] != current[key]}
    if mismatches:
        raise ValueError(f"resume run {status.run_id} does not match current corpus: {mismatches}")
    config = status.config
    if count != config.count:
        raise ValueError(f"resume run {status.run_id} count mismatch: {count} != {config.count}")
    if concurrency is not None and resolve_generate_concurrency(concurrency) != config.concurrency:
        raise ValueError(f"resume run {status.run_id} concurrency mismatch")
    if rejection_budget != config.rejection_budget:
        raise ValueError(f"resume run {status.run_id} rejection budget mismatch")
    if generator_alias is not None and generator_alias != config.generator.alias:
        raise ValueError(f"resume run {status.run_id} generator alias mismatch")
    if verifier_alias is not None and verifier_alias != config.verifier.alias:
        raise ValueError(f"resume run {status.run_id} verifier alias mismatch")
    if family_weights is not None and trusted_family_targets(config.count, family_weights) != config.family_targets:
        raise ValueError(f"resume run {status.run_id} family targets mismatch")
    resolve_chat_model_alias(config.generator.alias, settings)
    resolve_chat_model_alias(config.verifier.alias, settings)
    return config


def _catalog_items(chunks: list[CorpusChunk], aliases: list[tuple[str, CorpusChunk]]) -> list[TrustedCatalogItem]:
    items = [_catalog_item(chunk, alias="", redirect_target="") for chunk in chunks]
    items.extend(
        _catalog_item(
            chunk,
            alias=alias,
            redirect_target=str(chunk.metadata.get("zim_entry_path") or ""),
        )
        for alias, chunk in aliases
    )
    dedup: dict[tuple[str, str], TrustedCatalogItem] = {}
    for item in items:
        dedup.setdefault((item.chunk_id, item.alias), item)
    return list(dedup.values())


def _catalog_item(chunk: CorpusChunk, *, alias: str, redirect_target: str) -> TrustedCatalogItem:
    return TrustedCatalogItem(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        section_id=chunk.section_id,
        title=chunk.title,
        source_url=chunk.source_url,
        section_path=list(chunk.section_path),
        content=chunk.content,
        structural_element="redirect_alias" if alias else _structural_element(chunk),
        zim_entry_path=str(chunk.metadata.get("zim_entry_path") or ""),
        alias=alias,
        redirect_target=redirect_target,
        prev_chunk_id=chunk.prev_chunk_id,
        next_chunk_id=chunk.next_chunk_id,
        metadata=chunk.metadata,
    )


def _structural_element(chunk: CorpusChunk) -> str:
    title = chunk.title.casefold()
    content = chunk.content
    if "(значения)" in title or re.fullmatch(r"\d+(\s+год( до н\. э\.)?)?", title):
        return "ambiguous_title"
    if len(chunk.section_path) > 1 or chunk.prev_chunk_id:
        return "deep_section"
    if re.search(r"(^|\s)(список|перечень|состоит из|включает:)", content.casefold()):
        return "list_like"
    if re.search(r"\b(таблица|столбец|строка|показатель)\b", content.casefold()):
        return "table_like"
    if re.search(r"\b(родил[асься]|дата рождения|страна|жанр|тип|основан[ао]?)\b", content.casefold()):
        return "infobox_like"
    return "prose"


def _build_pools(items: list[TrustedCatalogItem]) -> dict[TrustedFamily, list[Any]]:
    aliases = [item for item in items if item.alias]
    structured = [item for item in items if item.structural_element in {"list_like", "table_like", "infobox_like"}]
    deep = [item for item in items if item.structural_element == "deep_section"]
    prose = [item for item in items if item.structural_element == "prose"]
    pairs = _paired_items(items)
    return {
        "single_hop_prose": prose or items,
        "deep_section_fact": deep or items,
        "redirect_alias_rare": aliases or [item for item in items if _rare_title(item.title)],
        "structured_fact": structured or items,
        "bridge_multi_hop": pairs,
        "comparison_multi_hop": pairs,
        "unanswerable": items,
        "hard_negative": pairs,
    }


def _select_packet(family: TrustedFamily, pool: list[Any], attempt: int) -> list[TrustedCatalogItem]:
    item = pool[(attempt - 1) % len(pool)]
    if isinstance(item, tuple):
        return [item[0], item[1]]
    return [item]


def _paired_items(items: list[TrustedCatalogItem]) -> list[tuple[TrustedCatalogItem, TrustedCatalogItem]]:
    ordered = sorted(items, key=lambda item: (re.sub(r"\d+", "#", item.title.casefold()), item.title, item.chunk_id))
    pairs: list[tuple[TrustedCatalogItem, TrustedCatalogItem]] = []
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.document_id != right.document_id:
            pairs.append((left, right))
    return pairs


def _source_span(index: int, item: TrustedCatalogItem) -> TrustedSourceSpan:
    text = _supporting_text(item.content)
    start = max(0, item.content.find(text))
    end = start + len(text)
    return TrustedSourceSpan(
        span_id=f"span-{index}",
        document_id=item.document_id,
        section_id=item.section_id,
        chunk_id=item.chunk_id,
        title=item.title,
        source_url=item.source_url,
        section_path=list(item.section_path),
        structural_element=item.structural_element,
        text=text,
        start_char=start,
        end_char=end,
        sentence=_first_sentence(text),
    )


def _supporting_text(content: str) -> str:
    normalized = " ".join(content.split())
    return normalized[:900].strip()


def _first_sentence(text: str) -> str:
    for separator in (". ", "! ", "? "):
        if separator in text:
            return text.split(separator, 1)[0].strip()
    return text[:240].strip()


def _deterministic_trusted_candidate(family: TrustedFamily, packet: list[TrustedCatalogItem]) -> dict[str, Any]:
    if family == "unanswerable":
        return {
            "question": f"Какой официальный серийный номер указан в локальном snapshot для «{packet[0].title}»?",
            "reference_answer": "Недостаточно evidence",
            "accepted_answers": ["Недостаточно evidence"],
            "answer_type": "unanswerable",
            "reasoning_path": [packet[0].title],
        }
    if family == "hard_negative":
        return {
            "question": (
                f"Какое утверждение подтверждает первое свидетельство о «{packet[0].title}», "
                f"если «{packet[1].title}» дано только как похожий отвлекающий контекст?"
            ),
            "reference_answer": _short_answer(packet[0].content),
            "accepted_answers": [_short_answer(packet[0].content)],
            "answer_type": "span",
            "reasoning_path": [packet[0].title, f"distractor: {packet[1].title}"],
        }
    if len(packet) > 1:
        return {
            "question": f"Какие локальные факты нужно сопоставить для «{packet[0].title}» и «{packet[1].title}»?",
            "reference_answer": f"{_short_answer(packet[0].content)} / {_short_answer(packet[1].content)}",
            "accepted_answers": [_short_answer(packet[0].content), _short_answer(packet[1].content)],
            "answer_type": "comparison",
            "reasoning_path": [packet[0].title, packet[1].title],
        }
    alias = f" под названием «{packet[0].alias}»" if packet[0].alias else ""
    return {
        "question": f"Какой факт{alias} подтверждается локальной статьёй «{packet[0].title}»?",
        "reference_answer": _short_answer(packet[0].content),
        "accepted_answers": [_short_answer(packet[0].content)],
        "answer_type": "span",
        "reasoning_path": [packet[0].alias or packet[0].title],
    }


def _format_packet(packet: list[TrustedCatalogItem]) -> str:
    parts: list[str] = []
    for index, item in enumerate(packet, start=1):
        parts.append(
            "\n".join(
                [
                    f"[E{index}] title={item.title}",
                    f"alias={item.alias}",
                    f"document_id={item.document_id}",
                    f"section_id={item.section_id}",
                    f"chunk_id={item.chunk_id}",
                    f"structural_element={item.structural_element}",
                    f"section_path={' / '.join(item.section_path)}",
                    f"source_url={item.source_url}",
                    f"text={item.content[:1400]}",
                ]
            )
        )
    return "\n\n".join(parts)


def _eval_task_family(family: TrustedFamily) -> TaskFamily:
    mapping: dict[TrustedFamily, TaskFamily] = {
        "single_hop_prose": "single_hop_factual",
        "deep_section_fact": "deep_section_fact",
        "redirect_alias_rare": "alias_redirect_rare",
        "structured_fact": "single_hop_factual",
        "bridge_multi_hop": "comparison_multi_hop",
        "comparison_multi_hop": "comparison_multi_hop",
        "unanswerable": "unanswerable",
        "hard_negative": "hard_negative",
    }
    return mapping[family]


def _negative_candidates(family: TrustedFamily, packet: list[TrustedCatalogItem]) -> list[TrustedNegativeCandidate]:
    if family == "hard_negative" and len(packet) > 1:
        return [
            TrustedNegativeCandidate(
                document_id=packet[1].document_id,
                chunk_id=packet[1].chunk_id,
                title=packet[1].title,
                reason="paired distractor with related title/category",
                source="local_pairing",
            )
        ]
    if family == "unanswerable":
        return [
            TrustedNegativeCandidate(
                document_id=packet[0].document_id,
                chunk_id=packet[0].chunk_id,
                title=packet[0].title,
                reason="topic-like page intentionally lacks the requested fact",
                source="counterfactual_question",
            )
        ]
    return []


def _local_verification_results(
    spans: list[TrustedSourceSpan],
    negatives: list[TrustedNegativeCandidate],
    *,
    unanswerable: bool,
    multi_hop: bool,
) -> list[TrustedVerificationResult]:
    gold_chunks = {span.chunk_id for span in spans}
    negative_chunks = {item.chunk_id for item in negatives if item.chunk_id}
    return [
        TrustedVerificationResult(
            check="answerable_has_source_span",
            passed=unanswerable or bool(spans),
            details="answerable tasks must keep exact parser-output spans",
        ),
        TrustedVerificationResult(
            check="span_chunk_binding",
            passed=all(span.text and span.end_char >= span.start_char for span in spans),
            details="source spans include text and character boundaries",
        ),
        TrustedVerificationResult(
            check="negative_gold_disjoint",
            passed=not bool(gold_chunks & negative_chunks),
            details="hard negatives must not reuse gold chunks",
        ),
        TrustedVerificationResult(
            check="multi_hop_full_coverage",
            passed=not multi_hop or len({span.document_id for span in spans}) >= 2,
            details="multi-hop tasks require evidence from at least two documents",
        ),
    ]


def _trusted_task_rejection_reason(task: TrustedEvalTask, seen_questions: set[str]) -> str:
    if task.question in seen_questions:
        return "duplicate_question"
    if not task.question or len(task.question) < 12:
        return "local_validation_rejected"
    if task.split != "train" or task.review_status != "unreviewed":
        return "local_validation_rejected"
    if _answer_leaks(task.question, task.reference_answer):
        return "answer_leak"
    if not task.unanswerable and (not task.source_spans or not task.gold_chunk_ids):
        return "missing_source_span"
    failed = {check.check for check in task.verification_results if not check.passed}
    if "negative_gold_disjoint" in failed:
        return "negative_gold_overlap"
    if "multi_hop_full_coverage" in failed:
        return "invalid_multi_hop"
    if failed:
        return "local_validation_rejected"
    return ""


def _trusted_task_valid(task: TrustedEvalTask) -> bool:
    return _trusted_task_rejection_reason(task, set()) == ""


def _validate_trusted_tasks(tasks: list[TrustedEvalTask]) -> None:
    if not tasks:
        raise RuntimeError("trusted dataset has no tasks")
    ids = [task.task_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise RuntimeError("trusted dataset has duplicate task ids")
    for task in tasks:
        if not _trusted_task_valid(task):
            raise RuntimeError(f"trusted task failed local validation: {task.task_id}")


def _write_trusted_dataset(
    tasks: list[TrustedEvalTask],
    manifest: EvalDatasetManifest,
    runtime: TrustedGenerateRuntimeConfig,
) -> None:
    jsonl_path = Path(manifest.jsonl_path)
    write_jsonl(jsonl_path, tasks)
    manifest_payload = {
        **manifest.model_dump(mode="json"),
        "trusted_schema_version": TRUSTED_DATASET_VERSION,
        "split_policy": "all_train_unreviewed",
        "runtime": runtime.model_dump(mode="json"),
        "coverage": {
            "by_trusted_family": dict(Counter(task.trusted_family for task in tasks)),
            "by_structural_element": dict(Counter(task.structural_element for task in tasks)),
        },
    }
    write_json(jsonl_path.with_suffix(".manifest.json"), manifest_payload)
    write_json(dataset_paths(manifest.dataset_name)["latest"], manifest_payload)


def _trusted_report_markdown(manifest: EvalDatasetManifest, payload: dict[str, Any]) -> str:
    lines = [
        f"# Trusted Wikipedia dataset report: {manifest.dataset_name}",
        "",
        f"- Dataset hash: `{manifest.dataset_hash}`",
        f"- Task count: `{manifest.task_count}`",
        f"- Snapshot: `{manifest.snapshot_id}`",
        f"- Index version: `{manifest.index_version}`",
        "- Split policy: `all train / unreviewed`",
        "",
        "## Coverage",
        "",
    ]
    coverage = dict(payload["coverage"])
    for section in ("by_trusted_family", "by_structural_element", "by_answer_type", "split", "review_status"):
        lines.extend([f"### {section}", "", "| Value | Count |", "|---|---:|"])
        for key, value in sorted(dict(coverage.get(section, {})).items()):
            lines.append(f"| {key} | {value} |")
        lines.append("")
    failures = list(payload["failed_local_checks"])
    lines.extend(["## Failed Local Checks", ""])
    lines.append("None" if not failures else f"{len(failures)} tasks failed local checks.")
    lines.append("")
    return "\n".join(lines)


def _parse_miracl_line(line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        parts = line.split("\t")
        return {
            "id": parts[0] if parts else "",
            "query": parts[1] if len(parts) > 1 else "",
            "title": parts[2] if len(parts) > 2 else "",
        }
    title = str(payload.get("title") or payload.get("doc_title") or "")
    positives = payload.get("positive_passages")
    if not title and isinstance(positives, list) and positives and isinstance(positives[0], dict):
        title = str(positives[0].get("title") or "")
    return {
        "id": payload.get("query_id") or payload.get("id") or payload.get("qid"),
        "query": payload.get("query") or payload.get("question") or "",
        "title": title,
    }


def _answer_type(value: Any, family: TrustedFamily) -> AnswerType:
    allowed = {"span", "number", "date", "yes_no", "list", "comparison", "unanswerable"}
    raw = str(value or "").strip()
    if raw in allowed:
        return raw  # type: ignore[return-value]
    if family == "unanswerable":
        return "unanswerable"
    if family in {"bridge_multi_hop", "comparison_multi_hop", "hard_negative"}:
        return "comparison"
    return "span"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _short_answer(text: str) -> str:
    normalized = " ".join(text.split())
    for separator in (". ", "! ", "? "):
        if separator in normalized:
            return normalized.split(separator, 1)[0][:240].strip()
    return normalized[:240].strip()


def _rare_title(title: str) -> bool:
    return bool(re.search(r"\d|[()]", title)) or len(title.split()) >= 3


def _answer_leaks(question: str, answer: str) -> bool:
    answer_tokens = {item for item in re.findall(r"[\wА-Яа-яЁё]+", answer.casefold()) if len(item) > 2}
    if len(answer_tokens) < 4:
        return False
    question_tokens = {item for item in re.findall(r"[\wА-Яа-яЁё]+", question.casefold()) if len(item) > 2}
    if not question_tokens:
        return False
    return len(answer_tokens & question_tokens) / len(answer_tokens | question_tokens) > 0.7


def _is_transient_provider_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.NetworkError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    return False
