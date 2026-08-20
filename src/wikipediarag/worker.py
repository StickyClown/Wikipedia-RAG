from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from wikipediarag.db import connect, ensure_schema
from wikipediarag.ingestion import claim_and_process_once
from wikipediarag.model_client import close_http_client
from wikipediarag.reliability import safe_failure_from_exception
from wikipediarag.repository import (
    claim_due_search_projection_reconciliations,
    claim_next_search_projection_event,
    cleanup_completed_search_projection_events,
    complete_search_projection_event,
    enqueue_due_source_sync_jobs,
    fail_search_projection_reconciliation,
    finish_search_projection_reconciliation,
    get_knowledge_base,
    load_current_document_projection,
    load_index_version_by_read_alias,
    renew_search_projection_reconciliation_lease,
    retry_search_projection_event,
    schedule_historical_search_projection_reconciliations,
    touch_worker_heartbeat,
)
from wikipediarag.search_index import (
    READ_ALIAS,
    bulk_index_chunks,
    delete_exact_projection_documents,
    projection_fingerprint,
    read_document_projection,
)

LOGGER = logging.getLogger(__name__)

RESEARCH_KINDS = ("deep_research",)
BACKGROUND_KINDS = ("wikipedia_xml", "wikipedia_zim", "document_upload", "source_sync", "document_delete")


class _SearchProjectionLeaseLost(RuntimeError):
    """A conditional lease update fenced this worker from further index I/O."""


class _SearchProjectionRepairError(RuntimeError):
    """Safe, typed reconciliation failure without exposing document content."""

    retryable = False

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.safe_code = code


def _expected_projection_records(chunks: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": chunk.id,
            "document_version_id": str(chunk.metadata.get("document_version_id") or ""),
            "content_hash": chunk.content_hash,
            "metadata": {
                "publication_status": str(chunk.metadata.get("publication_status") or "published"),
            },
        }
        for chunk in chunks
    ]


def _projection_field_matches(expected: list[dict[str, Any]], observed: list[dict[str, Any]]) -> str:
    """Return non-sensitive convergence diagnostics for a failed exact read-back."""
    expected_by_chunk = {str(record["chunk_id"]): record for record in expected}
    observed_by_chunk = {
        str((record.get("_source", record)).get("chunk_id") or ""): record.get("_source", record) for record in observed
    }
    if set(expected_by_chunk) != set(observed_by_chunk):
        return "chunk_ids"
    fields = ("document_version_id", "content_hash")
    for field in fields:
        if any(
            str(expected_by_chunk[key].get(field) or "") != str(observed_by_chunk[key].get(field) or "")
            for key in expected_by_chunk
        ):
            return field
    for field in ("publication_status",):
        if any(
            (expected_by_chunk[key].get("metadata") or {}).get(field)
            != (observed_by_chunk[key].get("metadata") or {}).get(field)
            for key in expected_by_chunk
        ):
            return field
    return "fingerprint_encoding"


async def _repair_search_projection_document(
    settings: Any,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    lease_guard: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[str | None, str]:
    """Replace one exact derived document projection from canonical PostgreSQL.

    A temporary empty result is acceptable while the replacement is in flight;
    a stale record is never used as authority by retrieval.
    """

    async def require_lease() -> None:
        if lease_guard is not None and not await lease_guard():
            raise _SearchProjectionLeaseLost("SEARCH_PROJECTION_LEASE_LOST")

    await require_lease()
    async with connect(settings) as conn:
        kb = await get_knowledge_base(conn, tenant_id, knowledge_base_id)
        version_id, chunks = await load_current_document_projection(
            conn, tenant_id=tenant_id, knowledge_base_id=knowledge_base_id, document_id=document_id
        )
        index_version = await load_index_version_by_read_alias(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            read_alias=str((kb or {}).get("active_index") or READ_ALIAS),
        )
    if index_version is None:
        raise _SearchProjectionRepairError("SEARCH_PROJECTION_INDEX_UNAVAILABLE")
    limit = max(1, int(settings.search_projection_reconcile_max_chunks_per_document))
    read_alias = str(index_version["read_alias"])
    observed = await asyncio.to_thread(
        read_document_projection,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        limit=limit + 1,
        settings=settings,
        read_alias=read_alias,
    )
    if len(observed) > limit:
        raise RuntimeError("SEARCH_PROJECTION_DOCUMENT_TOO_LARGE")
    expected_records = _expected_projection_records(chunks)
    expected_hash = projection_fingerprint(expected_records)
    observed_hash = projection_fingerprint(observed)
    if expected_hash == observed_hash:
        LOGGER.info("search projection reconciliation outcome=already_consistent document_id=%s", document_id)
        return version_id, expected_hash

    expected_by_chunk = {str(chunk.id): chunk for chunk in chunks}
    expected_by_id = {f"{tenant_id}:{knowledge_base_id}:{chunk.id}": chunk for chunk in chunks}
    delete_ids: list[str] = []
    stale_or_missing: dict[str, Any] = {}
    observed_expected_ids: set[str] = set()
    for record in observed:
        source = dict(record.get("_source") or {})
        record_id = str(record.get("_id") or "")
        chunk_id = str(source.get("chunk_id") or "")
        expected = expected_by_chunk.get(chunk_id)
        if expected is None or record_id not in expected_by_id:
            delete_ids.append(record_id)
            continue
        observed_expected_ids.add(record_id)
        if projection_fingerprint([_expected_projection_records([expected])[0]]) != projection_fingerprint([record]):
            stale_or_missing[chunk_id] = expected
    for record_id, chunk in expected_by_id.items():
        if record_id not in observed_expected_ids:
            stale_or_missing[str(chunk.id)] = chunk

    mutation_batch = max(1, int(settings.search_projection_reconcile_mutation_batch_size))
    for start in range(0, len(delete_ids), mutation_batch):
        await require_lease()
        delete_batch = delete_ids[start : start + mutation_batch]
        deleted = await asyncio.to_thread(
            delete_exact_projection_documents,
            document_ids=delete_batch,
            settings=settings,
            read_alias=read_alias,
            refresh="wait_for",
        )
        if deleted != len(delete_batch):
            raise RuntimeError("SEARCH_PROJECTION_DELETE_COUNT_MISMATCH")
        await require_lease()
    index_chunks = list(stale_or_missing.values())
    for start in range(0, len(index_chunks), mutation_batch):
        await require_lease()
        index_batch = index_chunks[start : start + mutation_batch]
        indexed = await asyncio.to_thread(
            bulk_index_chunks,
            index_batch,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            settings=settings,
            write_alias=str(index_version["write_alias"]),
            physical_index=str(index_version["physical_index"]),
            read_alias=read_alias,
            dimensions=int(index_version.get("embedding_dimensions") or 0) or None,
            refresh="wait_for",
        )
        if indexed != len(index_batch):
            raise RuntimeError("SEARCH_PROJECTION_INDEX_COUNT_MISMATCH")
        await require_lease()
    observed_after = await asyncio.to_thread(
        read_document_projection,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        limit=limit + 1,
        settings=settings,
        read_alias=read_alias,
    )
    if len(observed_after) > limit:
        raise RuntimeError("SEARCH_PROJECTION_DOCUMENT_TOO_LARGE")
    observed_hash = projection_fingerprint(observed_after)
    if expected_hash != observed_hash:
        # Hash prefixes are non-content diagnostic values.  They make a failed
        # convergence observable without recording document text or metadata.
        mismatch = _projection_field_matches(_expected_projection_records(chunks), observed_after)
        raise RuntimeError(
            f"SEARCH_PROJECTION_FINGERPRINT_MISMATCH:{mismatch}:{expected_hash[:12]}:{observed_hash[:12]}"
        )
    outcome = "repaired_stale" if delete_ids and index_chunks else "removed_extra" if delete_ids else "repaired_missing"
    LOGGER.info("search projection reconciliation outcome=%s document_id=%s", outcome, document_id)
    return version_id, expected_hash


async def _process_search_projection_once(settings: Any, *, lease_id: str) -> bool:
    """Project canonical PostgreSQL state to OpenSearch without widening reads."""
    async with connect(settings) as conn:
        event = await claim_next_search_projection_event(
            conn,
            lease_id=lease_id,
            lease_seconds=settings.worker_job_lease_seconds,
        )
    if event is None:
        return False
    event_id = str(event["id"])
    try:
        payload = dict(event.get("payload") or {})
        if event["event_kind"] != "document_publication":
            raise ValueError("unsupported search projection event")
        current_version_id, _fingerprint = await _repair_search_projection_document(
            settings,
            tenant_id=str(event["tenant_id"]),
            knowledge_base_id=str(event["knowledge_base_id"]),
            document_id=str(event["document_id"]),
        )
        if event["event_kind"] == "document_publication":
            requested_version_id = str(payload.get("document_version_id") or "")
            if requested_version_id and current_version_id and requested_version_id != current_version_id:
                LOGGER.info("search projection event superseded document_id=%s", event["document_id"])
    except Exception as exc:
        LOGGER.warning(
            "search projection event failed event_id=%s kind=%s code=%s",
            event_id,
            event.get("event_kind"),
            safe_failure_from_exception(exc, stage="search_projection").error_code,
            exc_info=True,
        )
        async with connect(settings) as conn:
            await retry_search_projection_event(
                conn,
                event_id=event_id,
                lease_id=lease_id,
                error_code=safe_failure_from_exception(exc, stage="search_projection").error_code,
            )
        return True
    async with connect(settings) as conn:
        await complete_search_projection_event(conn, event_id=event_id, lease_id=lease_id)
    return True


async def _process_search_projection_reconciliation_once(settings: Any, *, lease_id: str) -> int:
    """Repair a bounded batch of documents whose derived projection may drift."""
    async with connect(settings) as conn:
        scheduled = await schedule_historical_search_projection_reconciliations(
            conn, batch_size=settings.search_projection_reconcile_batch_size
        )
        items = await claim_due_search_projection_reconciliations(
            conn,
            lease_id=lease_id,
            lease_seconds=settings.worker_job_lease_seconds,
            batch_size=settings.search_projection_reconcile_batch_size,
        )
    for item in items:
        document_id = str(item["document_id"])

        async def lease_guard(bound_document_id: str = document_id) -> bool:
            async with connect(settings) as conn:
                renewed = await renew_search_projection_reconciliation_lease(
                    conn,
                    document_id=bound_document_id,
                    lease_id=lease_id,
                    lease_seconds=settings.worker_job_lease_seconds,
                )
            if renewed:
                LOGGER.info("search projection reconciliation lease_renewed document_id=%s", bound_document_id)
            else:
                LOGGER.warning("search projection reconciliation lease_lost document_id=%s", bound_document_id)
            return renewed

        try:
            version_id, fingerprint = await _repair_search_projection_document(
                settings,
                tenant_id=str(item["tenant_id"]),
                knowledge_base_id=str(item["knowledge_base_id"]),
                document_id=document_id,
                lease_guard=lease_guard,
            )
            async with connect(settings) as conn:
                await finish_search_projection_reconciliation(
                    conn,
                    document_id=document_id,
                    lease_id=lease_id,
                    expected_document_version_id=version_id,
                    expected_projection_hash=fingerprint,
                    observed_projection_hash=fingerprint,
                    interval_seconds=settings.search_projection_reconcile_interval_seconds,
                )
        except _SearchProjectionLeaseLost:
            # Do not overwrite another owner's state or falsely record success.
            continue
        except Exception as exc:
            failure = safe_failure_from_exception(exc, stage="search_projection_reconciliation")
            LOGGER.warning(
                "search projection reconciliation failed document_id=%s code=%s",
                document_id,
                failure.error_code,
                exc_info=True,
            )
            async with connect(settings) as conn:
                await fail_search_projection_reconciliation(
                    conn,
                    document_id=document_id,
                    lease_id=lease_id,
                    error_code=failure.error_code,
                )
    async with connect(settings) as conn:
        deleted = await cleanup_completed_search_projection_events(
            conn,
            retention_days=settings.search_projection_event_retention_days,
            batch_size=settings.search_projection_event_retention_batch_size,
        )
    if scheduled:
        LOGGER.info("search projection historical_scheduled=%s", scheduled)
    if deleted:
        LOGGER.info("search projection retention_deleted=%s", deleted)
    return len(items)


async def _run_lane(
    settings: Any,
    allowed_kinds: tuple[str, ...],
    concurrency: int,
    *,
    worker_id: str,
    enqueue_sources: bool = False,
) -> None:
    lane = "/".join(allowed_kinds)

    async def heartbeat() -> None:
        interval = max(int(settings.worker_job_heartbeat_seconds), 1)
        while True:
            try:
                async with connect(settings) as conn:
                    await touch_worker_heartbeat(conn, worker_id=worker_id, lane=lane)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("worker heartbeat failed lane=%s", lane)
            await asyncio.sleep(interval)

    async def runner() -> None:
        while True:
            try:
                if enqueue_sources:
                    async with connect(settings) as conn:
                        await enqueue_due_source_sync_jobs(conn)
                    await _process_search_projection_once(settings, lease_id=str(uuid.uuid4()))
                    await _process_search_projection_reconciliation_once(settings, lease_id=str(uuid.uuid4()))
                processed = await claim_and_process_once(
                    settings,
                    allowed_kinds=allowed_kinds,
                    lease_id=str(uuid.uuid4()),
                )
                if not processed:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("worker lane failed")
                await asyncio.sleep(2)

    async with asyncio.TaskGroup() as group:
        group.create_task(heartbeat())
        for _ in range(max(int(concurrency), 1)):
            group.create_task(runner())


async def run_worker() -> None:
    logging.basicConfig(level=logging.INFO)
    from wikipediarag.config import get_settings

    settings = get_settings()
    worker_id = str(uuid.uuid4())
    await ensure_schema(settings)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(
                _run_lane(
                    settings,
                    RESEARCH_KINDS,
                    settings.worker_research_concurrency,
                    worker_id=f"{worker_id}:research",
                )
            )
            group.create_task(
                _run_lane(
                    settings,
                    BACKGROUND_KINDS,
                    settings.worker_background_concurrency,
                    worker_id=f"{worker_id}:background",
                    enqueue_sources=True,
                )
            )
    finally:
        await close_http_client()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
