from __future__ import annotations

import inspect
import sys
from collections.abc import Awaitable, Callable
from typing import TextIO

from wikipediarag.eval.schemas import EvalGenerateProgressEvent, EvalGenerateRunStatus

type EvalGenerateProgressCallback = Callable[[EvalGenerateProgressEvent], Awaitable[None] | None]


async def emit_progress(
    callback: EvalGenerateProgressCallback | None,
    event: EvalGenerateProgressEvent,
) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


class EvalGenerateCliReporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def __call__(self, event: EvalGenerateProgressEvent) -> None:
        try:
            print(format_progress_event(event), file=self._stream, flush=True)
        except OSError:
            return


def format_progress_event(event: EvalGenerateProgressEvent) -> str:
    prefix = f"[{_format_elapsed(event.elapsed_seconds)}]"
    progress = _format_progress(event)
    attempt = f" attempt={event.attempt}" if event.attempt is not None else ""
    question = f' question="{event.question}"' if event.question else ""

    if event.event == "run_started":
        return (
            f"{prefix} start run_id={event.run_id} total=0/{event.count_target}"
            f" dataset={event.dataset_name or 'generated-wikipedia-v1'}"
            f" snapshot={event.snapshot_id}"
            f" index={event.index_version}"
        )
    if event.event == "family_started":
        return f"{prefix} {progress} state=family_started"
    if event.event == "attempt_started":
        return f"{prefix} {progress}{attempt} state=attempt_started"
    if event.event == "candidate_generated":
        return f"{prefix} {progress}{attempt} state=candidate_generated{question}"
    if event.event == "candidate_rejected":
        return f"{prefix} {progress}{attempt} state=rejected reason={event.reason}{question}"
    if event.event == "provider_error":
        return f"{prefix} {progress}{attempt} state=error reason=provider_error{question}"
    if event.event == "task_accepted":
        return f"{prefix} {progress}{attempt} state=accepted{question}"
    if event.event == "family_completed":
        return f"{prefix} {progress} state=family_completed"
    if event.event == "run_completed":
        return f"{prefix} state=completed {_format_stats(event)}"
    if event.event == "run_failed":
        return f"{prefix} state=failed {_format_stats(event)}"
    return f"{prefix} event={event.event}"


def _format_progress(event: EvalGenerateProgressEvent) -> str:
    if event.family is None:
        return f"total={event.total_accepted}/{event.count_target}"
    family_accepted = event.family_accepted if event.family_accepted is not None else 0
    family_target = event.family_target if event.family_target is not None else 0
    return (
        f"family={event.family} family_progress={family_accepted}/{family_target}"
        f" total={event.total_accepted}/{event.count_target}"
    )


def _format_stats(event: EvalGenerateProgressEvent) -> str:
    stats = event.stats
    if stats is None:
        return f"total={event.total_accepted}/{event.count_target}"
    families = ", ".join(
        f"{family}:{stats.family_accepted.get(family, 0)}/{target}" for family, target in stats.family_targets.items()
    )
    return (
        f"duration={_format_elapsed(event.elapsed_seconds)}"
        f" total={event.total_accepted}/{event.count_target}"
        f" accepted={stats.accepted}"
        f" rejected={stats.rejected}"
        f" errors={stats.errors}"
        f" retries={stats.retries}"
        f" families={families}"
    )


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_generate_status(status: EvalGenerateRunStatus) -> str:
    lines = [
        f"run_id={status.run_id} state={status.state} phase={status.phase}",
        f"updated_at={status.updated_at}",
        (
            f"progress total={status.stats.accepted}/{status.count_target}"
            f" rejected={status.stats.rejected}"
            f" errors={status.stats.errors}"
            f" retries={status.stats.retries}"
        ),
        (
            "models "
            f"generator={status.config.generator.alias} "
            f"({status.config.generator.provider}:{status.config.generator.model}) "
            f"verifier={status.config.verifier.alias} "
            f"({status.config.verifier.provider}:{status.config.verifier.model})"
        ),
    ]
    if status.active_family is not None:
        current = status.stats.family_accepted.get(status.active_family, 0)
        target = status.family_targets.get(status.active_family, 0)
        lines.append(
            f"active_family={status.active_family} family_progress={current}/{target} "
            f"current_attempt={status.current_attempt or '-'}"
        )
    family_progress = ", ".join(
        f"{family}:{status.stats.family_accepted.get(family, 0)}/{target}"
        for family, target in status.family_targets.items()
    )
    lines.append(f"families {family_progress}")
    if status.accepted_tasks:
        lines.append("recent_questions:")
        for record in status.accepted_tasks[-5:]:
            lines.append(
                f"- {record.task_family}: {record.question} "
                f"pages={','.join(record.gold_page_ids)} "
                f"chunks={','.join(record.gold_chunk_ids)}"
            )
    if status.error_message:
        lines.append(f"error={status.error_message}")
    if status.manifest_path:
        lines.append(f"manifest={status.manifest_path}")
    return "\n".join(lines)
