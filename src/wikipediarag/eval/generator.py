from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    DATASET_NAME,
    DATASET_VERSION,
    dataset_hash,
    utc_now_iso,
    write_dataset,
)
from wikipediarag.eval.corpus import CorpusChunk, CorpusSnapshot, load_alias_chunks, load_candidate_chunks
from wikipediarag.eval.generate_runs import (
    FAMILY_ORDER,
    accepted_task_record,
    append_generate_partial_task,
    default_family_targets,
    generate_run_paths,
    load_generate_partial_tasks,
    load_generate_status,
    normalize_family_targets,
    resolve_generate_runtime_config,
    resolve_resume_runtime_config,
    write_generate_status,
)
from wikipediarag.eval.hashing import stable_json_hash
from wikipediarag.eval.progress import EvalGenerateProgressCallback, emit_progress
from wikipediarag.eval.schemas import (
    EvalDatasetManifest,
    EvalGenerateAcceptedTaskRecord,
    EvalGenerateEventType,
    EvalGeneratePhase,
    EvalGenerateProgressEvent,
    EvalGenerateRejectReason,
    EvalGenerateRunStatus,
    EvalGenerateRuntimeConfig,
    EvalGenerateState,
    EvalGenerateStats,
    EvalTask,
    ExpectedMode,
    GoldEvidence,
    TaskFamily,
)
from wikipediarag.model_client import chat_completion

SMOKE_MARKER = ARTIFACT_ROOT / "smoke" / "latest-success.json"
VerifierOutcome = Literal["accept", "verifier_rejected", "invalid_verifier_json"]
MAX_ATTEMPTS_MULTIPLIER = 12
GENERATOR_TRY_LIMIT = 2


@dataclass
class _GenerateProgressTracker:
    runtime: EvalGenerateRuntimeConfig
    snapshot: CorpusSnapshot
    callback: EvalGenerateProgressCallback | None = None
    dataset_name: str = DATASET_NAME
    started_at_iso: str = field(default_factory=utc_now_iso)
    started_at: float = field(default_factory=time.perf_counter)
    state: EvalGenerateState = "running"
    phase: EvalGeneratePhase = "preparing"
    active_family: TaskFamily | None = None
    current_attempt: int | None = None
    error_message: str = ""
    dataset_hash: str = ""
    manifest_path: str = ""
    accepted: int = 0
    rejected: int = 0
    errors: int = 0
    retries: int = 0
    accepted_by_family: dict[TaskFamily, int] = field(init=False)
    family_attempts_started: dict[TaskFamily, int] = field(init=False)
    accepted_records: list[EvalGenerateAcceptedTaskRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.accepted_by_family = {family: 0 for family in self.runtime.family_targets}
        self.family_attempts_started = {family: 0 for family in self.runtime.family_targets}

    @classmethod
    def from_resume(
        cls,
        runtime: EvalGenerateRuntimeConfig,
        snapshot: CorpusSnapshot,
        status: EvalGenerateRunStatus,
        accepted_tasks: list[EvalTask],
        *,
        callback: EvalGenerateProgressCallback | None = None,
    ) -> _GenerateProgressTracker:
        tracker = cls(
            runtime=runtime,
            snapshot=snapshot,
            callback=callback,
            dataset_name=status.dataset_name or DATASET_NAME,
            started_at_iso=status.started_at,
        )
        tracker.state = "running"
        tracker.phase = status.phase if status.phase not in {"completed", "failed"} else "preparing"
        tracker.active_family = status.active_family
        tracker.current_attempt = status.current_attempt
        tracker.error_message = status.error_message
        tracker.accepted = status.stats.accepted
        tracker.rejected = status.stats.rejected
        tracker.errors = status.stats.errors
        tracker.retries = status.stats.retries
        tracker.accepted_by_family = {
            family: int(status.stats.family_accepted.get(family, 0)) for family in runtime.family_targets
        }
        tracker.family_attempts_started = {
            family: int(status.family_attempts_started.get(family, 0)) for family in runtime.family_targets
        }
        tracker.accepted_records = [accepted_task_record(task) for task in accepted_tasks]
        return tracker

    async def run_started(self) -> None:
        self.state = "running"
        self.phase = "preparing"
        await self._emit("run_started")

    async def family_started(self, family: TaskFamily) -> None:
        self.phase = "family_generation"
        self.active_family = family
        self.current_attempt = None
        await self._emit("family_started", family=family)

    async def attempt_started(self, family: TaskFamily, attempt: int) -> None:
        self.phase = "family_generation"
        self.active_family = family
        self.current_attempt = attempt
        self.family_attempts_started[family] = max(self.family_attempts_started[family], attempt)
        await self._emit("attempt_started", family=family, attempt=attempt)

    async def candidate_generated(self, family: TaskFamily, attempt: int, *, question: str) -> None:
        await self._emit("candidate_generated", family=family, attempt=attempt, question=question)

    async def candidate_rejected(
        self,
        family: TaskFamily,
        attempt: int,
        *,
        reason: EvalGenerateRejectReason,
        question: str = "",
    ) -> None:
        self.rejected += 1
        await self._emit("candidate_rejected", family=family, attempt=attempt, reason=reason, question=question)

    async def provider_error(self, family: TaskFamily, attempt: int, *, question: str = "") -> None:
        self.errors += 1
        await self._emit("provider_error", family=family, attempt=attempt, reason="provider_error", question=question)

    def note_retry(self) -> None:
        self.retries += 1

    async def task_accepted(self, family: TaskFamily, attempt: int, *, task: EvalTask) -> None:
        self.accepted += 1
        self.accepted_by_family[family] += 1
        self.accepted_records.append(accepted_task_record(task))
        append_generate_partial_task(self.runtime.run_id, task)
        await self._emit("task_accepted", family=family, attempt=attempt, question=task.question)

    async def family_completed(self, family: TaskFamily) -> None:
        self.phase = "family_generation"
        self.active_family = family
        self.current_attempt = None
        await self._emit("family_completed", family=family)

    async def dataset_writing(self) -> None:
        self.phase = "writing_dataset"
        self.active_family = None
        self.current_attempt = None
        await self._write_status()

    async def run_completed(self, manifest: EvalDatasetManifest) -> None:
        self.state = "completed"
        self.phase = "completed"
        self.active_family = None
        self.current_attempt = None
        self.dataset_hash = manifest.dataset_hash
        self.manifest_path = str(Path(manifest.jsonl_path).with_suffix(".manifest.json"))
        await self._emit("run_completed", include_stats=True)

    async def run_failed(self, error_message: str, family: TaskFamily | None = None) -> None:
        self.state = "failed"
        self.phase = "failed"
        self.active_family = family
        self.current_attempt = None
        self.error_message = error_message
        await self._emit("run_failed", family=family, include_stats=True)

    async def _emit(
        self,
        event: EvalGenerateEventType,
        *,
        family: TaskFamily | None = None,
        attempt: int | None = None,
        question: str = "",
        reason: EvalGenerateRejectReason | None = None,
        include_stats: bool = False,
    ) -> None:
        await emit_progress(
            self.callback,
            EvalGenerateProgressEvent(
                event=event,
                elapsed_seconds=time.perf_counter() - self.started_at,
                count_target=self.runtime.count,
                total_accepted=self.accepted,
                family=family,
                family_target=self.runtime.family_targets.get(family) if family is not None else None,
                family_accepted=self.accepted_by_family.get(family) if family is not None else None,
                attempt=attempt,
                question=question,
                reason=reason,
                dataset_name=self.dataset_name,
                run_id=self.runtime.run_id,
                snapshot_id=self.snapshot.snapshot_id,
                index_version=self.snapshot.index_version,
                stats=self._stats() if include_stats else None,
            ),
        )
        await self._write_status()

    def _stats(self) -> EvalGenerateStats:
        return EvalGenerateStats(
            accepted=self.accepted,
            rejected=self.rejected,
            errors=self.errors,
            retries=self.retries,
            family_accepted=dict(self.accepted_by_family),
            family_targets=dict(self.runtime.family_targets),
        )

    async def _write_status(self) -> None:
        write_generate_status(
            EvalGenerateRunStatus(
                run_id=self.runtime.run_id,
                state=self.state,
                phase=self.phase,
                started_at=self.started_at_iso,
                updated_at=utc_now_iso(),
                active_family=self.active_family,
                current_attempt=self.current_attempt,
                count_target=self.runtime.count,
                family_targets=dict(self.runtime.family_targets),
                family_attempts_started=dict(self.family_attempts_started),
                config=self.runtime,
                snapshot_id=self.snapshot.snapshot_id,
                index_version=self.snapshot.index_version,
                zim_checksum=self.snapshot.zim_checksum,
                retrieval_profile_hash=self.snapshot.retrieval_profile_hash,
                stats=self._stats(),
                accepted_tasks=list(self.accepted_records),
                dataset_name=self.dataset_name,
                dataset_hash=self.dataset_hash,
                manifest_path=self.manifest_path,
                error_message=self.error_message,
            )
        )


def family_targets(
    count: int,
    family_weights: dict[TaskFamily, float] | None = None,
) -> dict[TaskFamily, int]:
    if family_weights is None:
        return default_family_targets(count)
    return normalize_family_targets(count, family_weights)


async def generate_dataset(
    count: int | None = None,
    *,
    concurrency: int | None = None,
    generator_alias: str | None = None,
    verifier_alias: str | None = None,
    family_weights: dict[TaskFamily, float] | None = None,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    settings: Settings | None = None,
    progress_callback: EvalGenerateProgressCallback | None = None,
) -> EvalDatasetManifest:
    resolved = settings or get_settings()
    snapshot = await _load_and_validate_smoke_marker(resolved)
    resumed_tasks: list[EvalTask] = []
    tracker: _GenerateProgressTracker
    if resume_run_id is not None:
        status = load_generate_status(resume_run_id)
        runtime = resolve_resume_runtime_config(
            status,
            snapshot,
            count=count,
            concurrency=concurrency,
            generator_alias=generator_alias,
            verifier_alias=verifier_alias,
            family_weights=family_weights,
            settings=resolved,
        )
        resumed_tasks = load_generate_partial_tasks(runtime.run_id)
        tracker = _GenerateProgressTracker.from_resume(
            runtime,
            snapshot,
            status,
            resumed_tasks,
            callback=progress_callback,
        )
    else:
        if count is None:
            count = 150
        runtime = resolve_generate_runtime_config(
            snapshot,
            count=count,
            concurrency=concurrency,
            generator_alias=generator_alias,
            verifier_alias=verifier_alias,
            family_weights=family_weights,
            run_id=run_id,
            settings=resolved,
        )
        paths = generate_run_paths(runtime.run_id)
        if paths.base.exists():
            raise FileExistsError(f"generate run directory already exists for {runtime.run_id}")
        tracker = _GenerateProgressTracker(runtime=runtime, snapshot=snapshot, callback=progress_callback)

    candidates = await load_candidate_chunks(limit=max(1200, runtime.count * 12), settings=resolved)
    aliases = await load_alias_chunks(limit=max(300, runtime.count * 4), settings=resolved)
    pools = _build_pools(candidates, aliases)
    tasks = list(resumed_tasks)
    seen_questions = [task.question for task in tasks]

    await tracker.run_started()

    try:
        for family in FAMILY_ORDER:
            target = runtime.family_targets.get(family, 0)
            if target == 0:
                continue
            if tracker.accepted_by_family.get(family, 0) >= target:
                continue
            await tracker.family_started(family)
            await _generate_family(
                family,
                target,
                pools,
                tasks,
                seen_questions,
                snapshot=snapshot,
                runtime=runtime,
                settings=resolved,
                tracker=tracker,
            )
            await tracker.family_completed(family)
    except Exception as exc:
        await tracker.run_failed(str(exc))
        raise

    tasks.sort(key=lambda item: (item.task_family, item.generation_seed, item.question))
    for index, task in enumerate(tasks, start=1):
        task.task_id = f"priv-wiki-{index:06d}"
    digest = dataset_hash(tasks)
    base = ARTIFACT_ROOT / "datasets" / DATASET_NAME
    jsonl_path = base / f"{DATASET_NAME}-{snapshot.snapshot_id}-{digest[:12]}.jsonl"
    manifest = EvalDatasetManifest(
        dataset_name=DATASET_NAME,
        dataset_version=DATASET_VERSION,
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
    try:
        await tracker.dataset_writing()
        write_dataset(tasks, manifest)
    except Exception as exc:
        await tracker.run_failed(str(exc))
        raise
    await tracker.run_completed(manifest)
    return manifest


async def _generate_family(
    family: TaskFamily,
    target: int,
    pools: dict[TaskFamily, list[Any]],
    tasks: list[EvalTask],
    seen_questions: list[str],
    *,
    snapshot: CorpusSnapshot,
    runtime: EvalGenerateRuntimeConfig,
    settings: Settings,
    tracker: _GenerateProgressTracker,
) -> None:
    max_attempts = max(target * MAX_ATTEMPTS_MULTIPLIER, target)
    attempts_started = tracker.family_attempts_started.get(family, 0)
    pending: dict[asyncio.Task[tuple[int, EvalTask | None]], int] = {}
    try:
        while tracker.accepted_by_family[family] < target and (attempts_started < max_attempts or pending):
            while (
                tracker.accepted_by_family[family] < target
                and tracker.accepted_by_family[family] + len(pending) < target
                and len(pending) < runtime.concurrency
                and attempts_started < max_attempts
            ):
                attempts_started += 1
                tracker.family_attempts_started[family] = attempts_started
                task = asyncio.create_task(
                    _generate_one(
                        family,
                        pools,
                        attempts_started,
                        snapshot=snapshot,
                        generator_alias=runtime.generator.alias,
                        verifier_alias=runtime.verifier.alias,
                        settings=settings,
                        progress=tracker,
                    )
                )
                pending[task] = attempts_started
            if not pending:
                break
            done, _pending = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                attempt = pending.pop(finished)
                _attempt_number, candidate_task = await finished
                if candidate_task is None:
                    continue
                if tracker.accepted_by_family[family] >= target or not _task_valid(candidate_task, seen_questions):
                    await tracker.candidate_rejected(
                        family,
                        attempt,
                        reason="local_validation_rejected",
                        question=candidate_task.question,
                    )
                    continue
                tasks.append(candidate_task)
                seen_questions.append(candidate_task.question)
                await tracker.task_accepted(family, attempt, task=candidate_task)
        if tracker.accepted_by_family[family] != target:
            raise RuntimeError(
                f"could generate only {tracker.accepted_by_family[family]}/{target} tasks for family {family}"
            )
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending.keys(), return_exceptions=True)


async def _generate_one(
    family: TaskFamily,
    pools: dict[TaskFamily, list[Any]],
    attempt: int,
    *,
    snapshot: CorpusSnapshot,
    generator_alias: str,
    verifier_alias: str,
    settings: Settings,
    progress: _GenerateProgressTracker,
) -> tuple[int, EvalTask | None]:
    seed = stable_json_hash([family, attempt, snapshot.index_version], 16)
    await progress.attempt_started(family, attempt)
    packet = _select_packet(family, pools, attempt)
    if not packet:
        return attempt, None
    try:
        candidate = await _llm_candidate(
            family,
            packet,
            attempt=attempt,
            generator_alias=generator_alias,
            verifier_alias=verifier_alias,
            settings=settings,
            progress=progress,
        )
    except Exception:
        await progress.provider_error(family, attempt)
        return attempt, None
    if candidate is None:
        return attempt, None
    return (
        attempt,
        _task_from_candidate(
            family,
            packet,
            candidate,
            attempt=attempt,
            seed=int(seed[:8], 16),
            snapshot=snapshot,
            generator_alias=generator_alias,
            verifier_alias=verifier_alias,
        ),
    )


def build_smoke_tasks(chunks: list[CorpusChunk], snapshot: CorpusSnapshot, *, count: int) -> list[EvalTask]:
    ordered = [chunk for chunk in chunks if not chunk.prev_chunk_id]
    ordered.extend(chunk for chunk in chunks if chunk.prev_chunk_id)
    selected = _distinct_pages(ordered, count)
    if len(selected) < count:
        raise RuntimeError(f"not enough imported ZIM pages for smoke: {len(selected)}/{count}")
    tasks: list[EvalTask] = []
    for index, chunk in enumerate(selected, start=1):
        answer = _short_answer(chunk.content)
        question = f"Что говорится в статье «{chunk.title}»?"
        tasks.append(
            EvalTask(
                task_id=f"smoke-zim-{index:03d}",
                question=question,
                task_family="single_hop_factual",
                reference_answer=answer,
                accepted_answers=[answer],
                unanswerable=False,
                expected_mode="normal_sufficient",
                gold_page_ids=[chunk.document_id],
                gold_section_ids=[chunk.section_id],
                gold_chunk_ids=[chunk.chunk_id],
                gold_evidence=[
                    GoldEvidence(
                        evidence_id="e1",
                        document_id=chunk.document_id,
                        section_id=chunk.section_id,
                        chunk_id=chunk.chunk_id,
                        quote=chunk.content[:700],
                        supports_claim_ids=["c1"],
                        hop=1,
                        title=chunk.title,
                        source_url=chunk.source_url,
                    )
                ],
                reasoning_path=[chunk.title],
                generator_alias="deterministic_smoke",
                verifier_alias="deterministic_smoke",
                zim_checksum=snapshot.zim_checksum,
                snapshot_id=snapshot.snapshot_id,
                index_version=snapshot.index_version,
                retrieval_profile_hash=snapshot.retrieval_profile_hash,
                generation_seed=index,
            )
        )
    return tasks


async def _load_and_validate_smoke_marker(settings: Settings) -> CorpusSnapshot:
    from wikipediarag.eval.corpus import load_corpus_snapshot

    snapshot = await load_corpus_snapshot(settings)
    if not SMOKE_MARKER.exists():
        raise RuntimeError("eval-generate requires a successful eval-smoke marker first")
    marker = json.loads(SMOKE_MARKER.read_text(encoding="utf-8"))
    if int(marker.get("count") or 0) < 10:
        raise RuntimeError("eval-generate requires eval-smoke --count 10 or larger")
    expected = {
        "snapshot_id": snapshot.snapshot_id,
        "index_version": snapshot.index_version,
        "zim_checksum": snapshot.zim_checksum,
        "retrieval_profile_hash": snapshot.retrieval_profile_hash,
    }
    mismatches = {key: (marker.get(key), value) for key, value in expected.items() if marker.get(key) != value}
    if mismatches:
        raise RuntimeError(f"latest eval-smoke marker does not match current corpus: {mismatches}")
    return snapshot


def _build_pools(
    candidates: list[CorpusChunk],
    aliases: list[tuple[str, CorpusChunk]],
) -> dict[TaskFamily, list[Any]]:
    deep = [chunk for chunk in candidates if chunk.prev_chunk_id]
    rare = [chunk for chunk in candidates if _rare_title(chunk.title)]
    comparison_pairs = _paired_chunks(candidates)
    return {
        "single_hop_factual": candidates,
        "alias_redirect_rare": aliases or [(chunk.title, chunk) for chunk in rare],
        "deep_section_fact": deep or candidates,
        "comparison_multi_hop": comparison_pairs,
        "unanswerable": candidates,
        "hard_negative": comparison_pairs,
    }


def _select_packet(
    family: TaskFamily,
    pools: dict[TaskFamily, list[Any]],
    attempt: int,
) -> list[tuple[str, CorpusChunk]]:
    pool = pools[family]
    if not pool:
        return []
    item = pool[(attempt - 1) % len(pool)]
    if family == "alias_redirect_rare":
        alias, chunk = item
        return [(str(alias), chunk)]
    if family in {"comparison_multi_hop", "hard_negative"}:
        left, right = item
        return [(left.title, left), (right.title, right)]
    chunk = item
    return [(chunk.title, chunk)]


async def _llm_candidate(
    family: TaskFamily,
    packet: list[tuple[str, CorpusChunk]],
    *,
    attempt: int,
    generator_alias: str,
    verifier_alias: str,
    settings: Settings,
    progress: _GenerateProgressTracker,
) -> dict[str, Any] | None:
    if generator_alias.startswith("mock_") or verifier_alias.startswith("mock_"):
        candidate = _deterministic_candidate(family, packet)
        await progress.candidate_generated(family, attempt, question=str(candidate.get("question") or "").strip())
        return candidate
    evidence_packet = _format_evidence_packet(packet)
    prompt = (
        "Создай закрытую evaluation task для локальной Wikipedia RAG. "
        "Используй только EVIDENCE PACKET. Не используй знания из памяти. "
        "Не копируй длинные фразы из evidence в вопрос. "
        f"{_family_prompt_instructions(family)} "
        "Верни только JSON с ключами question, reference_answer, accepted_answers, reasoning_path.\n\n"
        f"task_family: {family}\n"
        f"EVIDENCE PACKET:\n{evidence_packet}"
    )
    for retry_index in range(GENERATOR_TRY_LIMIT):
        if retry_index > 0:
            progress.note_retry()
        try:
            payload = await chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты генератор RAG evaluation tasks. Закрытый корпус является единственным источником."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                settings,
                alias=generator_alias,
                response_format={"type": "json_object"},
            )
        except Exception:
            await progress.provider_error(family, attempt)
            return None
        content = str(payload["choices"][0]["message"]["content"])
        try:
            candidate = dict(json.loads(content))
        except json.JSONDecodeError:
            await progress.candidate_rejected(family, attempt, reason="invalid_generator_json")
            continue
        question = str(candidate.get("question") or "").strip()
        await progress.candidate_generated(family, attempt, question=question)
        try:
            verifier = await _verify_candidate(
                candidate,
                family,
                packet,
                verifier_alias=verifier_alias,
                settings=settings,
            )
        except Exception:
            await progress.provider_error(family, attempt, question=question)
            return None
        if verifier == "accept":
            return candidate
        await progress.candidate_rejected(family, attempt, reason=verifier, question=question)
    return None


async def _verify_candidate(
    candidate: dict[str, Any],
    family: TaskFamily,
    packet: list[tuple[str, CorpusChunk]],
    *,
    verifier_alias: str,
    settings: Settings,
) -> VerifierOutcome:
    if verifier_alias.startswith("mock_"):
        return "accept"
    prompt = (
        "Проверь candidate task как adversarial reviewer. Используй только evidence ниже. "
        "Верни JSON: verdict accept|reject, answerable_from_gold, shortcut_found, ambiguity_found.\n\n"
        f"task_family: {family}\nCANDIDATE:\n{json.dumps(candidate, ensure_ascii=False)}\n\n"
        f"EVIDENCE:\n{_format_evidence_packet(packet)}"
    )
    payload = await chat_completion(
        [
            {"role": "system", "content": "Ты строгий verifier закрытого RAG evaluation set."},
            {"role": "user", "content": prompt},
        ],
        settings,
        alias=verifier_alias,
        response_format={"type": "json_object"},
    )
    content = str(payload["choices"][0]["message"]["content"])
    try:
        result = dict(json.loads(content))
    except json.JSONDecodeError:
        return "invalid_verifier_json"
    if str(result.get("verdict")) != "accept":
        return "verifier_rejected"
    if result.get("ambiguity_found") is True:
        return "verifier_rejected"
    if family == "comparison_multi_hop" and result.get("shortcut_found") is True:
        return "verifier_rejected"
    return "accept"


def _task_from_candidate(
    family: TaskFamily,
    packet: list[tuple[str, CorpusChunk]],
    candidate: dict[str, Any],
    *,
    attempt: int,
    seed: int,
    snapshot: CorpusSnapshot,
    generator_alias: str,
    verifier_alias: str,
) -> EvalTask:
    chunks = [chunk for _label, chunk in packet]
    unanswerable = family == "unanswerable"
    gold_chunks = [] if unanswerable else chunks
    gold_evidence = [
        GoldEvidence(
            evidence_id=f"e{index}",
            document_id=chunk.document_id,
            section_id=chunk.section_id,
            chunk_id=chunk.chunk_id,
            quote=chunk.content[:900],
            supports_claim_ids=[f"c{index}"],
            hop=index,
            title=chunk.title,
            source_url=chunk.source_url,
        )
        for index, chunk in enumerate(gold_chunks, start=1)
    ]
    accepted_answers = _string_list(candidate.get("accepted_answers"))
    reference = str(candidate.get("reference_answer") or "").strip()
    if reference and reference not in accepted_answers:
        accepted_answers.insert(0, reference)
    expected_mode: ExpectedMode
    if unanswerable:
        expected_mode = "unanswerable"
    elif len(gold_chunks) > 1:
        expected_mode = "extended_beneficial"
    else:
        expected_mode = "normal_sufficient"
    final_answers = accepted_answers or [reference]
    return EvalTask(
        task_id=f"pending-{family}-{attempt:06d}",
        question=str(candidate.get("question") or "").strip(),
        task_family=family,
        reference_answer=reference or ("Недостаточно evidence" if unanswerable else _short_answer(chunks[0].content)),
        accepted_answers=["Недостаточно evidence"] if unanswerable else final_answers,
        unanswerable=unanswerable,
        expected_mode=expected_mode,
        gold_page_ids=sorted({chunk.document_id for chunk in gold_chunks}),
        gold_section_ids=sorted({chunk.section_id for chunk in gold_chunks}),
        gold_chunk_ids=[chunk.chunk_id for chunk in gold_chunks],
        gold_evidence=gold_evidence,
        reasoning_path=_string_list(candidate.get("reasoning_path")) or [label for label, _ in packet],
        generator_alias=generator_alias,
        verifier_alias=verifier_alias,
        zim_checksum=snapshot.zim_checksum,
        snapshot_id=snapshot.snapshot_id,
        index_version=snapshot.index_version,
        retrieval_profile_hash=snapshot.retrieval_profile_hash,
        tags=["wikipedia", family],
        generation_seed=seed,
        hard_negative_page_ids=[chunks[1].document_id] if family == "hard_negative" and len(chunks) > 1 else [],
    )


def _task_valid(task: EvalTask, seen_questions: list[str]) -> bool:
    if not task.question or len(task.question) < 12:
        return False
    if _generic_question(task.question):
        return False
    if not task.unanswerable and (not task.gold_chunk_ids or not task.reference_answer):
        return False
    if not task.unanswerable and _weak_reference_answer(task.reference_answer):
        return False
    if _answer_leaks(task.question, task.reference_answer):
        return False
    if task.task_family == "comparison_multi_hop" and not _comparison_task_valid(task):
        return False
    normalized = _question_tokens(task.question)
    for seen in seen_questions:
        if _jaccard(normalized, _question_tokens(seen)) > 0.86:
            return False
    return True


def _deterministic_candidate(family: TaskFamily, packet: list[tuple[str, CorpusChunk]]) -> dict[str, Any]:
    labels = [label for label, _chunk in packet]
    chunks = [chunk for _label, chunk in packet]
    if family == "unanswerable":
        return {
            "question": f"Какой точный серийный номер локального объекта указан для «{labels[0]}»?",
            "reference_answer": "Недостаточно evidence",
            "accepted_answers": ["Недостаточно evidence"],
            "reasoning_path": labels,
        }
    if family in {"comparison_multi_hop", "hard_negative"} and len(chunks) > 1:
        return {
            "question": f"В чём различие между «{labels[0]}» и «{labels[1]}» по локальным статьям?",
            "reference_answer": f"{_short_answer(chunks[0].content)} / {_short_answer(chunks[1].content)}",
            "accepted_answers": [_short_answer(chunks[0].content), _short_answer(chunks[1].content)],
            "reasoning_path": labels,
        }
    alias_prefix = (
        f" под alias «{labels[0]}»" if family == "alias_redirect_rare" and labels[0] != chunks[0].title else ""
    )
    return {
        "question": f"Какой факт о «{chunks[0].title}»{alias_prefix} подтверждается локальным evidence?",
        "reference_answer": _short_answer(chunks[0].content),
        "accepted_answers": [_short_answer(chunks[0].content)],
        "reasoning_path": labels,
    }


def _family_prompt_instructions(family: TaskFamily) -> str:
    instructions: dict[TaskFamily, str] = {
        "single_hop_factual": (
            "Сформулируй конкретный factual question по одному подтверждаемому факту, а не просьбу пересказать статью."
        ),
        "alias_redirect_rare": (
            "Вопрос должен использовать редкий alias или redirect и быть привязан к факту, а не к пересказу."
        ),
        "deep_section_fact": "Вопрос должен опираться на поздний или секционный факт, а не на lead summary.",
        "comparison_multi_hop": (
            "Нужен вопрос на сравнение или различие по двум разным статьям. Не проси просто пересказать обе статьи."
        ),
        "unanswerable": "Вопрос должен звучать правдоподобно, но не иметь подтверждения в данном evidence packet.",
        "hard_negative": (
            "Сформулируй вопрос так, чтобы был возможен правдоподобный distractor, но gold оставался однозначным."
        ),
    }
    return instructions[family]


def _format_evidence_packet(packet: list[tuple[str, CorpusChunk]]) -> str:
    parts = []
    for index, (label, chunk) in enumerate(packet, start=1):
        parts.append(
            "\n".join(
                [
                    f"[E{index}] label={label}",
                    f"title={chunk.title}",
                    f"document_id={chunk.document_id}",
                    f"section_id={chunk.section_id}",
                    f"chunk_id={chunk.chunk_id}",
                    f"text={chunk.content[:1400]}",
                ]
            )
        )
    return "\n\n".join(parts)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _distinct_pages(chunks: Iterable[CorpusChunk], count: int) -> list[CorpusChunk]:
    seen: set[str] = set()
    selected: list[CorpusChunk] = []
    for chunk in chunks:
        if chunk.document_id in seen:
            continue
        seen.add(chunk.document_id)
        selected.append(chunk)
        if len(selected) >= count:
            return selected
    return selected


def _paired_chunks(chunks: list[CorpusChunk]) -> list[tuple[CorpusChunk, CorpusChunk]]:
    pairs: list[tuple[CorpusChunk, CorpusChunk]] = []
    ordered = sorted(chunks, key=lambda item: (re.sub(r"\d+", "#", item.title.casefold()), item.title))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.document_id != right.document_id:
            pairs.append((left, right))
    return pairs


def _rare_title(title: str) -> bool:
    return bool(re.search(r"\d|[()]", title)) or len(title.split()) >= 3


def _short_answer(text: str) -> str:
    normalized = " ".join(text.split())
    for separator in (". ", "! ", "? "):
        if separator in normalized:
            return normalized.split(separator, 1)[0][:240].strip()
    return normalized[:240].strip()


def _answer_leaks(question: str, answer: str) -> bool:
    normalized_answer = _question_tokens(answer)
    if len(normalized_answer) < 4:
        return False
    return _jaccard(normalized_answer, _question_tokens(question)) > 0.65


def _generic_question(question: str) -> bool:
    normalized = " ".join(question.casefold().split())
    generic_patterns = (
        r"^что (известно|говорится) о статье",
        r"^сравни,? что говорится",
        r"^расскажи о статье",
    )
    return any(re.search(pattern, normalized) for pattern in generic_patterns)


def _weak_reference_answer(answer: str) -> bool:
    normalized = answer.casefold().strip()
    if normalized in {"", "неизвестно", "не указано", "недостаточно evidence"}:
        return True
    return len(re.findall(r"[\wА-Яа-яЁё]+", normalized)) == 0


def _comparison_task_valid(task: EvalTask) -> bool:
    if len(set(task.gold_page_ids)) < 2:
        return False
    if len(task.reasoning_path) < 2:
        return False
    return True


def _question_tokens(text: str) -> set[str]:
    return {item for item in re.findall(r"[\wА-Яа-яЁё]+", text.casefold()) if len(item) > 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
