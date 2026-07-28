from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from wikipediarag.config import Settings, get_settings
from wikipediarag.eval.artifacts import (
    ARTIFACT_ROOT,
    append_jsonl,
    read_json,
    read_jsonl,
    utc_now_iso,
    write_json_atomic,
)
from wikipediarag.eval.corpus import CorpusSnapshot
from wikipediarag.eval.schemas import (
    EvalGenerateAcceptedTaskRecord,
    EvalGenerateModelRef,
    EvalGenerateRunStatus,
    EvalGenerateRuntimeConfig,
    EvalTask,
    TaskFamily,
)
from wikipediarag.model_registry import get_model_registry
from wikipediarag.retrieval_profile import get_retrieval_profile

GENERATE_RUNS_ROOT = ARTIFACT_ROOT / "generate-runs"
LATEST_GENERATE_RUN = GENERATE_RUNS_ROOT / "latest.json"
DEFAULT_GENERATE_CONCURRENCY = 4
MAX_GENERATE_CONCURRENCY = 128
DEFAULT_FAMILY_WEIGHTS: dict[TaskFamily, float] = {
    "single_hop_factual": 60.0,
    "alias_redirect_rare": 20.0,
    "deep_section_fact": 20.0,
    "comparison_multi_hop": 25.0,
    "unanswerable": 15.0,
    "hard_negative": 10.0,
}
FAMILY_ORDER: tuple[TaskFamily, ...] = tuple(DEFAULT_FAMILY_WEIGHTS)


@dataclass(frozen=True)
class GenerateRunPaths:
    run_id: str
    base: Path
    status: Path
    accepted_partial: Path


def default_family_targets(count: int) -> dict[TaskFamily, int]:
    return normalize_family_targets(count, None)


def normalize_family_targets(
    count: int,
    family_weights: dict[TaskFamily, float] | None,
) -> dict[TaskFamily, int]:
    if count < 1:
        raise ValueError("eval-generate count must be >= 1")
    source = family_weights or DEFAULT_FAMILY_WEIGHTS
    weights = {family: max(0.0, float(source.get(family, 0.0))) for family in FAMILY_ORDER}
    if all(weight == 0.0 for weight in weights.values()):
        raise ValueError("at least one family weight must be > 0")
    total_weight = sum(weights.values())
    raw = {family: count * weights[family] / total_weight for family in FAMILY_ORDER}
    targets = {family: int(raw[family]) for family in FAMILY_ORDER}
    while sum(targets.values()) < count:
        family = max(FAMILY_ORDER, key=lambda item: (raw[item] - targets[item], -FAMILY_ORDER.index(item)))
        targets[family] += 1
    return targets


def generate_run_paths(run_id: str) -> GenerateRunPaths:
    base = GENERATE_RUNS_ROOT / run_id
    return GenerateRunPaths(
        run_id=run_id,
        base=base,
        status=base / "status.json",
        accepted_partial=base / "accepted.partial.jsonl",
    )


def new_run_id() -> str:
    return f"eval-generate-{utc_now_iso().replace(':', '').replace('-', '')}-{secrets.token_hex(4)}"


def resolve_generate_runtime_config(
    snapshot: CorpusSnapshot,
    *,
    count: int,
    concurrency: int | None,
    generator_alias: str | None,
    verifier_alias: str | None,
    family_weights: dict[TaskFamily, float] | None,
    run_id: str | None,
    settings: Settings | None = None,
) -> EvalGenerateRuntimeConfig:
    resolved = settings or get_settings()
    profile = get_retrieval_profile(snapshot.retrieval_profile, resolved)
    generator_name = generator_alias or profile.model_aliases.generator_main
    verifier_name = verifier_alias or profile.model_aliases.verifier
    weights = (
        {family: float(family_weights.get(family, 0.0)) for family in FAMILY_ORDER}
        if family_weights is not None
        else dict(DEFAULT_FAMILY_WEIGHTS)
    )
    return EvalGenerateRuntimeConfig(
        run_id=run_id or new_run_id(),
        count=count,
        concurrency=resolve_generate_concurrency(concurrency),
        generator=resolve_chat_model_alias(generator_name, resolved),
        verifier=resolve_chat_model_alias(verifier_name, resolved),
        family_weights=weights,
        family_targets=normalize_family_targets(count, family_weights),
    )


def resolve_resume_runtime_config(
    status: EvalGenerateRunStatus,
    snapshot: CorpusSnapshot,
    *,
    count: int | None,
    concurrency: int | None,
    generator_alias: str | None,
    verifier_alias: str | None,
    family_weights: dict[TaskFamily, float] | None,
    settings: Settings | None = None,
) -> EvalGenerateRuntimeConfig:
    resolved = settings or get_settings()
    if status.state == "completed":
        raise ValueError(f"generate run {status.run_id} is already completed")
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
    if count is not None and count != config.count:
        raise ValueError(f"resume run {status.run_id} count mismatch: {count} != {config.count}")
    if concurrency is not None and resolve_generate_concurrency(concurrency) != config.concurrency:
        raise ValueError(f"resume run {status.run_id} concurrency mismatch")
    if generator_alias is not None and generator_alias != config.generator.alias:
        raise ValueError(f"resume run {status.run_id} generator alias mismatch")
    if verifier_alias is not None and verifier_alias != config.verifier.alias:
        raise ValueError(f"resume run {status.run_id} verifier alias mismatch")
    if family_weights is not None:
        candidate_targets = normalize_family_targets(config.count, family_weights)
        if candidate_targets != config.family_targets:
            raise ValueError(f"resume run {status.run_id} family targets mismatch")
    resolve_chat_model_alias(config.generator.alias, resolved)
    resolve_chat_model_alias(config.verifier.alias, resolved)
    return config


def resolve_generate_concurrency(explicit: int | None) -> int:
    raw = explicit
    if raw is None:
        env_value = os.environ.get("EVAL_GENERATE_CONCURRENCY")
        raw = int(env_value) if env_value else DEFAULT_GENERATE_CONCURRENCY
    if raw < 1 or raw > MAX_GENERATE_CONCURRENCY:
        raise ValueError(f"eval-generate concurrency must be between 1 and {MAX_GENERATE_CONCURRENCY}")
    return raw


def resolve_chat_model_alias(alias: str, settings: Settings | None = None) -> EvalGenerateModelRef:
    registry = get_model_registry(settings or get_settings())
    model = registry.require(alias, "chat")
    return EvalGenerateModelRef(alias=alias, provider=model.provider, model=model.model)


def accepted_task_record(task: EvalTask) -> EvalGenerateAcceptedTaskRecord:
    return EvalGenerateAcceptedTaskRecord(
        question=task.question,
        task_family=task.task_family,
        gold_page_ids=list(task.gold_page_ids),
        gold_chunk_ids=list(task.gold_chunk_ids),
        reasoning_path=list(task.reasoning_path),
        generation_seed=task.generation_seed,
    )


def write_generate_status(status: EvalGenerateRunStatus) -> None:
    paths = generate_run_paths(status.run_id)
    write_json_atomic(paths.status, status.model_dump(mode="json"))
    write_json_atomic(LATEST_GENERATE_RUN, {"run_id": status.run_id, "updated_at": status.updated_at})


def append_generate_partial_task(run_id: str, task: EvalTask) -> None:
    append_jsonl(generate_run_paths(run_id).accepted_partial, task)


def load_generate_status(run_id: str) -> EvalGenerateRunStatus:
    return EvalGenerateRunStatus.model_validate(read_json(generate_run_paths(run_id).status))


def load_latest_generate_status() -> EvalGenerateRunStatus:
    if not LATEST_GENERATE_RUN.exists():
        raise FileNotFoundError("no eval-generate run status is available yet")
    payload = read_json(LATEST_GENERATE_RUN)
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        raise FileNotFoundError("latest eval-generate run pointer is empty")
    return load_generate_status(run_id)


def load_generate_partial_tasks(run_id: str) -> list[EvalTask]:
    return read_jsonl(generate_run_paths(run_id).accepted_partial, EvalTask)
