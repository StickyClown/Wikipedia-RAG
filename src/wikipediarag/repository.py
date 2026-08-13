from __future__ import annotations

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.auth import ActorContext, KnowledgeBaseRole, PlatformRole, TenantRole, effective_knowledge_base_role
from wikipediarag.db import json_dumps
from wikipediarag.document_access import DocumentAccessScope, document_access_bypass, normalize_document_access
from wikipediarag.ids import new_uuid, scoped_id, stable_hash, stable_uuid
from wikipediarag.observability import safe_telemetry_payload
from wikipediarag.schemas import JobStatus
from wikipediarag.wiki_dump import Chunk, WikiPage


class StaleWorkerLeaseError(RuntimeError):
    """Raised when a worker no longer owns the durable job lease."""


class DocumentVersionLifecycleError(RuntimeError):
    """Raised when an upload would move an existing version backwards."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_worker_lease_context: ContextVar[str | None] = ContextVar("worker_lease_id", default=None)


def set_worker_lease_context(lease_id: str | None) -> object:
    return _worker_lease_context.set(lease_id)


def reset_worker_lease_context(token: object) -> None:
    _worker_lease_context.reset(token)  # type: ignore[arg-type]


UNSAFE_PUBLIC_METADATA_TOKENS = (
    "SECRET",
    "object_key",
    "original_artifact_key",
    "normalized_artifact_key",
    "server_side_tokens",
    "access_token",
    "refresh_token",
    "s3://",
    "raw_provider_payload",
)


def research_evidence_ref(record_id: uuid.UUID | str) -> str:
    """Return the stable public citation reference for an evidence record."""

    parsed = record_id if isinstance(record_id, uuid.UUID) else uuid.UUID(str(record_id))
    return f"E-{parsed.hex}"


async def create_ingestion_job(
    conn: AsyncConnection,
    tenant_id: str,
    knowledge_base_id: str,
    kind: str,
    config: dict[str, Any],
    model_config_revision_id: str | None = None,
    model_config_hash: str | None = None,
) -> uuid.UUID:
    job_id = new_uuid()
    await conn.execute(
        text(
            """  # noqa: S608
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress,
              model_config_revision_id, model_config_hash)
            VALUES (:id, :tenant_id, :knowledge_base_id, :kind, 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb),
                    :model_config_revision_id, :model_config_hash)
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "kind": kind,
            "config": json_dumps(config),
            "progress": json_dumps({"pages_seen": 0, "pages_imported": 0, "chunks_indexed": 0}),
            "model_config_revision_id": model_config_revision_id,
            "model_config_hash": model_config_hash,
        },
    )
    return job_id


async def create_document_deletion_job(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    purge_after: datetime,
) -> uuid.UUID:
    existing = await conn.execute(
        text(
            """
            SELECT id
            FROM ingestion_jobs
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND kind = 'document_delete'
              AND status IN ('received','running')
              AND config ->> 'document_id' = :document_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "document_id": document_id},
    )
    row = existing.mappings().first()
    if row is not None:
        return uuid.UUID(str(row["id"]))

    job_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress)
            VALUES (:id, :tenant_id, :kb_id, 'document_delete', 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb))
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "config": json_dumps({"document_id": document_id, "purge_after": purge_after.isoformat()}),
            "progress": json_dumps({"stage": "scheduled", "purge_after": purge_after.isoformat()}),
        },
    )
    return job_id


async def create_source_sync_job(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    mode: str,
    cursor_before: dict[str, Any],
) -> tuple[uuid.UUID, uuid.UUID]:
    existing = await conn.execute(
        text(
            """
            SELECT id, config ->> 'sync_run_id' AS sync_run_id
            FROM ingestion_jobs
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND kind = 'source_sync'
              AND status IN ('received','running')
              AND config ->> 'source_id' = :source_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "source_id": source_id},
    )
    row = existing.mappings().first()
    if row is not None and row.get("sync_run_id"):
        return uuid.UUID(str(row["id"])), uuid.UUID(str(row["sync_run_id"]))

    job_id = new_uuid()
    run_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO source_sync_runs(
              id, tenant_id, knowledge_base_id, source_id, job_id, mode, status,
              cursor_before, checkpoint, stats
            )
            VALUES (
              :id, :tenant_id, :kb_id, :source_id, :job_id, :mode, 'received',
              CAST(:cursor_before AS jsonb), CAST(:checkpoint AS jsonb), CAST(:stats AS jsonb)
            )
            """
        ),
        {
            "id": str(run_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "source_id": source_id,
            "job_id": str(job_id),
            "mode": mode,
            "cursor_before": json_dumps(cursor_before),
            "checkpoint": json_dumps({"stage": "received"}),
            "stats": json_dumps({}),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress)
            VALUES (:id, :tenant_id, :kb_id, 'source_sync', 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb))
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "config": json_dumps({"source_id": source_id, "sync_run_id": str(run_id), "mode": mode}),
            "progress": json_dumps({"stage": "received", "source_id": source_id, "sync_run_id": str(run_id)}),
        },
    )
    await conn.execute(
        text(
            """
            UPDATE knowledge_sources
            SET last_sync_run_id = :run_id,
                last_sync_status = 'received',
                updated_at = now()
            WHERE id = :source_id AND tenant_id = :tenant_id AND knowledge_base_id = :kb_id
            """
        ),
        {"run_id": str(run_id), "source_id": source_id, "tenant_id": tenant_id, "kb_id": knowledge_base_id},
    )
    return job_id, run_id


async def get_job(conn: AsyncConnection, job_id: str) -> dict[str, Any] | None:
    result = await conn.execute(text("SELECT * FROM ingestion_jobs WHERE id = :id"), {"id": job_id})
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def claim_next_job(
    conn: AsyncConnection,
    *,
    lease_id: str | None = None,
    allowed_kinds: list[str] | tuple[str, ...] | None = None,
    lease_seconds: int = 180,
) -> dict[str, Any] | None:
    lease_id = lease_id or str(uuid.uuid4())
    kind_clause = ""
    params: dict[str, Any] = {"lease_id": lease_id, "lease_seconds": max(int(lease_seconds), 1)}
    if allowed_kinds:
        placeholders = []
        for index, kind in enumerate(allowed_kinds):
            key = f"kind_{index}"
            placeholders.append(f":{key}")
            params[key] = kind
        kind_clause = f"AND kind IN ({', '.join(placeholders)})"
    result = await conn.execute(
        text(
            f"""
            WITH candidate AS (
                SELECT id
                FROM ingestion_jobs
                WHERE cancel_requested = false
                  {kind_clause}
                  AND (
                    status = 'received'
                    OR (status = 'running' AND worker_lease_expires_at IS NOT NULL
                        AND worker_lease_expires_at < now())
                  )
                  AND (
                    kind <> 'document_delete'
                    OR config ->> 'purge_after' IS NULL
                    OR (config ->> 'purge_after')::timestamptz <= now()
                  )
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE ingestion_jobs AS job
            SET status = 'running',
                started_at = COALESCE(job.started_at, now()),
                worker_lease_id = :lease_id,
                worker_lease_expires_at = now() + make_interval(secs => :lease_seconds),
                worker_last_heartbeat_at = now(),
                updated_at = now()
            FROM candidate
            WHERE job.id = candidate.id
            RETURNING job.*
            """  # noqa: S608
        ),
        params,
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def heartbeat_job_lease(conn: AsyncConnection, *, job_id: str, lease_id: str, lease_seconds: int = 180) -> bool:
    result = await conn.execute(
        text(
            """
            UPDATE ingestion_jobs
            SET worker_lease_expires_at = now() + make_interval(secs => :lease_seconds),
                worker_last_heartbeat_at = now(), updated_at = now()
            WHERE id = :id AND status = 'running' AND worker_lease_id = :lease_id
            """
        ),
        {"id": job_id, "lease_id": lease_id, "lease_seconds": max(int(lease_seconds), 1)},
    )
    return bool(getattr(result, "rowcount", 0))


async def update_job(
    conn: AsyncConnection,
    job_id: str,
    *,
    status: JobStatus | None = None,
    progress: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    lease_id: str | None = None,
) -> None:
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"id": job_id}
    lease_id = lease_id or _worker_lease_context.get()
    lease_clause = ""
    if lease_id is not None:
        lease_clause = " AND worker_lease_id = :lease_id"
        params["lease_id"] = lease_id
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status.value
        if status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
            assignments.append("completed_at = now()")
            assignments.extend(
                ["worker_lease_id = NULL", "worker_lease_expires_at = NULL", "worker_last_heartbeat_at = NULL"]
            )
        if status == JobStatus.completed and error_code is None:
            assignments.append("error_code = NULL")
        if status == JobStatus.completed and error_message is None:
            assignments.append("error_message = NULL")
    if progress is not None:
        assignments.append("progress = CAST(:progress AS jsonb)")
        params["progress"] = json_dumps(progress)
    if checkpoint is not None:
        assignments.append("checkpoint = CAST(:checkpoint AS jsonb)")
        params["checkpoint"] = json_dumps(checkpoint)
    if error_code is not None:
        assignments.append("error_code = :error_code")
        params["error_code"] = error_code
    if error_message is not None:
        assignments.append("error_message = :error_message")
        params["error_message"] = error_message[:1000]
    result = await conn.execute(
        text(f"UPDATE ingestion_jobs SET {', '.join(assignments)} WHERE id = :id{lease_clause}"),  # noqa: S608
        params,
    )
    if lease_id is not None and not getattr(result, "rowcount", 0):
        raise StaleWorkerLeaseError(f"job lease lost: {job_id}")


async def request_cancel(conn: AsyncConnection, job_id: str) -> None:
    await conn.execute(
        text("UPDATE ingestion_jobs SET cancel_requested = true, updated_at = now() WHERE id = :id"),
        {"id": job_id},
    )


async def request_resume(conn: AsyncConnection, job_id: str) -> None:
    await conn.execute(
        text(
            """
            UPDATE ingestion_jobs
            SET cancel_requested = false,
                status = CASE WHEN status IN ('cancelled','failed') THEN 'received' ELSE status END,
                updated_at = now()
            WHERE id = :id AND status IN ('received', 'running')
            """
        ),
        {"id": job_id},
    )
    await conn.execute(
        text(
            """
            UPDATE ingestion_job_items
            SET status = 'received',
                stage = 'received',
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE job_id = :id AND status IN ('cancelled','failed')
            """
        ),
        {"id": job_id},
    )


async def create_upload_session(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    batch_id: str | None = None,
    filename: str,
    content_type: str,
    size_bytes: int,
    checksum_sha256: str,
    object_key: str,
    parser_profile: str,
    metadata: dict[str, Any],
    ttl_seconds: int,
) -> tuple[uuid.UUID, datetime]:
    session_id = new_uuid()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    await conn.execute(
        text(
            """
            INSERT INTO upload_sessions(
              id, tenant_id, knowledge_base_id, batch_id, status, filename, content_type, size_bytes,
              checksum_sha256, object_key, parser_profile, metadata, expires_at
            )
            VALUES (
              :id, :tenant_id, :kb_id, :batch_id, 'created', :filename, :content_type, :size_bytes,
              :checksum_sha256, :object_key, :parser_profile, CAST(:metadata AS jsonb), :expires_at
            )
            """
        ),
        {
            "id": str(session_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "batch_id": batch_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "checksum_sha256": checksum_sha256,
            "object_key": object_key,
            "parser_profile": parser_profile,
            "metadata": json_dumps(metadata),
            "expires_at": expires_at,
        },
    )
    return session_id, expires_at


async def create_upload_batch(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    total_items: int,
    metadata: dict[str, Any],
) -> uuid.UUID:
    batch_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO upload_batches(id, tenant_id, knowledge_base_id, status, total_items, metadata)
            VALUES (:id, :tenant_id, :kb_id, 'received', :total_items, CAST(:metadata AS jsonb))
            """
        ),
        {
            "id": str(batch_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "total_items": total_items,
            "metadata": json_dumps(metadata),
        },
    )
    return batch_id


async def get_upload_session(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    upload_session_id: str,
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM upload_sessions
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {"id": upload_session_id, "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_upload_batch_status(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    batch_id: str,
) -> dict[str, Any] | None:
    batch_result = await conn.execute(
        text(
            """
            SELECT id, tenant_id, knowledge_base_id, total_items, metadata, created_at, updated_at
            FROM upload_batches
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {"id": batch_id, "tenant_id": tenant_id},
    )
    batch = batch_result.mappings().first()
    if batch is None:
        return None

    items_result = await conn.execute(
        text(
            """
            SELECT s.id AS upload_session_id,
                   s.filename,
                   s.content_type,
                   s.size_bytes,
                   s.checksum_sha256,
                   s.status AS upload_status,
                   s.upload_completed_at,
                   i.document_id,
                   i.document_version_id,
                   i.job_id,
                   i.status AS item_status,
                   i.progress,
                   i.error_code,
                   i.error_message,
                   i.updated_at AS item_updated_at,
                   j.status AS job_status,
                   j.started_at AS job_started_at,
                   j.worker_last_heartbeat_at AS job_last_heartbeat_at
            FROM upload_sessions s
            LEFT JOIN ingestion_job_items i
              ON i.upload_session_id = s.id
             AND i.tenant_id = s.tenant_id
            LEFT JOIN ingestion_jobs j
              ON j.id = i.job_id
             AND j.tenant_id = s.tenant_id
            WHERE s.batch_id = :batch_id
              AND s.tenant_id = :tenant_id
            ORDER BY s.created_at, s.filename, s.id
            """
        ),
        {"batch_id": batch_id, "tenant_id": tenant_id},
    )
    items: list[dict[str, Any]] = []
    completed_items = 0
    failed_items = 0
    cancelled_items = 0
    for row in items_result.mappings():
        item = dict(row)
        status = str(item.get("job_status") or item.get("item_status") or item.get("upload_status") or "created")
        if status == "completed":
            completed_items += 1
        elif status == "failed":
            failed_items += 1
        elif status == "cancelled":
            cancelled_items += 1
        items.append(
            {
                "upload_session_id": str(item["upload_session_id"]),
                "filename": item["filename"],
                "content_type": item["content_type"],
                "size_bytes": item["size_bytes"],
                "checksum_sha256": item["checksum_sha256"],
                "status": status,
                "upload_completed_at": item.get("upload_completed_at"),
                "document_id": item.get("document_id"),
                "document_version_id": item.get("document_version_id"),
                "job_id": str(item["job_id"]) if item.get("job_id") is not None else None,
                "job_status": item.get("job_status"),
                "job_started_at": item.get("job_started_at"),
                "job_last_heartbeat_at": item.get("job_last_heartbeat_at"),
                "item_updated_at": item.get("item_updated_at"),
                "progress": item.get("progress") or {},
                "error_code": item.get("error_code"),
                "error_message": item.get("error_message"),
            }
        )

    total_items = max(int(batch["total_items"]), len(items))
    terminal_items = completed_items + failed_items + cancelled_items
    pending_items = max(0, total_items - terminal_items)
    if total_items > 0 and completed_items == total_items:
        status = "completed"
    elif total_items > 0 and cancelled_items == total_items:
        status = "cancelled"
    elif pending_items == 0 and failed_items > 0:
        status = "failed"
    elif terminal_items > 0 or any(item["status"] in {"uploaded", "completed", "running"} for item in items):
        status = "running"
    else:
        status = "received"
    return {
        "batch_id": str(batch["id"]),
        "knowledge_base_id": str(batch["knowledge_base_id"]),
        "status": status,
        "total_items": total_items,
        "completed_items": completed_items,
        "failed_items": failed_items,
        "cancelled_items": cancelled_items,
        "pending_items": pending_items,
        "items": items,
    }


async def create_document_upload_records(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    upload_session: dict[str, Any],
    document_id: str,
    document_version_id: str,
    content_hash: str,
    metadata: dict[str, Any],
) -> tuple[uuid.UUID, str]:
    job_id = new_uuid()
    existing_batch_id = upload_session.get("batch_id")
    batch_id = uuid.UUID(str(existing_batch_id)) if existing_batch_id is not None else new_uuid()
    item_id = new_uuid()
    source_id = stable_uuid([tenant_id, knowledge_base_id, "direct_upload"])
    filename = str(upload_session["filename"])
    object_key = str(upload_session["object_key"])
    now_payload = json_dumps(
        {"stage": "received", "documents_total": 1, "documents_completed": 0, "documents_failed": 0}
    )
    existing_result = await conn.execute(
        text(
            """
            SELECT status, content_hash, tenant_id, knowledge_base_id
            FROM document_versions
            WHERE id = :id
            FOR UPDATE
            """
        ),
        {"id": document_version_id, "tenant_id": tenant_id, "kb_id": knowledge_base_id},
    )
    existing_row = existing_result.mappings().first()
    existing_version = dict(existing_row) if existing_row is not None else None
    if existing_version is not None and (
        str(existing_version.get("tenant_id")) != tenant_id
        or str(existing_version.get("knowledge_base_id")) != knowledge_base_id
    ):
        raise DocumentVersionLifecycleError("DOCUMENT_VERSION_CONFLICT")
    if existing_version is not None and str(existing_version.get("content_hash")) != content_hash:
        raise DocumentVersionLifecycleError("DOCUMENT_VERSION_CONFLICT")
    existing_status = str(existing_version.get("status")) if existing_version is not None else None
    if existing_status not in {None, "published"}:
        code = (
            "DOCUMENT_VERSION_IN_PROGRESS"
            if existing_status in {"received", "validating", "parsing", "normalized", "indexing"}
            else "DOCUMENT_VERSION_REPROCESS_REQUIRED"
        )
        raise DocumentVersionLifecycleError(code)
    await conn.execute(
        text(
            """
            INSERT INTO knowledge_sources(id, tenant_id, knowledge_base_id, kind, name, config, metadata)
            VALUES (
              :id, :tenant_id, :kb_id, 'direct_upload', 'Direct Uploads',
              CAST(:config AS jsonb), CAST(:metadata AS jsonb)
            )
            ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                config = knowledge_sources.config || EXCLUDED.config,
                metadata = knowledge_sources.metadata || EXCLUDED.metadata,
                updated_at = now()
            """
        ),
        {
            "id": str(source_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "config": json_dumps({"source_contract": "upload_sessions_v1"}),
            "metadata": json_dumps({"created_by": "async_upload_pipeline"}),
        },
    )
    if existing_batch_id is None:
        await conn.execute(
            text(
                """
                INSERT INTO upload_batches(id, tenant_id, knowledge_base_id, status, total_items, metadata)
                VALUES (:id, :tenant_id, :kb_id, 'received', 1, CAST(:metadata AS jsonb))
                """
            ),
            {"id": str(batch_id), "tenant_id": tenant_id, "kb_id": knowledge_base_id, "metadata": json_dumps({})},
        )
    await conn.execute(
        text(
            """
            UPDATE upload_sessions
            SET status = 'completed',
                batch_id = :batch_id,
                upload_completed_at = now(),
                updated_at = now()
            WHERE id = :session_id AND tenant_id = :tenant_id
            """
        ),
        {"batch_id": str(batch_id), "session_id": upload_session["id"], "tenant_id": tenant_id},
    )
    if existing_status == "published":
        completed_progress = json_dumps(
            {
                "stage": "deduplicated",
                "documents_total": 1,
                "documents_completed": 1,
                "documents_failed": 0,
            }
        )
        await conn.execute(
            text(
                """
                INSERT INTO ingestion_jobs(
                  id, tenant_id, knowledge_base_id, kind, status, config, progress,
                  started_at, completed_at, worker_last_heartbeat_at
                )
                VALUES (
                  :id, :tenant_id, :kb_id, 'document_upload', 'completed',
                  CAST(:config AS jsonb), CAST(:progress AS jsonb), now(), now(), now()
                )
                """
            ),
            {
                "id": str(job_id),
                "tenant_id": tenant_id,
                "kb_id": knowledge_base_id,
                "config": json_dumps(
                    {
                        "batch_id": str(batch_id),
                        "upload_session_id": str(upload_session["id"]),
                        "deduplicated": True,
                    }
                ),
                "progress": completed_progress,
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO ingestion_job_items(
                  id, job_id, tenant_id, knowledge_base_id, document_id, document_version_id,
                  upload_session_id, status, stage, progress, claimed_at, completed_at
                )
                VALUES (
                  :id, :job_id, :tenant_id, :kb_id, :document_id, :document_version_id,
                  :upload_session_id, 'completed', 'deduplicated', CAST(:progress AS jsonb), now(), now()
                )
                """
            ),
            {
                "id": str(item_id),
                "job_id": str(job_id),
                "tenant_id": tenant_id,
                "kb_id": knowledge_base_id,
                "document_id": document_id,
                "document_version_id": document_version_id,
                "upload_session_id": str(upload_session["id"]),
                "progress": json_dumps({"stage": "deduplicated"}),
            },
        )
        return job_id, "completed"
    await conn.execute(
        text(
            """
            INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata)
            VALUES (:id, :tenant_id, :kb_id, 'upload_document', :title, :source_uri, CAST(:metadata AS jsonb))
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                metadata = documents.metadata || EXCLUDED.metadata,
                updated_at = now()
            """
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "title": filename,
            "source_uri": f"upload://{document_id}",
            "metadata": json_dumps(
                {
                    "filename": filename,
                    "current_version_id": document_version_id,
                    "knowledge_source_id": str(source_id),
                    "source_kind": "direct_upload",
                }
            ),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO document_versions(
              id, document_id, tenant_id, knowledge_base_id, version_ordinal, status,
              content_hash, original_artifact_key, parser_options, source_metadata,
              public_metadata, uploaded_at, upload_completed_at
            )
            VALUES (
              :id, :document_id, :tenant_id, :kb_id, 1, 'received',
              :content_hash, :original_artifact_key, CAST(:parser_options AS jsonb),
              CAST(:source_metadata AS jsonb), CAST(:public_metadata AS jsonb), now(), now()
            )
            ON CONFLICT (id) DO UPDATE
            SET status = 'received',
                public_metadata = EXCLUDED.public_metadata,
                upload_completed_at = now(),
                updated_at = now()
            """
        ),
        {
            "id": document_version_id,
            "document_id": document_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "content_hash": content_hash,
            "original_artifact_key": object_key,
            "parser_options": json_dumps({"profile": upload_session.get("parser_profile") or "standard"}),
            "source_metadata": json_dumps(metadata),
            "public_metadata": json_dumps(
                {
                    "filename": filename,
                    "content_type": upload_session.get("content_type"),
                    "size_bytes": upload_session.get("size_bytes"),
                    "checksum_sha256": content_hash,
                }
            ),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO document_artifacts(
              id, tenant_id, knowledge_base_id, document_id, document_version_id,
              kind, object_key, content_type, size_bytes, checksum_sha256, metadata
            )
            VALUES (
              :id, :tenant_id, :kb_id, :document_id, :document_version_id, 'original',
              :object_key, :content_type, :size_bytes, :checksum_sha256, CAST(:metadata AS jsonb)
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "object_key": object_key,
            "content_type": upload_session.get("content_type"),
            "size_bytes": upload_session.get("size_bytes"),
            "checksum_sha256": content_hash,
            "metadata": json_dumps({"artifact_contract": "document_artifact_v1"}),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress)
            VALUES (:id, :tenant_id, :kb_id, 'document_upload', 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb))
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "config": json_dumps({"batch_id": str(batch_id), "upload_session_id": str(upload_session["id"])}),
            "progress": now_payload,
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_job_items(
              id, job_id, tenant_id, knowledge_base_id, document_id, document_version_id,
              upload_session_id, status, stage, progress
            )
            VALUES (
              :id, :job_id, :tenant_id, :kb_id, :document_id, :document_version_id,
              :upload_session_id, 'received', 'received', CAST(:progress AS jsonb)
            )
            ON CONFLICT (job_id, document_version_id) DO NOTHING
            """
        ),
        {
            "id": str(item_id),
            "job_id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "upload_session_id": str(upload_session["id"]),
            "progress": json_dumps({"stage": "received"}),
        },
    )
    return job_id, "received"


async def create_knowledge_source(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    kind: str,
    name: str,
    config: dict[str, Any],
    encrypted_credentials: dict[str, Any],
    metadata: dict[str, Any],
    refresh_interval_seconds: int | None,
) -> uuid.UUID:
    source_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO knowledge_sources(
              id, tenant_id, knowledge_base_id, kind, name, config, encrypted_credentials,
              metadata, refresh_interval_seconds, next_sync_at
            )
            VALUES (
              :id, :tenant_id, :kb_id, :kind, :name, CAST(:config AS jsonb),
              CAST(:encrypted_credentials AS jsonb), CAST(:metadata AS jsonb),
              :refresh_interval_seconds,
              CASE WHEN CAST(:refresh_interval_seconds AS int) IS NULL THEN NULL ELSE now() END
            )
            """
        ),
        {
            "id": str(source_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "kind": kind,
            "name": name,
            "config": json_dumps(config),
            "encrypted_credentials": json_dumps(encrypted_credentials),
            "metadata": json_dumps(metadata),
            "refresh_interval_seconds": refresh_interval_seconds,
        },
    )
    return source_id


async def list_knowledge_sources_public(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, tenant_id, knowledge_base_id, kind, name, status, config, metadata,
                   refresh_interval_seconds, last_sync_run_id, last_sync_status,
                   last_synced_at, next_sync_at, created_at, updated_at
            FROM knowledge_sources
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id
            ORDER BY created_at DESC
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id},
    )
    return [dict(row) for row in result.mappings()]


async def get_knowledge_source(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    include_credentials: bool = False,
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT id, tenant_id, knowledge_base_id, kind, name, status, config,
                   encrypted_credentials, metadata, refresh_interval_seconds, sync_cursor,
                   last_sync_run_id, last_sync_status, last_synced_at, next_sync_at,
                   created_at, updated_at
            FROM knowledge_sources
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND id = :source_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "source_id": source_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    payload = dict(row)
    if not include_credentials:
        payload.pop("encrypted_credentials", None)
    return payload


async def update_knowledge_source(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    name: str | None = None,
    status: str | None = None,
    config: dict[str, Any] | None = None,
    encrypted_credentials: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    refresh_interval_seconds: int | None = None,
    refresh_interval_supplied: bool = False,
) -> None:
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "source_id": source_id}
    if name is not None:
        assignments.append("name = :name")
        params["name"] = name
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status
    if config is not None:
        assignments.append("config = CAST(:config AS jsonb)")
        params["config"] = json_dumps(config)
    if encrypted_credentials is not None:
        assignments.append("encrypted_credentials = CAST(:encrypted_credentials AS jsonb)")
        params["encrypted_credentials"] = json_dumps(encrypted_credentials)
    if metadata is not None:
        assignments.append("metadata = CAST(:metadata AS jsonb)")
        params["metadata"] = json_dumps(metadata)
    if refresh_interval_supplied:
        assignments.append("refresh_interval_seconds = :refresh_interval_seconds")
        assignments.append(
            "next_sync_at = CASE "
            "WHEN CAST(:refresh_interval_seconds AS int) IS NULL THEN NULL "
            "ELSE COALESCE(next_sync_at, now()) END"
        )
        params["refresh_interval_seconds"] = refresh_interval_seconds
    await conn.execute(
        text(
            f"""
            UPDATE knowledge_sources
            SET {", ".join(assignments)}
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND id = :source_id
            """  # noqa: S608
        ),
        params,
    )


async def update_knowledge_source_document_access_default(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    document_access: dict[str, Any],
) -> None:
    access = normalize_document_access(document_access)
    await conn.execute(
        text(
            """
            UPDATE knowledge_sources
            SET metadata = metadata || jsonb_build_object('document_access_default', CAST(:document_access AS jsonb)),
                updated_at = now()
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND id = :source_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "source_id": source_id,
            "document_access": json_dumps(access),
        },
    )


async def list_source_active_document_refs(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT s.document_id, s.document_version_id
            FROM source_document_states s
            JOIN documents d
              ON d.id = s.document_id
             AND d.tenant_id = s.tenant_id
             AND d.knowledge_base_id = s.knowledge_base_id
            WHERE s.tenant_id = :tenant_id
              AND s.knowledge_base_id = :kb_id
              AND s.source_id = :source_id
              AND s.status = 'active'
              AND d.lifecycle_state = 'active'
              AND s.document_id IS NOT NULL
            ORDER BY s.updated_at DESC
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "source_id": source_id},
    )
    return [dict(row) for row in result.mappings()]


async def get_source_sync_run_public(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT id, source_id, knowledge_base_id, mode, status, cursor_before, cursor_after,
                   checkpoint, stats, error_code, error_message, started_at, completed_at,
                   created_at, updated_at
            FROM source_sync_runs
            WHERE tenant_id = :tenant_id AND id = :run_id
            """
        ),
        {"tenant_id": tenant_id, "run_id": run_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def list_source_document_states(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM source_document_states
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND source_id = :source_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "source_id": source_id},
    )
    return [dict(row) for row in result.mappings()]


async def get_source_document_state(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    external_id: str,
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM source_document_states
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND source_id = :source_id
              AND external_id = :external_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "source_id": source_id, "external_id": external_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def create_source_document_ingestion_item(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    sync_run_id: str,
    job_id: str,
    external_id: str,
    title: str,
    source_uri: str,
    source_url: str,
    source_version: str,
    content_hash: str,
    object_key: str,
    content_type: str,
    size_bytes: int,
    parser_profile: str,
    metadata: dict[str, Any],
) -> tuple[str, str, uuid.UUID]:
    document_id = f"src:{stable_hash([tenant_id, knowledge_base_id, source_id, external_id], 32)}"
    document_version_id = "docv:" + stable_hash(
        [document_id, source_version, content_hash, parser_profile, "normalized_document_v1"],
        32,
    )
    item_id = new_uuid()
    source_metadata = {
        **metadata,
        "source_id": source_id,
        "source_external_id": external_id,
        "source_version": source_version,
        "source_uri": source_uri,
        "source_url": source_url,
    }
    document_access = normalize_document_access(metadata.get("document_access"))
    document_access_origin = str(metadata.get("document_access_origin") or "")
    await conn.execute(
        text(
            """
            INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata)
            VALUES (:id, :tenant_id, :kb_id, 'external_source', :title, :source_uri, CAST(:metadata AS jsonb))
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                source_uri = EXCLUDED.source_uri,
                lifecycle_state = 'active',
                deleted_at = NULL,
                purge_after = NULL,
                deletion_reason = NULL,
                metadata = documents.metadata || EXCLUDED.metadata,
                updated_at = now()
            """
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "title": title,
            "source_uri": source_uri,
            "metadata": json_dumps(
                {
                    "knowledge_source_id": source_id,
                    "source_kind": "external_source",
                    "source_external_id": external_id,
                    "source_url": source_url,
                    "current_version_id": document_version_id,
                    "document_access": document_access,
                    "document_access_origin": document_access_origin,
                }
            ),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO document_versions(
              id, document_id, tenant_id, knowledge_base_id, version_ordinal, status,
              content_hash, original_artifact_key, parser_options, source_metadata,
              public_metadata, uploaded_at, upload_completed_at
            )
            VALUES (
              :id, :document_id, :tenant_id, :kb_id,
              COALESCE(
                (SELECT max(version_ordinal) + 1 FROM document_versions WHERE document_id = :document_id),
                1
              ),
              'received', :content_hash, :original_artifact_key,
              CAST(:parser_options AS jsonb), CAST(:source_metadata AS jsonb),
              CAST(:public_metadata AS jsonb), now(), now()
            )
            ON CONFLICT (id) DO UPDATE
            SET status = 'received',
                lifecycle_state = 'active',
                deleted_at = NULL,
                purge_after = NULL,
                original_artifact_key = EXCLUDED.original_artifact_key,
                public_metadata = EXCLUDED.public_metadata,
                updated_at = now()
            """
        ),
        {
            "id": document_version_id,
            "document_id": document_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "content_hash": content_hash,
            "original_artifact_key": object_key,
            "parser_options": json_dumps({"profile": parser_profile}),
            "source_metadata": json_dumps(source_metadata),
            "public_metadata": json_dumps(
                {
                    "filename": title,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                    "checksum_sha256": content_hash,
                    "source_url": source_url,
                    "source_external_id": external_id,
                }
            ),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO document_artifacts(
              id, tenant_id, knowledge_base_id, document_id, document_version_id,
              kind, object_key, content_type, size_bytes, checksum_sha256, metadata
            )
            VALUES (
              :id, :tenant_id, :kb_id, :document_id, :document_version_id, 'original',
              :object_key, :content_type, :size_bytes, :checksum_sha256, CAST(:metadata AS jsonb)
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "object_key": object_key,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "checksum_sha256": content_hash,
            "metadata": json_dumps({"artifact_contract": "source_document_artifact_v1", "sync_run_id": sync_run_id}),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_job_items(
              id, job_id, tenant_id, knowledge_base_id, document_id, document_version_id,
              status, stage, progress
            )
            VALUES (
              :id, :job_id, :tenant_id, :kb_id, :document_id, :document_version_id,
              'received', 'received', CAST(:progress AS jsonb)
            )
            ON CONFLICT (job_id, document_version_id) DO NOTHING
            """
        ),
        {
            "id": str(item_id),
            "job_id": job_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "progress": json_dumps({"stage": "received", "sync_run_id": sync_run_id}),
        },
    )
    return document_id, document_version_id, item_id


async def upsert_source_document_state(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    sync_run_id: str,
    external_id: str,
    title: str,
    source_uri: str,
    source_url: str,
    source_version: str,
    content_hash: str,
    document_id: str,
    document_version_id: str,
    metadata: dict[str, Any],
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO source_document_states(
              tenant_id, knowledge_base_id, source_id, external_id, source_uri, source_url,
              title, source_version, content_hash, document_id, document_version_id,
              last_sync_run_id, status, metadata, last_seen_at
            )
            VALUES (
              :tenant_id, :kb_id, :source_id, :external_id, :source_uri, :source_url,
              :title, :source_version, :content_hash, :document_id, :document_version_id,
              :sync_run_id, 'active', CAST(:metadata AS jsonb), now()
            )
            ON CONFLICT (tenant_id, knowledge_base_id, source_id, external_id) DO UPDATE
            SET source_uri = EXCLUDED.source_uri,
                source_url = EXCLUDED.source_url,
                title = EXCLUDED.title,
                source_version = EXCLUDED.source_version,
                content_hash = EXCLUDED.content_hash,
                document_id = EXCLUDED.document_id,
                document_version_id = EXCLUDED.document_version_id,
                last_sync_run_id = EXCLUDED.last_sync_run_id,
                status = 'active',
                metadata = EXCLUDED.metadata,
                last_seen_at = now(),
                deleted_at = NULL,
                tombstone_version = NULL,
                updated_at = now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "source_id": source_id,
            "external_id": external_id,
            "source_uri": source_uri,
            "source_url": source_url,
            "title": title,
            "source_version": source_version,
            "content_hash": content_hash,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "sync_run_id": sync_run_id,
            "metadata": json_dumps(metadata),
        },
    )
    await conn.execute(
        text(
            """
            UPDATE documents
            SET metadata = metadata || CAST(:metadata AS jsonb),
                lifecycle_state = 'active',
                deleted_at = NULL,
                purge_after = NULL,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND id = :document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "metadata": json_dumps({"current_version_id": document_version_id, "source_url": source_url}),
        },
    )


async def mark_source_document_tombstone(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    sync_run_id: str,
    external_id: str,
    tombstone_version: str,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    state = await get_source_document_state(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_id=source_id,
        external_id=external_id,
    )
    if state is None:
        return None
    await conn.execute(
        text(
            """
            UPDATE source_document_states
            SET status = 'deleted',
                deleted_at = COALESCE(deleted_at, now()),
                tombstone_version = :tombstone_version,
                last_sync_run_id = :sync_run_id,
                metadata = metadata || CAST(:metadata AS jsonb),
                updated_at = now()
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id
              AND source_id = :source_id AND external_id = :external_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "source_id": source_id,
            "external_id": external_id,
            "tombstone_version": tombstone_version,
            "sync_run_id": sync_run_id,
            "metadata": json_dumps(metadata),
        },
    )
    return state


async def update_source_sync_run(
    conn: AsyncConnection,
    *,
    run_id: str,
    status: str,
    cursor_after: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    assignments = ["status = :status", "updated_at = now()"]
    params: dict[str, Any] = {"run_id": run_id, "status": status}
    if status == "running":
        assignments.append("started_at = COALESCE(started_at, now())")
    if status in {"completed", "failed", "cancelled"}:
        assignments.append("completed_at = now()")
    for key, value in {
        "cursor_after": cursor_after,
        "checkpoint": checkpoint,
        "stats": stats,
    }.items():
        if value is not None:
            assignments.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json_dumps(value)
    if error_code is not None:
        assignments.append("error_code = :error_code")
        params["error_code"] = error_code[:120]
    if error_message is not None:
        assignments.append("error_message = :error_message")
        params["error_message"] = error_message[:1000]
    await conn.execute(
        text(f"UPDATE source_sync_runs SET {', '.join(assignments)} WHERE id = :run_id"),  # noqa: S608
        params,
    )


async def finish_knowledge_source_sync(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    run_id: str,
    status: str,
    cursor_after: dict[str, Any],
    refresh_interval_seconds: int | None,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE knowledge_sources
            SET sync_cursor = CAST(:cursor_after AS jsonb),
                last_sync_run_id = :run_id,
                last_sync_status = :status,
                last_synced_at = CASE WHEN :status = 'completed' THEN now() ELSE last_synced_at END,
                next_sync_at = CASE
                  WHEN CAST(:refresh_interval_seconds AS int) IS NULL THEN NULL
                  WHEN :status = 'completed' THEN now() + make_interval(secs => CAST(:refresh_interval_seconds AS int))
                  ELSE now() + interval '5 minutes'
                END,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND id = :source_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "source_id": source_id,
            "run_id": run_id,
            "status": status,
            "cursor_after": json_dumps(cursor_after),
            "refresh_interval_seconds": refresh_interval_seconds,
        },
    )


async def enqueue_due_source_sync_jobs(conn: AsyncConnection) -> int:
    result = await conn.execute(
        text(
            """
            SELECT id, tenant_id, knowledge_base_id, sync_cursor
            FROM knowledge_sources
            WHERE status = 'active'
              AND refresh_interval_seconds IS NOT NULL
              AND next_sync_at IS NOT NULL
              AND next_sync_at <= now()
            ORDER BY next_sync_at
            LIMIT 10
            FOR UPDATE SKIP LOCKED
            """
        )
    )
    rows = [dict(row) for row in result.mappings()]
    created = 0
    for row in rows:
        before = dict(row.get("sync_cursor") or {})
        await create_source_sync_job(
            conn,
            tenant_id=str(row["tenant_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            source_id=str(row["id"]),
            mode="incremental",
            cursor_before=before,
        )
        created += 1
    return created


async def claim_next_ingestion_job_item(conn: AsyncConnection, job_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM ingestion_job_items
            WHERE job_id = :job_id
              AND (
                status = 'received'
                AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                OR (status = 'running' AND claimed_at < now() - interval '30 minutes')
              )
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"job_id": job_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    await conn.execute(
        text(
            """
            UPDATE ingestion_job_items
            SET status = 'running',
                attempts = attempts + 1,
                claimed_at = now(),
                next_attempt_at = NULL,
                updated_at = now()
            WHERE id = :id AND status IN ('received', 'running')
        """
        ),
        {"id": row["id"]},
    )
    claimed = dict(row)
    claimed["attempts"] = int(claimed.get("attempts") or 0) + 1
    return claimed


async def next_ingestion_job_item_retry_delay_seconds(conn: AsyncConnection, job_id: str) -> float | None:
    """Return the bounded wait until a received item becomes claimable."""
    result = await conn.execute(
        text(
            """
            SELECT EXTRACT(EPOCH FROM min(next_attempt_at) - now()) AS delay_seconds
            FROM ingestion_job_items
            WHERE job_id = :job_id AND status = 'received' AND next_attempt_at IS NOT NULL
            """
        ),
        {"job_id": job_id},
    )
    value = result.scalar()
    return max(0.0, float(value)) if value is not None else None


async def update_ingestion_job_item(
    conn: AsyncConnection,
    item_id: str,
    *,
    status: JobStatus | None = None,
    stage: str | None = None,
    progress: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    retry_after_seconds: float | None = None,
) -> None:
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"id": item_id}
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status.value
        if status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
            assignments.append("completed_at = now()")
        if status == JobStatus.completed and error_code is None:
            assignments.append("error_code = NULL")
        if status == JobStatus.completed and error_message is None:
            assignments.append("error_message = NULL")
    if stage is not None:
        assignments.append("stage = :stage")
        params["stage"] = stage
    if progress is not None:
        assignments.append("progress = CAST(:progress AS jsonb)")
        params["progress"] = json_dumps(progress)
    if checkpoint is not None:
        assignments.append("checkpoint = CAST(:checkpoint AS jsonb)")
        params["checkpoint"] = json_dumps(checkpoint)
    if error_code is not None:
        assignments.append("error_code = :error_code")
        params["error_code"] = error_code
    if error_message is not None:
        assignments.append("error_message = :error_message")
        params["error_message"] = error_message[:1000]
    if retry_after_seconds is not None:
        assignments.append("next_attempt_at = now() + make_interval(secs => :retry_after_seconds)")
        params["retry_after_seconds"] = max(0.0, float(retry_after_seconds))
    await conn.execute(
        text(f"UPDATE ingestion_job_items SET {', '.join(assignments)} WHERE id = :id"),  # noqa: S608
        params,
    )


async def update_document_version(
    conn: AsyncConnection,
    document_version_id: str,
    *,
    status: str,
    normalized_hash: str | None = None,
    normalized_artifact_key: str | None = None,
    parser_route: str | None = None,
    parser_name: str | None = None,
    parser_version: str | None = None,
    parser_options: dict[str, Any] | None = None,
    extracted_metadata: dict[str, Any] | None = None,
    public_metadata: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> None:
    assignments = ["status = :status", "updated_at = now()"]
    params: dict[str, Any] = {"id": document_version_id, "status": status}
    if status == "normalized":
        assignments.append("ingested_at = now()")
    if status == "published":
        assignments.append("published_at = now()")
    optional_values: dict[str, Any] = {
        "normalized_hash": normalized_hash,
        "normalized_artifact_key": normalized_artifact_key,
        "parser_route": parser_route,
        "parser_name": parser_name,
        "parser_version": parser_version,
    }
    for key, value in optional_values.items():
        if value is not None:
            assignments.append(f"{key} = :{key}")
            params[key] = value
    json_values = {
        "parser_options": parser_options,
        "extracted_metadata": extracted_metadata,
        "public_metadata": public_metadata,
        "validation": validation,
        "warnings": warnings,
    }
    for key, value in json_values.items():
        if value is not None:
            assignments.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json_dumps(value)
    await conn.execute(
        text(f"UPDATE document_versions SET {', '.join(assignments)} WHERE id = :id"),  # noqa: S608
        params,
    )


async def insert_document_artifact(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str,
    kind: str,
    object_key: str,
    content_type: str,
    size_bytes: int,
    checksum_sha256: str,
    metadata: dict[str, Any],
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO document_artifacts(
              id, tenant_id, knowledge_base_id, document_id, document_version_id,
              kind, object_key, content_type, size_bytes, checksum_sha256, metadata
            )
            VALUES (
              :id, :tenant_id, :kb_id, :document_id, :document_version_id,
              :kind, :object_key, :content_type, :size_bytes, :checksum_sha256,
              CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "kind": kind,
            "object_key": object_key,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "checksum_sha256": checksum_sha256,
            "metadata": json_dumps(metadata),
        },
    )


async def summarize_ingestion_job_items(conn: AsyncConnection, job_id: str) -> dict[str, int]:
    result = await conn.execute(
        text(
            """
            SELECT status, count(*) AS count
            FROM ingestion_job_items
            WHERE job_id = :job_id
            GROUP BY status
            """
        ),
        {"job_id": job_id},
    )
    counts = {str(row["status"]): int(row["count"]) for row in result.mappings()}
    return {
        "total": sum(counts.values()),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "running": counts.get("running", 0),
        "received": counts.get("received", 0),
    }


async def load_document_version(
    conn: AsyncConnection, tenant_id: str, document_version_id: str
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM document_versions
            WHERE tenant_id = :tenant_id AND id = :id
            """
        ),
        {"tenant_id": tenant_id, "id": document_version_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_document_public(conn: AsyncConnection, tenant_id: str, document_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT d.id, d.knowledge_base_id, d.title, d.source_type,
                   COALESCE(d.metadata ->> 'source_kind', d.source_type) AS source_kind,
                   d.metadata, d.created_at, d.updated_at,
                   d.lifecycle_state, d.deleted_at, d.purge_after,
                   v.id AS current_version_id, v.status AS version_status, v.status AS status,
                   v.public_metadata ->> 'filename' AS filename, v.public_metadata,
                   v.parser_route, v.parser_name, v.parser_version, v.content_hash, v.normalized_hash,
                   v.uploaded_at, v.upload_completed_at, v.ingested_at, v.published_at
            FROM documents d
            LEFT JOIN LATERAL (
              SELECT *
              FROM document_versions
              WHERE document_id = d.id AND tenant_id = d.tenant_id
              ORDER BY created_at DESC
              LIMIT 1
            ) v ON true
            WHERE d.tenant_id = :tenant_id
              AND d.id = :document_id
              AND d.lifecycle_state = 'active'
            """
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_document_lifecycle(conn: AsyncConnection, tenant_id: str, document_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT d.id, d.knowledge_base_id, d.lifecycle_state, d.deleted_at, d.purge_after,
                   d.deleted_by_user_id, d.deletion_reason,
                   d.metadata ->> 'current_version_id' AS current_version_id
            FROM documents d
            WHERE d.tenant_id = :tenant_id AND d.id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def soft_delete_document(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    deleted_by_user_id: str,
    purge_after: datetime,
    deletion_reason: str,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE documents
            SET lifecycle_state = CASE
                    WHEN lifecycle_state = 'deleted' THEN lifecycle_state
                    ELSE 'deleting'
                END,
                deleted_at = COALESCE(deleted_at, now()),
                purge_after = COALESCE(purge_after, :purge_after),
                deleted_by_user_id = COALESCE(deleted_by_user_id, :deleted_by_user_id),
                deletion_reason = COALESCE(deletion_reason, :deletion_reason),
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND id = :document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "deleted_by_user_id": deleted_by_user_id,
            "purge_after": purge_after,
            "deletion_reason": deletion_reason[:200],
        },
    )
    await conn.execute(
        text(
            """
            UPDATE document_versions
            SET lifecycle_state = CASE
                    WHEN lifecycle_state = 'deleted' THEN lifecycle_state
                    ELSE 'deleting'
                END,
                deleted_at = COALESCE(deleted_at, now()),
                purge_after = COALESCE(purge_after, :purge_after),
                deleted_by_user_id = COALESCE(deleted_by_user_id, :deleted_by_user_id),
                deletion_reason = COALESCE(deletion_reason, :deletion_reason),
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "deleted_by_user_id": deleted_by_user_id,
            "purge_after": purge_after,
            "deletion_reason": deletion_reason[:200],
        },
    )
    await mark_document_chunks_deleted(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
    )


async def mark_document_chunks_deleted(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
) -> int:
    result = await conn.execute(
        text(
            """
            UPDATE chunks
            SET publication_status = 'deleted',
                metadata = metadata || CAST(:metadata AS jsonb)
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
              AND publication_status <> 'deleted'
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "metadata": json_dumps({"publication_status": "deleted"}),
        },
    )
    return int(result.rowcount or 0)


async def mark_document_version_chunks_deleted(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_version_id: str,
) -> int:
    result = await conn.execute(
        text(
            """
            UPDATE chunks
            SET publication_status = 'deleted',
                metadata = metadata || CAST(:metadata AS jsonb)
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_version_id = :document_version_id
              AND publication_status <> 'deleted'
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_version_id": document_version_id,
            "metadata": json_dumps({"publication_status": "deleted"}),
        },
    )
    return int(result.rowcount or 0)


async def list_document_artifact_keys(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
) -> list[str]:
    result = await conn.execute(
        text(
            """
            SELECT object_key
            FROM document_artifacts
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "document_id": document_id},
    )
    return [str(row["object_key"]) for row in result.mappings()]


async def mark_document_purge_failed(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    safe_error_code: str,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE documents
            SET lifecycle_state = 'purge_failed',
                metadata = metadata || CAST(:metadata AS jsonb),
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND id = :document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "metadata": json_dumps({"purge_error_code": safe_error_code[:120]}),
        },
    )
    await conn.execute(
        text(
            """
            UPDATE document_versions
            SET lifecycle_state = 'purge_failed',
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "document_id": document_id},
    )


async def mark_document_purged(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
) -> int:
    chunk_result = await conn.execute(
        text(
            """
            DELETE FROM chunks
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "document_id": document_id},
    )
    await conn.execute(
        text(
            """
            DELETE FROM document_artifacts
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "document_id": document_id},
    )
    await conn.execute(
        text(
            """
            UPDATE document_versions
            SET lifecycle_state = 'deleted',
                original_artifact_key = 'purged',
                normalized_artifact_key = NULL,
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "document_id": document_id},
    )
    await conn.execute(
        text(
            """
            UPDATE documents
            SET lifecycle_state = 'deleted',
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "document_id": document_id},
    )
    return int(chunk_result.rowcount or 0)


async def list_document_versions_public(
    conn: AsyncConnection,
    tenant_id: str,
    document_id: str,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, document_id, status, content_hash, normalized_hash, parser_route,
                   parser_name, parser_version, public_metadata, uploaded_at,
                   upload_completed_at, ingested_at, published_at, created_at, updated_at
            FROM document_versions
            WHERE tenant_id = :tenant_id AND document_id = :document_id
            ORDER BY created_at DESC
            """
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    )
    return [dict(row) for row in result.mappings()]


async def create_reprocess_job(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str,
) -> uuid.UUID:
    job_id = new_uuid()
    item_id = new_uuid()
    await conn.execute(
        text(
            """
            UPDATE document_versions
            SET status = 'received', updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :document_version_id
            """
        ),
        {"tenant_id": tenant_id, "document_version_id": document_version_id},
    )
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress)
            VALUES (:id, :tenant_id, :kb_id, 'document_upload', 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb))
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "config": json_dumps({"document_id": document_id, "document_version_id": document_version_id}),
            "progress": json_dumps({"stage": "received", "documents_total": 1}),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_job_items(
              id, job_id, tenant_id, knowledge_base_id, document_id, document_version_id,
              status, stage, progress
            )
            VALUES (
              :id, :job_id, :tenant_id, :kb_id, :document_id, :document_version_id,
              'received', 'received', CAST(:progress AS jsonb)
            )
            """
        ),
        {
            "id": str(item_id),
            "job_id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "progress": json_dumps({"stage": "received"}),
        },
    )
    return job_id


async def upsert_wiki_page_and_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    snapshot_id: str,
    page: WikiPage,
    chunks: list[Chunk],
) -> None:
    native_document_id = f"wiki:{snapshot_id}:{page.page_id}"
    document_id = scoped_id(
        "wiki-document",
        page.page_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_type="wikipedia_xml",
        snapshot_id=snapshot_id,
    )
    metadata = {
        "page_id": page.page_id,
        "revision_id": page.revision_id,
        "timestamp": page.timestamp,
        "redirect_target": page.redirect_target,
        "namespace": page.namespace,
        "snapshot_id": snapshot_id,
    }
    await conn.execute(
        text(
            """
            INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata,
                                  source_document_id, identity_scope)
            VALUES (:id, :tenant_id, :kb_id, 'wikipedia_xml', :title, :source_uri,
                    CAST(:metadata AS jsonb), :source_document_id, :identity_scope)
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                metadata = EXCLUDED.metadata,
                source_document_id = EXCLUDED.source_document_id,
                identity_scope = EXCLUDED.identity_scope,
                updated_at = now()
            """
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "title": page.title,
            "source_uri": f"wikipedia://{snapshot_id}/{page.page_id}",
            "metadata": json_dumps({**metadata, "source_document_id": native_document_id}),
            "source_document_id": native_document_id,
            "identity_scope": f"{tenant_id}:{knowledge_base_id}",
        },
    )
    legacy_document_id = str(metadata.get("source_document_id") or document_id)
    if legacy_document_id != document_id:
        await conn.execute(
            text(
                """
                INSERT INTO legacy_id_mappings(tenant_id, knowledge_base_id, entity_kind, legacy_id, scoped_id)
                VALUES (:tenant_id, :knowledge_base_id, 'document', :legacy_id, :scoped_id)
                ON CONFLICT (tenant_id, knowledge_base_id, entity_kind, legacy_id)
                DO UPDATE SET scoped_id = EXCLUDED.scoped_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "legacy_id": legacy_document_id,
                "scoped_id": document_id,
            },
        )
    await conn.execute(
        text(
            """
            INSERT INTO legacy_id_mappings(tenant_id, knowledge_base_id, entity_kind, legacy_id, scoped_id)
            VALUES (:tenant_id, :knowledge_base_id, 'document', :legacy_id, :scoped_id)
            ON CONFLICT (tenant_id, knowledge_base_id, entity_kind, legacy_id)
            DO UPDATE SET scoped_id = EXCLUDED.scoped_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "legacy_id": native_document_id,
            "scoped_id": document_id,
        },
    )
    for chunk in chunks:
        await upsert_chunk(conn, tenant_id=tenant_id, knowledge_base_id=knowledge_base_id, chunk=chunk)


async def upsert_chunk(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    chunk: Chunk,
) -> None:
    metadata = dict(chunk.metadata)
    chunk_ordinal = metadata.get("chunk_ordinal")
    locator = metadata.get("locator") if isinstance(metadata.get("locator"), dict) else {}
    await conn.execute(
        text(
            """
            INSERT INTO chunks(
              id, tenant_id, knowledge_base_id, document_id, page_id, revision_id, title,
              section_path, content, parent_chunk_id, prev_chunk_id, next_chunk_id,
              source_uri, source_url, embedding, content_hash, metadata,
              document_version_id, chunk_ordinal, locator, publication_status,
              source_chunk_id, content_unit_id, identity_scope
            )
            VALUES (
              :id, :tenant_id, :kb_id, :document_id, :page_id, :revision_id, :title,
              :section_path, :content, :parent_chunk_id, :prev_chunk_id, :next_chunk_id,
              :source_uri, :source_url, CAST(:embedding AS jsonb), :content_hash,
              CAST(:metadata AS jsonb), :document_version_id, :chunk_ordinal,
              CAST(:locator AS jsonb), :publication_status, :source_chunk_id,
              :content_unit_id, :identity_scope
            )
            ON CONFLICT (id) DO UPDATE
            SET content = EXCLUDED.content,
                prev_chunk_id = EXCLUDED.prev_chunk_id,
                next_chunk_id = EXCLUDED.next_chunk_id,
                embedding = EXCLUDED.embedding,
                content_hash = EXCLUDED.content_hash,
                metadata = EXCLUDED.metadata,
                document_version_id = EXCLUDED.document_version_id,
                chunk_ordinal = EXCLUDED.chunk_ordinal,
                locator = EXCLUDED.locator,
                publication_status = EXCLUDED.publication_status,
                source_chunk_id = EXCLUDED.source_chunk_id,
                content_unit_id = EXCLUDED.content_unit_id,
                identity_scope = EXCLUDED.identity_scope
            """
        ),
        {
            "id": chunk.id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": chunk.document_id,
            "page_id": chunk.page_id,
            "revision_id": chunk.revision_id,
            "title": chunk.title,
            "section_path": list(chunk.section_path),
            "content": chunk.content,
            "parent_chunk_id": chunk.parent_chunk_id,
            "prev_chunk_id": chunk.prev_chunk_id,
            "next_chunk_id": chunk.next_chunk_id,
            "source_uri": chunk.source_uri,
            "source_url": chunk.source_url,
            "embedding": json_dumps(chunk.embedding),
            "content_hash": chunk.content_hash,
            "metadata": json_dumps(metadata),
            "source_chunk_id": str(metadata.get("source_chunk_id") or chunk.id),
            "content_unit_id": str(metadata.get("content_unit_id") or metadata.get("parent_chunk_id") or chunk.id),
            "identity_scope": f"{tenant_id}:{knowledge_base_id}",
            "document_version_id": metadata.get("document_version_id"),
            "chunk_ordinal": int(chunk_ordinal) if isinstance(chunk_ordinal, int | float | str) else None,
            "locator": json_dumps(locator),
            "publication_status": str(metadata.get("publication_status") or "published"),
        },
    )
    legacy_chunk_id = str(metadata.get("source_chunk_id") or chunk.id)
    if legacy_chunk_id != chunk.id:
        await conn.execute(
            text(
                """
                INSERT INTO legacy_id_mappings(tenant_id, knowledge_base_id, entity_kind, legacy_id, scoped_id)
                VALUES (:tenant_id, :knowledge_base_id, 'chunk', :legacy_id, :scoped_id)
                ON CONFLICT (tenant_id, knowledge_base_id, entity_kind, legacy_id)
                DO UPDATE SET scoped_id = EXCLUDED.scoped_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "legacy_id": legacy_chunk_id,
                "scoped_id": chunk.id,
            },
        )


async def replace_document_sections_from_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str | None,
    chunks: list[Chunk],
) -> None:
    await conn.execute(
        text(
            """
            DELETE FROM document_sections
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
              AND (
                (CAST(:document_version_id AS text) IS NULL AND document_version_id IS NULL)
                OR document_version_id = CAST(:document_version_id AS text)
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
        },
    )
    sections = _sections_from_chunks(document_id=document_id, document_version_id=document_version_id, chunks=chunks)
    for section in sections:
        await conn.execute(
            text(
                """
                INSERT INTO document_sections(
                  section_id, tenant_id, knowledge_base_id, document_id, document_version_id,
                  parent_section_id, title, level, path, ordinal, locator,
                  first_chunk_id, last_chunk_id, metadata
                )
                VALUES (
                  :section_id, :tenant_id, :kb_id, :document_id, :document_version_id,
                  :parent_section_id, :title, :level, :path, :ordinal, CAST(:locator AS jsonb),
                  :first_chunk_id, :last_chunk_id, CAST(:metadata AS jsonb)
                )
                ON CONFLICT (tenant_id, knowledge_base_id, document_id, section_id) DO UPDATE
                SET document_version_id = EXCLUDED.document_version_id,
                    parent_section_id = EXCLUDED.parent_section_id,
                    title = EXCLUDED.title,
                    level = EXCLUDED.level,
                    path = EXCLUDED.path,
                    ordinal = EXCLUDED.ordinal,
                    locator = EXCLUDED.locator,
                    first_chunk_id = EXCLUDED.first_chunk_id,
                    last_chunk_id = EXCLUDED.last_chunk_id,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "section_id": section["section_id"],
                "tenant_id": tenant_id,
                "kb_id": knowledge_base_id,
                "document_id": document_id,
                "document_version_id": document_version_id,
                "parent_section_id": section["parent_section_id"],
                "title": section["title"],
                "level": section["level"],
                "path": section["path"],
                "ordinal": section["ordinal"],
                "locator": json_dumps(section["locator"]),
                "first_chunk_id": section["first_chunk_id"],
                "last_chunk_id": section["last_chunk_id"],
                "metadata": json_dumps(section["metadata"]),
            },
        )


def _sections_from_chunks(
    *, document_id: str, document_version_id: str | None, chunks: list[Chunk]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for fallback_ordinal, chunk in enumerate(chunks, start=1):
        path = tuple(str(item) for item in chunk.section_path if str(item)) or (chunk.title,)
        metadata = dict(chunk.metadata or {})
        raw_locator = metadata.get("locator")
        locator: dict[str, Any] = dict(raw_locator) if isinstance(raw_locator, dict) else {}
        chunk_ordinal = metadata.get("chunk_ordinal")
        ordinal = (
            int(chunk_ordinal)
            if isinstance(chunk_ordinal, int | float | str) and str(chunk_ordinal).isdigit()
            else fallback_ordinal
        )
        section = grouped.setdefault(
            path,
            {
                "section_id": str(
                    metadata.get("section_id") or _stable_section_id(document_id, document_version_id, path)
                ),
                "parent_section_id": _parent_section_id(document_id, document_version_id, path),
                "title": path[-1],
                "level": len(path),
                "path": list(path),
                "ordinal": ordinal,
                "locator": locator,
                "first_chunk_id": chunk.id,
                "last_chunk_id": chunk.id,
                "metadata": {"source": "chunks"},
            },
        )
        if ordinal < int(section["ordinal"]):
            section["ordinal"] = ordinal
            section["first_chunk_id"] = chunk.id
            section["locator"] = locator
        if ordinal >= int(section["ordinal"]):
            section["last_chunk_id"] = chunk.id
    return sorted(grouped.values(), key=lambda item: (int(item["ordinal"]), item["path"]))


def _stable_section_id(document_id: str, document_version_id: str | None, path: tuple[str, ...]) -> str:
    return "section:" + stable_hash([document_version_id or document_id, *path], 24)


def _parent_section_id(document_id: str, document_version_id: str | None, path: tuple[str, ...]) -> str | None:
    if len(path) <= 1:
        return None
    return _stable_section_id(document_id, document_version_id, path[:-1])


async def fetch_chunks_for_dense_scan(
    conn: AsyncConnection,
    tenant_id: str,
    knowledge_base_id: str,
    limit: int = 2500,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, knowledge_base_id, title, section_path, content, source_uri, source_url,
                   embedding, page_id, document_id, document_version_id, locator, metadata,
                   parent_chunk_id, content_hash
            FROM chunks
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND publication_status = 'published'
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def update_document_access_metadata(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str | None,
    document_access: dict[str, Any],
    origin: str | None = None,
) -> None:
    """Update DB-side document/chunk access metadata without changing content."""
    access = normalize_document_access(document_access)
    metadata_patch: dict[str, Any] = {"document_access": access}
    if origin is not None:
        metadata_patch["document_access_origin"] = origin
    await conn.execute(
        text(
            """
            UPDATE documents
            SET metadata = metadata || CAST(:metadata_patch AS jsonb),
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND id = :document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "metadata_patch": json_dumps(metadata_patch),
        },
    )
    await conn.execute(
        text(
            """
            UPDATE chunks
            SET metadata = metadata || CAST(:metadata_patch AS jsonb)
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
              AND (
                (CAST(:document_version_id AS text) IS NULL AND document_version_id IS NULL)
                OR document_version_id = CAST(:document_version_id AS text)
                OR CAST(:document_version_id AS text) IS NULL
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "metadata_patch": json_dumps(metadata_patch),
        },
    )


async def enqueue_document_access_projection(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_access: dict[str, Any],
    origin: str | None,
) -> None:
    """Durably schedule the derived index update in the same DB transaction.

    PostgreSQL remains the access authority.  The event is intentionally
    separate from ingestion jobs: it is small, retryable projection work and
    must not be lost when an HTTP request finishes before OpenSearch responds.
    """
    access = normalize_document_access(document_access)
    payload = {"document_access": access, "origin": origin or ""}
    dedupe_key = stable_hash(["document_access", tenant_id, knowledge_base_id, document_id, json_dumps(payload)], 64)
    await conn.execute(
        text(
            """
            INSERT INTO search_projection_events(
              id, tenant_id, knowledge_base_id, document_id, event_kind, dedupe_key, payload
            )
            VALUES (
              :id, :tenant_id, :knowledge_base_id, :document_id, 'document_access', :dedupe_key,
              CAST(:payload AS jsonb)
            )
            ON CONFLICT (dedupe_key) DO NOTHING
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "dedupe_key": dedupe_key,
            "payload": json_dumps(payload),
        },
    )


async def enqueue_document_publication_projection(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str,
    chunks: list[Chunk],
) -> None:
    """Durably request indexing of an already-published DB document version."""
    chunk_ids = sorted(str(chunk.id) for chunk in chunks)
    payload = {
        "document_version_id": document_version_id,
        "chunk_ids": chunk_ids,
        "chunk_count": len(chunk_ids),
        "chunk_set_hash": stable_hash(chunk_ids, 64),
    }
    dedupe_key = stable_hash(
        [
            "document_publication",
            tenant_id,
            knowledge_base_id,
            document_id,
            document_version_id,
            payload["chunk_set_hash"],
        ],
        64,
    )
    await conn.execute(
        text(
            """
            INSERT INTO search_projection_events(
              id, tenant_id, knowledge_base_id, document_id, event_kind, dedupe_key, payload
            )
            VALUES (
              :id, :tenant_id, :knowledge_base_id, :document_id, 'document_publication', :dedupe_key,
              CAST(:payload AS jsonb)
            )
            ON CONFLICT (dedupe_key) DO NOTHING
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "dedupe_key": dedupe_key,
            "payload": json_dumps(payload),
        },
    )
    await mark_search_projection_reconciliation_due(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
    )
    await mark_search_projection_reconciliation_due(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        expected_document_version_id=document_version_id,
        expected_projection_hash=str(payload["chunk_set_hash"]),
    )


async def load_published_document_version_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str,
) -> list[Chunk]:
    """Load the canonical published projection input; never trust event content."""
    result = await conn.execute(
        text(
            """
            SELECT id, document_id, page_id, revision_id, title, section_path, content,
                   parent_chunk_id, prev_chunk_id, next_chunk_id, source_uri, source_url,
                   content_hash, embedding, metadata
            FROM chunks
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :knowledge_base_id
              AND document_id = :document_id AND document_version_id = :document_version_id
              AND publication_status = 'published'
            ORDER BY chunk_ordinal, id
            """
        ),
        {
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
        },
    )
    return [
        Chunk(
            id=str(row["id"]),
            document_id=str(row["document_id"]),
            page_id=int(row["page_id"] or 0),
            revision_id=int(row["revision_id"] or 0),
            title=str(row["title"]),
            section_path=tuple(row["section_path"] or []),
            content=str(row["content"]),
            parent_chunk_id=str(row["parent_chunk_id"]) if row["parent_chunk_id"] else None,
            prev_chunk_id=str(row["prev_chunk_id"]) if row["prev_chunk_id"] else None,
            next_chunk_id=str(row["next_chunk_id"]) if row["next_chunk_id"] else None,
            source_uri=str(row["source_uri"]),
            source_url=str(row["source_url"]),
            content_hash=str(row["content_hash"]),
            embedding=list(row["embedding"] or []),
            metadata=dict(row["metadata"] or {}),
        )
        for row in result.mappings()
    ]


async def load_current_document_projection(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
) -> tuple[str | None, list[Chunk]]:
    """Load the only projection a reader may eventually observe.

    The current document version and its ACL are read from PostgreSQL at repair
    time.  A stale OpenSearch document can therefore only be removed or
    replaced, never treated as an authority.
    """
    result = await conn.execute(
        text(
            """
            SELECT d.metadata AS document_metadata, d.lifecycle_state,
                   c.id, c.document_id, c.page_id, c.revision_id, c.title,
                   c.section_path, c.content, c.parent_chunk_id, c.prev_chunk_id,
                   c.next_chunk_id, c.source_uri, c.source_url, c.content_hash,
                   c.embedding, c.metadata
            FROM documents AS d
            LEFT JOIN document_versions AS dv
              ON dv.id = d.metadata->>'current_version_id'
             AND dv.document_id = d.id
             AND dv.tenant_id = d.tenant_id
             AND dv.knowledge_base_id = d.knowledge_base_id
             AND dv.status = 'published'
             AND dv.lifecycle_state = 'active'
            LEFT JOIN chunks AS c
              ON c.document_id = d.id
             AND c.tenant_id = d.tenant_id
             AND c.knowledge_base_id = d.knowledge_base_id
             AND c.document_version_id = dv.id
             AND c.publication_status = 'published'
            WHERE d.id = :document_id AND d.tenant_id = :tenant_id
              AND d.knowledge_base_id = :knowledge_base_id
            ORDER BY c.chunk_ordinal, c.id
            """
        ),
        {"tenant_id": tenant_id, "knowledge_base_id": knowledge_base_id, "document_id": document_id},
    )
    rows = [dict(row) for row in result.mappings()]
    if not rows or str(rows[0].get("lifecycle_state") or "") != "active":
        return None, []
    document_metadata = dict(rows[0].get("document_metadata") or {})
    version_id = str(document_metadata.get("current_version_id") or "") or None
    chunks: list[Chunk] = []
    for row in rows:
        if row.get("id") is None:
            continue
        metadata = dict(row.get("metadata") or {})
        metadata["publication_status"] = "published"
        if "document_access" in document_metadata:
            metadata["document_access"] = document_metadata["document_access"]
        chunks.append(
            Chunk(
                id=str(row["id"]),
                document_id=str(row["document_id"]),
                page_id=int(row["page_id"] or 0),
                revision_id=int(row["revision_id"] or 0),
                title=str(row["title"]),
                section_path=tuple(row["section_path"] or []),
                content=str(row["content"]),
                parent_chunk_id=str(row["parent_chunk_id"]) if row["parent_chunk_id"] else None,
                prev_chunk_id=str(row["prev_chunk_id"]) if row["prev_chunk_id"] else None,
                next_chunk_id=str(row["next_chunk_id"]) if row["next_chunk_id"] else None,
                source_uri=str(row["source_uri"]),
                source_url=str(row["source_url"]),
                content_hash=str(row["content_hash"]),
                embedding=list(row["embedding"] or []),
                metadata=metadata,
            )
        )
    return version_id, chunks


async def claim_next_search_projection_event(
    conn: AsyncConnection,
    *,
    lease_id: str,
    lease_seconds: int,
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            WITH candidate AS (
              SELECT id
              FROM search_projection_events
              WHERE (status = 'received' OR (status = 'running' AND worker_lease_expires_at < now()))
                AND next_attempt_at <= now()
              ORDER BY created_at
              LIMIT 1
              FOR UPDATE SKIP LOCKED
            )
            UPDATE search_projection_events AS event
            SET status = 'running',
                attempts = event.attempts + 1,
                worker_lease_id = :lease_id,
                worker_lease_expires_at = now() + make_interval(secs => :lease_seconds),
                updated_at = now()
            FROM candidate
            WHERE event.id = candidate.id
            RETURNING event.*
            """
        ),
        {"lease_id": lease_id, "lease_seconds": max(1, int(lease_seconds))},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def complete_search_projection_event(conn: AsyncConnection, *, event_id: str, lease_id: str) -> None:
    result = await conn.execute(
        text(
            """
            UPDATE search_projection_events
            SET status = 'completed', completed_at = now(), worker_lease_id = NULL,
                worker_lease_expires_at = NULL, error_code = NULL, error_message = NULL, updated_at = now()
            WHERE id = :id AND status = 'running' AND worker_lease_id = :lease_id
            """
        ),
        {"id": event_id, "lease_id": lease_id},
    )
    if result.rowcount != 1:
        raise RuntimeError("SEARCH_PROJECTION_LEASE_LOST")


async def retry_search_projection_event(
    conn: AsyncConnection,
    *,
    event_id: str,
    lease_id: str,
    error_code: str,
    max_attempts: int = 5,
) -> None:
    result = await conn.execute(
        text(
            """
            UPDATE search_projection_events
            SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'received' END,
                next_attempt_at = now() + make_interval(secs => LEAST(300, 2 ^ LEAST(attempts, 8))),
                worker_lease_id = NULL, worker_lease_expires_at = NULL,
                error_code = :error_code, error_message = 'search projection update failed', updated_at = now()
            WHERE id = :id AND status = 'running' AND worker_lease_id = :lease_id
            """
        ),
        {"id": event_id, "lease_id": lease_id, "error_code": error_code[:96], "max_attempts": max_attempts},
    )
    if result.rowcount != 1:
        raise RuntimeError("SEARCH_PROJECTION_LEASE_LOST")


async def claim_due_search_projection_reconciliations(
    conn: AsyncConnection, *, lease_id: str, lease_seconds: int, batch_size: int
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            WITH candidate AS (
              SELECT document_id FROM search_projection_reconciliation
              WHERE (status IN ('due','degraded') OR (status='running' AND worker_lease_expires_at < now()))
                AND next_check_at <= now()
              ORDER BY next_check_at, updated_at LIMIT :batch_size FOR UPDATE SKIP LOCKED
            )
            UPDATE search_projection_reconciliation AS item
            SET status='running', attempts=item.attempts+1, worker_lease_id=:lease_id,
                worker_lease_expires_at=now()+make_interval(secs => :lease_seconds),
                last_checked_at=now(), updated_at=now()
            FROM candidate WHERE item.document_id=candidate.document_id
            RETURNING item.*
            """
        ),
        {"lease_id": lease_id, "lease_seconds": max(1, int(lease_seconds)), "batch_size": max(1, int(batch_size))},
    )
    return [dict(row) for row in result.mappings()]


async def schedule_historical_search_projection_reconciliations(
    conn: AsyncConnection, *, batch_size: int, generation: int = 1
) -> int:
    """Schedule one durable, resumable page of pre-reconciliation documents.

    The scan-state row is a short-lived scheduler claim, not a global worker
    lock.  The reconciliation table's document primary key is the durable
    idempotency identity and prevents duplicate logical work.
    """
    result = await conn.execute(
        text(
            """
            WITH state AS (
              SELECT cursor_document_id, completed_at
              FROM search_projection_reconciliation_scan_state
              WHERE generation = :generation
              FOR UPDATE SKIP LOCKED
            ), candidate AS (
              SELECT d.id, d.tenant_id, d.knowledge_base_id
              FROM documents AS d CROSS JOIN state
              WHERE state.completed_at IS NULL
                AND (state.cursor_document_id IS NULL OR d.id > state.cursor_document_id)
                AND NOT EXISTS (
                  SELECT 1 FROM search_projection_reconciliation AS r
                  WHERE r.document_id = d.id AND r.reconciliation_generation >= :generation
                )
              ORDER BY d.id
              LIMIT :batch_size
              FOR UPDATE OF d SKIP LOCKED
            ), scheduled AS (
              INSERT INTO search_projection_reconciliation(
                document_id, tenant_id, knowledge_base_id, status, next_check_at, reconciliation_generation
              )
              SELECT id, tenant_id, knowledge_base_id, 'due', now(), :generation FROM candidate
              ON CONFLICT (document_id) DO UPDATE
              SET status = CASE WHEN search_projection_reconciliation.reconciliation_generation < :generation
                                  THEN 'due' ELSE search_projection_reconciliation.status END,
                  next_check_at = CASE WHEN search_projection_reconciliation.reconciliation_generation < :generation
                                       THEN now() ELSE search_projection_reconciliation.next_check_at END,
                  reconciliation_generation = GREATEST(search_projection_reconciliation.reconciliation_generation,
                                                      :generation),
                  updated_at = now()
              RETURNING document_id
            ), advanced AS (
              UPDATE search_projection_reconciliation_scan_state AS scan
              SET cursor_document_id = COALESCE((SELECT max(id) FROM candidate), scan.cursor_document_id),
                  completed_at = CASE
                    WHEN scan.completed_at IS NOT NULL THEN scan.completed_at
                    WHEN NOT EXISTS (SELECT 1 FROM candidate) THEN now()
                    ELSE NULL END,
                  updated_at = now()
              WHERE scan.generation = :generation
                AND EXISTS (SELECT 1 FROM state)
              RETURNING scan.generation
            )
            SELECT count(*) AS scheduled FROM scheduled
            """
        ),
        {"generation": generation, "batch_size": max(1, int(batch_size))},
    )
    return int(result.scalar_one() or 0)


async def renew_search_projection_reconciliation_lease(
    conn: AsyncConnection, *, document_id: str, lease_id: str, lease_seconds: int
) -> bool:
    """Extend only the current owner's lease; false is an external fence."""
    result = await conn.execute(
        text(
            """
            UPDATE search_projection_reconciliation
            SET worker_lease_expires_at = now() + make_interval(secs => :lease_seconds), updated_at = now()
            WHERE document_id = :document_id AND status = 'running'
              AND worker_lease_id = :lease_id AND worker_lease_expires_at >= now()
            RETURNING document_id
            """
        ),
        {"document_id": document_id, "lease_id": lease_id, "lease_seconds": max(1, int(lease_seconds))},
    )
    return result.mappings().first() is not None


async def cleanup_completed_search_projection_events(
    conn: AsyncConnection, *, retention_days: int, batch_size: int
) -> int:
    """Bounded, restart-safe retention for obsolete completed projection events."""
    result = await conn.execute(
        text(
            """
            WITH candidate AS (
              SELECT id FROM search_projection_events
              WHERE status = 'completed'
                AND completed_at < now() - make_interval(days => :retention_days)
              ORDER BY completed_at, id
              LIMIT :batch_size
              FOR UPDATE SKIP LOCKED
            )
            DELETE FROM search_projection_events AS event
            USING candidate
            WHERE event.id = candidate.id
            RETURNING event.id
            """
        ),
        {"retention_days": max(0, int(retention_days)), "batch_size": max(1, int(batch_size))},
    )
    return len(list(result.mappings()))


async def finish_search_projection_reconciliation(
    conn: AsyncConnection,
    *,
    document_id: str,
    lease_id: str,
    expected_document_version_id: str | None,
    expected_projection_hash: str,
    observed_projection_hash: str,
    interval_seconds: int,
) -> None:
    result = await conn.execute(
        text(
            """
            UPDATE search_projection_reconciliation
            SET status='ok', next_check_at=now()+make_interval(secs => :interval_seconds), last_success_at=now(),
                worker_lease_id=NULL, worker_lease_expires_at=NULL,
                expected_document_version_id=:version_id, expected_projection_hash=:expected_hash,
                observed_projection_hash=:observed_hash, last_error_code=NULL, updated_at=now()
            WHERE document_id=:document_id AND status='running' AND worker_lease_id=:lease_id
            """
        ),
        {
            "document_id": document_id,
            "lease_id": lease_id,
            "version_id": expected_document_version_id,
            "expected_hash": expected_projection_hash,
            "observed_hash": observed_projection_hash,
            "interval_seconds": max(1, int(interval_seconds)),
        },
    )
    if result.rowcount != 1:
        raise RuntimeError("SEARCH_PROJECTION_LEASE_LOST")


async def fail_search_projection_reconciliation(
    conn: AsyncConnection, *, document_id: str, lease_id: str, error_code: str, max_attempts: int = 5
) -> None:
    result = await conn.execute(
        text(
            """
            UPDATE search_projection_reconciliation
            SET status=CASE WHEN attempts >= :max_attempts THEN 'degraded' ELSE 'due' END,
                next_check_at=now()+make_interval(secs => CASE WHEN attempts >= :max_attempts THEN 3600
                    ELSE LEAST(300, 2 ^ LEAST(attempts, 8)) END), worker_lease_id=NULL,
                worker_lease_expires_at=NULL, last_error_code=:error_code, updated_at=now()
            WHERE document_id=:document_id AND status='running' AND worker_lease_id=:lease_id
            """
        ),
        {"document_id": document_id, "lease_id": lease_id, "error_code": error_code[:96], "max_attempts": max_attempts},
    )
    if result.rowcount != 1:
        raise RuntimeError("SEARCH_PROJECTION_LEASE_LOST")


async def search_projection_health(conn: AsyncConnection) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE status IN ('received','running')) AS pending,
                   EXTRACT(EPOCH FROM (
                     now() - min(created_at) FILTER (WHERE status IN ('received','running'))
                   )) AS oldest_age_seconds,
                   (array_agg(error_code ORDER BY updated_at DESC)
                     FILTER (WHERE status = 'failed'))[1] AS last_error_code
            FROM search_projection_events
            """
        )
    )
    row = dict(result.mappings().one())
    reconciliation = await conn.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE status IN ('due','running')) AS pending,
                   count(*) FILTER (WHERE status='degraded') AS degraded,
                   EXTRACT(EPOCH FROM (now() - min(next_check_at) FILTER (WHERE status IN ('due','degraded'))))
                     AS oldest_age_seconds,
                   (array_agg(last_error_code ORDER BY updated_at DESC)
                     FILTER (WHERE status='degraded' AND last_error_code IS NOT NULL))[1] AS last_error_code
            FROM search_projection_reconciliation
            """
        )
    )
    reconciliation_row = dict(reconciliation.mappings().one())
    return {
        "pending": int(row.get("pending") or 0),
        "oldest_age_seconds": int(float(row.get("oldest_age_seconds") or 0)),
        "last_error_code": str(row["last_error_code"]) if row.get("last_error_code") else None,
        "reconciliation_pending": int(reconciliation_row.get("pending") or 0),
        "reconciliation_degraded": int(reconciliation_row.get("degraded") or 0),
        "reconciliation_oldest_age_seconds": int(float(reconciliation_row.get("oldest_age_seconds") or 0)),
        "reconciliation_error_code": (
            str(reconciliation_row["last_error_code"]) if reconciliation_row.get("last_error_code") else None
        ),
    }


async def fetch_chunk_by_id(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    chunk_id: str,
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT id, title, section_path, content, source_url, page_id, document_id,
                   knowledge_base_id,
                   parent_chunk_id, prev_chunk_id, next_chunk_id, metadata
            FROM chunks
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND (
                id = :chunk_id
                OR id = (
                    SELECT scoped_id
                    FROM legacy_id_mappings
                    WHERE tenant_id = :tenant_id
                      AND knowledge_base_id = :kb_id
                      AND entity_kind = 'chunk'
                      AND legacy_id = :chunk_id
                )
              )
              AND publication_status = 'published'
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "chunk_id": chunk_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_current_retrieval_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    chunk_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Return the currently publishable candidates for a search result set.

    OpenSearch and Redis are candidate/cache layers, not authorization owners.
    A single PostgreSQL read therefore confirms publication, document lifecycle,
    and the current document access metadata before a candidate is exposed.
    """
    requested = sorted({str(chunk_id) for chunk_id in chunk_ids if str(chunk_id)})
    if not requested:
        return {}
    result = await conn.execute(
        text(
            """
            SELECT c.id AS chunk_id,
                   c.document_id,
                   c.document_version_id,
                   c.metadata AS chunk_metadata,
                   d.metadata AS document_metadata
            FROM chunks AS c
            JOIN documents AS d
              ON d.id = c.document_id
             AND d.tenant_id = c.tenant_id
             AND d.knowledge_base_id = c.knowledge_base_id
            JOIN document_versions AS dv
              ON dv.id = c.document_version_id
             AND dv.document_id = d.id
             AND dv.tenant_id = d.tenant_id
             AND dv.knowledge_base_id = d.knowledge_base_id
            WHERE c.tenant_id = :tenant_id
              AND c.knowledge_base_id = :knowledge_base_id
              AND c.id = ANY(CAST(:chunk_ids AS text[]))
              AND c.publication_status = 'published'
              AND d.lifecycle_state = 'active'
              AND c.document_version_id = (d.metadata->>'current_version_id')
              AND dv.status = 'published'
              AND dv.lifecycle_state = 'active'
            """
        ),
        {
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "chunk_ids": requested,
        },
    )
    rows: dict[str, dict[str, Any]] = {}
    for row in result.mappings():
        item = dict(row)
        metadata = dict(item.pop("chunk_metadata") or {})
        # Document ACL is the current authority.  It intentionally overwrites
        # stale copied metadata from an indexed chunk.
        document_metadata = dict(item.pop("document_metadata") or {})
        if "document_access" in document_metadata:
            metadata["document_access"] = document_metadata["document_access"]
        rows[str(item["chunk_id"])] = {**item, "metadata": metadata}
    return rows


async def mark_search_projection_reconciliation_due(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    expected_document_version_id: str | None = None,
    expected_projection_hash: str | None = None,
) -> None:
    """Make a derived document projection eligible for bounded repair.

    This runs in the same PostgreSQL transaction as the canonical change.  It
    never grants access itself; it merely schedules a repair of OpenSearch.
    """
    await conn.execute(
        text(
            """
            INSERT INTO search_projection_reconciliation(
              document_id, tenant_id, knowledge_base_id, status, next_check_at,
              expected_document_version_id, expected_projection_hash
            )
            VALUES (:document_id, :tenant_id, :knowledge_base_id, 'due', now(),
                    :expected_document_version_id, :expected_projection_hash)
            ON CONFLICT (document_id) DO UPDATE
            SET status='due', next_check_at=now(), worker_lease_id=NULL,
                worker_lease_expires_at=NULL,
                expected_document_version_id=COALESCE(EXCLUDED.expected_document_version_id,
                                                       search_projection_reconciliation.expected_document_version_id),
                expected_projection_hash=COALESCE(EXCLUDED.expected_projection_hash,
                                                    search_projection_reconciliation.expected_projection_hash),
                updated_at=now()
            """
        ),
        {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "expected_document_version_id": expected_document_version_id,
            "expected_projection_hash": expected_projection_hash,
        },
    )


async def retrieval_document_scope_marker(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
) -> str:
    """A small cache-version marker that changes when document access changes."""
    scoped_ids = sorted({str(value) for value in knowledge_base_ids if str(value)})
    if not scoped_ids:
        return "none"
    result = await conn.execute(
        text(
            """
            SELECT COALESCE(MAX(updated_at)::text, 'none') AS marker
            FROM documents
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = ANY(CAST(:knowledge_base_ids AS uuid[]))
            """
        ),
        {"tenant_id": tenant_id, "knowledge_base_ids": scoped_ids},
    )
    return str(result.scalar_one() or "none")


async def upsert_document(
    conn: AsyncConnection,
    *,
    document_id: str,
    tenant_id: str,
    knowledge_base_id: str,
    source_type: str,
    title: str,
    source_uri: str,
    metadata: dict[str, Any],
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata,
                                  source_document_id, identity_scope)
            VALUES (:id, :tenant_id, :kb_id, :source_type, :title, :source_uri, CAST(:metadata AS jsonb),
                    :source_document_id, :identity_scope)
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                source_uri = EXCLUDED.source_uri,
                metadata = EXCLUDED.metadata,
                source_document_id = EXCLUDED.source_document_id,
                identity_scope = EXCLUDED.identity_scope,
                updated_at = now()
            """
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "source_type": source_type,
            "title": title,
            "source_uri": source_uri,
            "metadata": json_dumps(metadata),
            "source_document_id": str(metadata.get("source_document_id") or document_id),
            "identity_scope": f"{tenant_id}:{knowledge_base_id}",
        },
    )
    legacy_document_id = str(metadata.get("source_document_id") or document_id)
    if legacy_document_id != document_id:
        await conn.execute(
            text(
                """
                INSERT INTO legacy_id_mappings(tenant_id, knowledge_base_id, entity_kind, legacy_id, scoped_id)
                VALUES (:tenant_id, :knowledge_base_id, 'document', :legacy_id, :scoped_id)
                ON CONFLICT (tenant_id, knowledge_base_id, entity_kind, legacy_id)
                DO UPDATE SET scoped_id = EXCLUDED.scoped_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "legacy_id": legacy_document_id,
                "scoped_id": document_id,
            },
        )


async def save_index_version(
    conn: AsyncConnection,
    *,
    index_version_id: str,
    tenant_id: str,
    knowledge_base_id: str,
    source_type: str,
    snapshot_id: str,
    retrieval_profile: str,
    embedding_alias: str,
    embedding_dimensions: int,
    physical_index: str,
    read_alias: str,
    write_alias: str,
    metadata: dict[str, Any],
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO index_versions(
              id, tenant_id, knowledge_base_id, source_type, snapshot_id, retrieval_profile,
              embedding_alias, embedding_dimensions, physical_index, read_alias, write_alias, metadata,
              identity_scope
            )
            VALUES (
              :id, :tenant_id, :kb_id, :source_type, :snapshot_id, :retrieval_profile,
              :embedding_alias, :embedding_dimensions, :physical_index, :read_alias, :write_alias,
              CAST(:metadata AS jsonb), :identity_scope
            )
            ON CONFLICT (id) DO UPDATE
            SET status = 'active',
                metadata = EXCLUDED.metadata,
                identity_scope = EXCLUDED.identity_scope,
                updated_at = now()
            """
        ),
        {
            "id": index_version_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "source_type": source_type,
            "snapshot_id": snapshot_id,
            "retrieval_profile": retrieval_profile,
            "embedding_alias": embedding_alias,
            "embedding_dimensions": embedding_dimensions,
            "physical_index": physical_index,
            "read_alias": read_alias,
            "write_alias": write_alias,
            "metadata": json_dumps(metadata),
            "identity_scope": f"{tenant_id}:{knowledge_base_id}",
        },
    )
    legacy_index_id = str(metadata.get("source_index_version_id") or index_version_id)
    if legacy_index_id != index_version_id:
        await conn.execute(
            text(
                """
                INSERT INTO legacy_id_mappings(tenant_id, knowledge_base_id, entity_kind, legacy_id, scoped_id)
                VALUES (:tenant_id, :knowledge_base_id, 'index_version', :legacy_id, :scoped_id)
                ON CONFLICT (tenant_id, knowledge_base_id, entity_kind, legacy_id)
                DO UPDATE SET scoped_id = EXCLUDED.scoped_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "legacy_id": legacy_index_id,
                "scoped_id": index_version_id,
            },
        )


async def load_index_version_by_read_alias(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    read_alias: str,
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM index_versions
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND read_alias = :read_alias
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "read_alias": read_alias},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def search_public_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
    query: str,
    limit: int,
    offset: int,
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    query_pattern = f"%{query.strip()}%"
    document_type = str(filters.get("document_type") or "").strip().casefold()
    language = str(filters.get("language") or "").strip().casefold()
    source = str(filters.get("source") or "").strip().casefold()
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    document_type_pattern = f"%{document_type}%" if document_type else None
    source_pattern = f"%{source}%" if source else None
    result = await conn.execute(
        text(
            """
            WITH searchable_chunks AS (
              SELECT
                c.id AS chunk_id,
                c.document_id,
                c.document_version_id,
                c.knowledge_base_id,
                c.title,
                c.section_path,
                c.content,
                c.source_url,
                c.source_uri,
                c.page_id,
                c.locator,
                c.metadata AS chunk_metadata,
                d.source_type,
                d.metadata AS document_metadata,
                v.public_metadata,
                COALESCE(
                  v.public_metadata ->> 'content_type',
                  v.public_metadata ->> 'detected_mime',
                  c.metadata ->> 'content_type',
                  d.source_type
                ) AS document_type,
                COALESCE(v.public_metadata ->> 'detected_language', c.metadata ->> 'language') AS language,
                COALESCE(v.public_metadata ->> 'document_date', c.metadata ->> 'document_date') AS document_date,
                CASE WHEN c.title ILIKE :query_pattern THEN 4.0 ELSE 0.0 END
                  + CASE WHEN array_to_string(c.section_path, ' ') ILIKE :query_pattern THEN 2.0 ELSE 0.0 END
                  + CASE WHEN c.content ILIKE :query_pattern THEN 1.0 ELSE 0.0 END AS score
              FROM chunks c
              JOIN documents d
                ON d.id = c.document_id
               AND d.tenant_id = c.tenant_id
               AND d.knowledge_base_id = c.knowledge_base_id
              LEFT JOIN document_versions v
                ON v.id = c.document_version_id
               AND v.tenant_id = c.tenant_id
               AND v.knowledge_base_id = c.knowledge_base_id
              WHERE c.tenant_id = :tenant_id
                AND c.knowledge_base_id = ANY(CAST(:knowledge_base_ids AS uuid[]))
                AND c.publication_status = 'published'
                AND d.lifecycle_state = 'active'
                AND (
                  c.title ILIKE :query_pattern
                  OR array_to_string(c.section_path, ' ') ILIKE :query_pattern
                  OR c.content ILIKE :query_pattern
                )
            )
            SELECT *
            FROM searchable_chunks
            WHERE (
                CAST(:document_type_pattern AS text) IS NULL
                OR lower(COALESCE(document_type, '')) LIKE :document_type_pattern
              )
              AND (CAST(:language AS text) IS NULL OR lower(COALESCE(language, '')) = :language)
              AND (CAST(:date_from AS text) IS NULL OR COALESCE(document_date, '') >= :date_from)
              AND (CAST(:date_to AS text) IS NULL OR COALESCE(document_date, '') <= :date_to)
              AND (
                CAST(:source_pattern AS text) IS NULL
                OR lower(
                  concat_ws(
                    ' ',
                    source_type,
                    source_uri,
                    source_url,
                    COALESCE(public_metadata ->> 'filename', '')
                  )
                ) LIKE :source_pattern
              )
            ORDER BY score DESC, title ASC, chunk_id ASC
            LIMIT :limit_plus_one OFFSET :offset
            """
        ),
        {
            "tenant_id": tenant_id,
            "knowledge_base_ids": knowledge_base_ids,
            "query_pattern": query_pattern,
            "document_type_pattern": document_type_pattern,
            "language": language or None,
            "date_from": str(date_from) if date_from is not None else None,
            "date_to": str(date_to) if date_to is not None else None,
            "source_pattern": source_pattern,
            "limit_plus_one": limit + 1,
            "offset": offset,
        },
    )
    rows = [dict(row) for row in result.mappings()]
    for rank, row in enumerate(rows, start=offset + 1):
        row["ranks"] = {"search": rank}
    return rows


async def list_document_sections(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str | None,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT section_id, parent_section_id, title, level, path, ordinal,
                   locator, first_chunk_id, last_chunk_id, metadata
            FROM document_sections
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND document_id = :document_id
              AND (
                (CAST(:document_version_id AS text) IS NULL AND document_version_id IS NULL)
                OR document_version_id = CAST(:document_version_id AS text)
              )
            ORDER BY ordinal ASC, path ASC
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
        },
    )
    rows = [dict(row) for row in result.mappings()]
    if rows:
        return rows
    return await list_document_sections_from_chunks(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        document_version_id=document_version_id,
    )


async def list_document_sections_from_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str | None,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            WITH section_chunks AS (
              SELECT
                c.section_path,
                MIN(COALESCE(c.chunk_ordinal, 2147483647)) AS ordinal,
                (ARRAY_AGG(c.id ORDER BY COALESCE(c.chunk_ordinal, 2147483647), c.id))[1] AS first_chunk_id,
                (ARRAY_AGG(c.id ORDER BY COALESCE(c.chunk_ordinal, 2147483647) DESC, c.id DESC))[1] AS last_chunk_id,
                (ARRAY_AGG(c.locator ORDER BY COALESCE(c.chunk_ordinal, 2147483647), c.id))[1] AS locator
              FROM chunks c
              JOIN documents d
                ON d.id = c.document_id
               AND d.tenant_id = c.tenant_id
               AND d.knowledge_base_id = c.knowledge_base_id
              WHERE c.tenant_id = :tenant_id
                AND c.knowledge_base_id = :kb_id
                AND c.document_id = :document_id
                AND c.publication_status = 'published'
                AND d.lifecycle_state = 'active'
                AND (
                  (CAST(:document_version_id AS text) IS NULL AND c.document_version_id IS NULL)
                  OR c.document_version_id = CAST(:document_version_id AS text)
                  OR CAST(:document_version_id AS text) IS NULL
                )
              GROUP BY c.section_path
            )
            SELECT section_path, ordinal, first_chunk_id, last_chunk_id, locator
            FROM section_chunks
            ORDER BY ordinal ASC, section_path ASC
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
        },
    )
    sections: list[dict[str, Any]] = []
    for index, row in enumerate(result.mappings(), start=1):
        path = [str(item) for item in row.get("section_path") or [] if str(item)]
        if not path:
            continue
        section_id = _stable_section_id(document_id, document_version_id, tuple(path))
        sections.append(
            {
                "section_id": section_id,
                "parent_section_id": _parent_section_id(document_id, document_version_id, tuple(path)),
                "title": path[-1],
                "level": len(path),
                "path": path,
                "ordinal": int(row.get("ordinal") or index),
                "locator": dict(row.get("locator") or {}),
                "first_chunk_id": row.get("first_chunk_id"),
                "last_chunk_id": row.get("last_chunk_id"),
                "metadata": {"source": "chunks_fallback"},
            }
        )
    return sections


async def fetch_document_context_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str | None,
    chunk_id: str | None = None,
    section_path: list[str] | None = None,
    before: int = 2,
    after: int = 2,
    limit: int = 80,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if section_path:
        result = await conn.execute(
            text(
                """
                SELECT c.id AS chunk_id, c.document_id, c.document_version_id, c.knowledge_base_id,
                       c.title, c.section_path, c.content, c.source_url, c.source_uri,
                       c.page_id, c.locator, c.metadata AS chunk_metadata,
                       c.prev_chunk_id, c.next_chunk_id, c.chunk_ordinal
                FROM chunks c
                JOIN documents d
                  ON d.id = c.document_id
                 AND d.tenant_id = c.tenant_id
                 AND d.knowledge_base_id = c.knowledge_base_id
                WHERE c.tenant_id = :tenant_id
                  AND c.knowledge_base_id = :kb_id
                  AND c.document_id = :document_id
                  AND c.publication_status = 'published'
                  AND d.lifecycle_state = 'active'
                  AND c.section_path = CAST(:section_path AS text[])
                  AND (
                    (CAST(:document_version_id AS text) IS NULL AND c.document_version_id IS NULL)
                    OR c.document_version_id = CAST(:document_version_id AS text)
                    OR CAST(:document_version_id AS text) IS NULL
                  )
                ORDER BY COALESCE(c.chunk_ordinal, 2147483647), c.id
                LIMIT :limit OFFSET :offset
                """
            ),
            {
                "tenant_id": tenant_id,
                "kb_id": knowledge_base_id,
                "document_id": document_id,
                "document_version_id": document_version_id,
                "section_path": section_path,
                "limit": limit,
                "offset": offset,
            },
        )
        return [dict(row) for row in result.mappings()]

    result = await conn.execute(
        text(
            """
            WITH ordered_chunks AS (
              SELECT c.id AS chunk_id, c.document_id, c.document_version_id, c.knowledge_base_id,
                     c.title, c.section_path, c.content, c.source_url, c.source_uri,
                     c.page_id, c.locator, c.metadata AS chunk_metadata,
                     c.prev_chunk_id, c.next_chunk_id, c.chunk_ordinal,
                     ROW_NUMBER() OVER (ORDER BY COALESCE(c.chunk_ordinal, 2147483647), c.id) AS row_number
              FROM chunks c
              JOIN documents d
                ON d.id = c.document_id
               AND d.tenant_id = c.tenant_id
               AND d.knowledge_base_id = c.knowledge_base_id
              WHERE c.tenant_id = :tenant_id
                AND c.knowledge_base_id = :kb_id
                AND c.document_id = :document_id
                AND c.publication_status = 'published'
                AND d.lifecycle_state = 'active'
                AND (
                  (CAST(:document_version_id AS text) IS NULL AND c.document_version_id IS NULL)
                  OR c.document_version_id = CAST(:document_version_id AS text)
                  OR CAST(:document_version_id AS text) IS NULL
                )
            ),
            anchor AS (
              SELECT row_number
              FROM ordered_chunks
              WHERE chunk_id = :chunk_id
              UNION ALL
              SELECT 1 AS row_number
              WHERE :chunk_id IS NULL
              LIMIT 1
            )
            SELECT ordered_chunks.*
            FROM ordered_chunks, anchor
            WHERE ordered_chunks.row_number BETWEEN GREATEST(anchor.row_number - :before, 1)
                                                AND anchor.row_number + :after
            ORDER BY ordered_chunks.row_number
            LIMIT :limit OFFSET :offset
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "chunk_id": chunk_id,
            "before": before,
            "after": after,
            "limit": limit,
            "offset": offset,
        },
    )
    return [dict(row) for row in result.mappings()]


async def search_document_chunks(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_id: str | None,
    query: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    query_pattern = f"%{query.strip()}%"
    result = await conn.execute(
        text(
            """
            SELECT c.id AS chunk_id, c.document_id, c.document_version_id, c.knowledge_base_id,
                   c.title, c.section_path, c.content, c.source_url, c.source_uri,
                   c.page_id, c.locator, c.metadata AS chunk_metadata,
                   c.prev_chunk_id, c.next_chunk_id, c.chunk_ordinal,
                   CASE WHEN c.title ILIKE :query_pattern THEN 4.0 ELSE 0.0 END
                     + CASE WHEN array_to_string(c.section_path, ' ') ILIKE :query_pattern THEN 2.0 ELSE 0.0 END
                     + CASE WHEN c.content ILIKE :query_pattern THEN 1.0 ELSE 0.0 END AS score
            FROM chunks c
            JOIN documents d
              ON d.id = c.document_id
             AND d.tenant_id = c.tenant_id
             AND d.knowledge_base_id = c.knowledge_base_id
            WHERE c.tenant_id = :tenant_id
              AND c.knowledge_base_id = :kb_id
              AND c.document_id = :document_id
              AND c.publication_status = 'published'
              AND d.lifecycle_state = 'active'
              AND (
                (CAST(:document_version_id AS text) IS NULL AND c.document_version_id IS NULL)
                OR c.document_version_id = CAST(:document_version_id AS text)
                OR CAST(:document_version_id AS text) IS NULL
              )
              AND (
                c.title ILIKE :query_pattern
                OR array_to_string(c.section_path, ' ') ILIKE :query_pattern
                OR c.content ILIKE :query_pattern
              )
            ORDER BY score DESC, COALESCE(c.chunk_ordinal, 2147483647), c.id
            LIMIT :limit_plus_one OFFSET :offset
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "query_pattern": query_pattern,
            "limit_plus_one": limit + 1,
            "offset": offset,
        },
    )
    rows = [dict(row) for row in result.mappings()]
    for rank, row in enumerate(rows, start=offset + 1):
        row["ranks"] = {"document_search": rank}
    return rows


async def insert_retrieval_event(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    query_run_id: str | None,
    trace_id: str,
    event_type: str,
    stage: str,
    payload: dict[str, Any],
) -> None:
    safe_payload = safe_telemetry_payload(payload)
    encoded = json_dumps(safe_payload)
    if len(encoded.encode("utf-8")) > 256 * 1024:
        safe_payload = {
            "event_truncated": True,
            "payload_hash": stable_hash([encoded], 32),
            "payload_bytes": len(encoded.encode("utf-8")),
        }
        encoded = json_dumps(safe_payload)
    await conn.execute(
        text(
            """
            INSERT INTO retrieval_events(id, tenant_id, query_run_id, trace_id, event_type, stage, payload)
            VALUES (:id, :tenant_id, :query_run_id, :trace_id, :event_type, :stage,
                    CAST(:payload AS jsonb))
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "query_run_id": query_run_id,
            "trace_id": trace_id,
            "event_type": event_type,
            "stage": stage,
            "payload": encoded,
        },
    )


async def load_retrieval_events(conn: AsyncConnection, tenant_id: str, query_run_id: str) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT event_type, stage, payload, created_at, sequence
            FROM retrieval_events
            WHERE tenant_id = :tenant_id AND query_run_id = :query_run_id
            ORDER BY sequence
            """
        ),
        {"tenant_id": tenant_id, "query_run_id": query_run_id},
    )
    return [dict(row) for row in result.mappings()]


async def create_query_run(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str | None = None,
    user_id: str,
    request_id: str,
    client_request_id: str | None,
    mode: str,
    input_text: str,
    trace_id: str,
    usage: dict[str, Any] | None = None,
    model_config_revision_id: str | None = None,
    model_config_hash: str | None = None,
) -> uuid.UUID:
    # The request identifies the logical episode, not a model attempt.  A
    # retry after a failure before episode creation must find the same row.
    query_run_id = stable_uuid(["query_run_v3", request_id])
    result = await conn.execute(
        text(
            """
            INSERT INTO query_runs(
              id, tenant_id, knowledge_base_id, user_id, request_id, client_request_id, mode, status,
              input_text, usage, trace_id, started_at, model_config_revision_id, model_config_hash
            )
            VALUES (:id, :tenant_id, :knowledge_base_id, :user_id, :request_id, :client_request_id, :mode,
                    'running', :input_text, CAST(:usage AS jsonb), :trace_id, now(),
                    :model_config_revision_id, :model_config_hash)
            ON CONFLICT (request_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": str(query_run_id),
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "user_id": user_id,
            "request_id": request_id,
            "client_request_id": client_request_id,
            "mode": mode,
            "input_text": input_text,
            "usage": json_dumps(usage or {}),
            "trace_id": trace_id,
            "model_config_revision_id": model_config_revision_id,
            "model_config_hash": model_config_hash,
        },
    )
    mappings = getattr(result, "mappings", None)
    row = mappings().first() if callable(mappings) else None
    if row is not None and row.get("id") is not None:
        return uuid.UUID(str(row["id"]))
    existing = await conn.execute(
        text("SELECT id FROM query_runs WHERE tenant_id = :tenant_id AND request_id = :request_id"),
        {"tenant_id": tenant_id, "request_id": request_id},
    )
    existing_mappings = getattr(existing, "mappings", None)
    existing_row = existing_mappings().first() if callable(existing_mappings) else None
    if existing_row is not None and existing_row.get("id") is not None:
        return uuid.UUID(str(existing_row["id"]))
    return query_run_id


async def claim_idempotency_record(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    actor_user_id: str,
    route: str,
    idempotency_key: str,
    request_hash: str,
    ttl_seconds: int,
) -> tuple[dict[str, Any], bool]:
    """Atomically claim an optional client idempotency key.

    The returned boolean is true only for the request that may perform side
    effects.  Callers must persist their safe response with
    ``complete_idempotency_record`` before returning it to the client.
    """
    result = await conn.execute(
        text(
            """
            INSERT INTO idempotency_records(
              id, tenant_id, actor_user_id, route, idempotency_key, request_hash, status, expires_at
            )
            VALUES (
              :id, :tenant_id, :actor_user_id, :route, :idempotency_key, :request_hash, 'in_progress',
              now() + make_interval(secs => :ttl_seconds)
            )
            ON CONFLICT (tenant_id, actor_user_id, route, idempotency_key) DO NOTHING
            RETURNING *
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "actor_user_id": actor_user_id,
            "route": route,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "ttl_seconds": max(1, int(ttl_seconds)),
        },
    )
    row = result.mappings().first()
    if row is not None:
        return dict(row), True
    existing_result = await conn.execute(
        text(
            """
            SELECT *
            FROM idempotency_records
            WHERE tenant_id = :tenant_id AND actor_user_id = :actor_user_id
              AND route = :route AND idempotency_key = :idempotency_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_user_id": actor_user_id,
            "route": route,
            "idempotency_key": idempotency_key,
        },
    )
    existing = existing_result.mappings().first()
    if existing is None:
        raise RuntimeError("idempotency record was not persisted")
    return dict(existing), False


async def complete_idempotency_record(
    conn: AsyncConnection,
    *,
    record_id: str,
    resource_id: str | None,
    response_status: int,
    safe_response: dict[str, Any],
) -> None:
    await conn.execute(
        text(
            """
            UPDATE idempotency_records
            SET status = 'completed', resource_id = :resource_id, response_status = :response_status,
                safe_response = CAST(:safe_response AS jsonb), updated_at = now()
            WHERE id = :id AND status = 'in_progress'
            """
        ),
        {
            "id": record_id,
            "resource_id": resource_id,
            "response_status": int(response_status),
            "safe_response": json_dumps(safe_response),
        },
    )


async def list_upload_batch_sessions_private(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    batch_id: str,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM upload_sessions
            WHERE tenant_id = :tenant_id AND batch_id = CAST(:batch_id AS uuid)
            ORDER BY created_at, id
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id},
    )
    return [dict(row) for row in result.mappings()]


async def touch_worker_heartbeat(conn: AsyncConnection, *, worker_id: str, lane: str) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO worker_instances(id, lane, last_heartbeat_at, started_at, updated_at)
            VALUES (:id, :lane, now(), now(), now())
            ON CONFLICT (id) DO UPDATE
            SET lane = EXCLUDED.lane, last_heartbeat_at = now(), updated_at = now()
            """
        ),
        {"id": worker_id, "lane": lane},
    )


async def fail_idempotency_record(conn: AsyncConnection, *, record_id: str, safe_response: dict[str, Any]) -> None:
    await conn.execute(
        text(
            """
            UPDATE idempotency_records
            SET status = 'failed', safe_response = CAST(:safe_response AS jsonb), updated_at = now()
            WHERE id = :id AND status = 'in_progress'
            """
        ),
        {"id": record_id, "safe_response": json_dumps(safe_response)},
    )


async def insert_audit_event(
    conn: AsyncConnection,
    *,
    actor: ActorContext | None,
    action: str,
    target_type: str,
    outcome: str,
    tenant_id: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO audit_events(
              id, tenant_id, actor_user_id, actor_session_id, request_id, trace_id,
              action, target_type, target_id, outcome, metadata
            )
            VALUES (
              :id, :tenant_id, :actor_user_id, :actor_session_id, :request_id, :trace_id,
              :action, :target_type, :target_id, :outcome, CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id if tenant_id is not None else actor.active_tenant_id if actor else None,
            "actor_user_id": actor.user_id if actor else None,
            "actor_session_id": actor.session_id if actor else None,
            "request_id": actor.request_id if actor else str(new_uuid()),
            "trace_id": actor.trace_id if actor else "",
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "outcome": outcome,
            "metadata": json_dumps(metadata or {}),
        },
    )


async def load_effective_knowledge_base_role(
    conn: AsyncConnection,
    *,
    user_id: str,
    tenant_id: str,
    knowledge_base_id: str,
) -> KnowledgeBaseRole | None:
    platform_role = await load_platform_role(conn, user_id=user_id)
    if platform_role is None:
        return None
    tenant_role = await load_tenant_role(conn, user_id=user_id, tenant_id=tenant_id)
    direct_user_role = await _load_direct_kb_role(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        subject_type="USER",
        subject_id=user_id,
    )
    local_group_roles = await _load_group_kb_roles(
        conn,
        user_id=user_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        membership_type="LOCAL",
    )
    oidc_group_roles = await _load_group_kb_roles(
        conn,
        user_id=user_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        membership_type="OIDC",
    )
    return effective_knowledge_base_role(
        platform_role=platform_role,
        tenant_role=tenant_role,
        direct_user_role=direct_user_role,
        local_group_roles=local_group_roles,
        oidc_group_roles=oidc_group_roles,
    )


async def load_actor_document_access_scope(
    conn: AsyncConnection,
    *,
    actor: ActorContext,
    tenant_id: str,
    knowledge_base_id: str,
    effective_kb_role: KnowledgeBaseRole | None = None,
) -> DocumentAccessScope:
    kb_role = effective_kb_role
    if kb_role is None and actor.platform_role != PlatformRole.platform_admin:
        kb_role = await load_effective_knowledge_base_role(
            conn,
            user_id=actor.user_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        )
    if document_access_bypass(
        platform_role=actor.platform_role,
        tenant_role=actor.tenant_role,
        kb_role=kb_role,
    ):
        return DocumentAccessScope(bypass=True, tenant_id=tenant_id, user_id=actor.user_id, kb_role=kb_role)
    return DocumentAccessScope(
        bypass=False,
        tenant_id=tenant_id,
        user_id=actor.user_id,
        kb_role=kb_role,
        group_ids=frozenset(await load_actor_group_ids(conn, user_id=actor.user_id, tenant_id=tenant_id)),
    )


async def load_actor_group_ids(conn: AsyncConnection, *, user_id: str, tenant_id: str) -> list[str]:
    result = await conn.execute(
        text(
            """
            SELECT gm.group_id::text AS group_id
            FROM group_memberships gm
            JOIN groups g ON g.id = gm.group_id
            WHERE gm.user_id = :user_id
              AND g.tenant_id = :tenant_id
            ORDER BY gm.group_id::text
            """
        ),
        {"user_id": user_id, "tenant_id": tenant_id},
    )
    return [str(row["group_id"]) for row in result.mappings()]


async def load_platform_role(conn: AsyncConnection, *, user_id: str) -> PlatformRole | None:
    result = await conn.execute(
        text("SELECT platform_role FROM users WHERE id = :user_id AND is_disabled = false"),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    return PlatformRole(str(row["platform_role"])) if row is not None else None


async def load_tenant_role(conn: AsyncConnection, *, user_id: str, tenant_id: str) -> TenantRole | None:
    result = await conn.execute(
        text(
            """
            SELECT role
            FROM tenant_memberships
            WHERE tenant_id = :tenant_id AND user_id = :user_id
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return TenantRole(str(row["role"])) if row is not None else None


async def _load_direct_kb_role(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    subject_type: str,
    subject_id: str,
) -> KnowledgeBaseRole | None:
    result = await conn.execute(
        text(
            """
            SELECT role
            FROM knowledge_base_grants
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND subject_type = :subject_type
              AND subject_id = :subject_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
    )
    row = result.mappings().first()
    return KnowledgeBaseRole(str(row["role"])) if row is not None else None


async def _load_group_kb_roles(
    conn: AsyncConnection,
    *,
    user_id: str,
    tenant_id: str,
    knowledge_base_id: str,
    membership_type: str,
) -> list[KnowledgeBaseRole]:
    result = await conn.execute(
        text(
            """
            SELECT kbg.role
            FROM group_memberships gm
            JOIN knowledge_base_grants kbg
              ON kbg.subject_type = 'GROUP'
             AND kbg.subject_id = gm.group_id::text
            JOIN groups g
              ON g.id = gm.group_id
            WHERE gm.user_id = :user_id
              AND gm.membership_type = :membership_type
              AND g.tenant_id = :tenant_id
              AND kbg.tenant_id = :tenant_id
              AND kbg.knowledge_base_id = :kb_id
            """
        ),
        {
            "user_id": user_id,
            "membership_type": membership_type,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
        },
    )
    return [KnowledgeBaseRole(str(row["role"])) for row in result.mappings()]


async def complete_query_run(
    conn: AsyncConnection,
    *,
    query_run_id: str,
    answer: str,
    usage: dict[str, Any],
    model_alias: str | None = None,
    provider_request_id: str | None = None,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE query_runs
            SET status = 'completed',
                answer = :answer,
                model_alias = :model_alias,
                provider_request_id = :provider_request_id,
                usage = usage || CAST(:usage AS jsonb),
                completed_at = now()
            WHERE id = :id AND status IN ('received', 'running')
            """
        ),
        {
            "id": query_run_id,
            "answer": answer,
            "usage": json_dumps(usage),
            "model_alias": model_alias,
            "provider_request_id": provider_request_id,
        },
    )


async def update_query_run_usage(conn: AsyncConnection, *, query_run_id: str, usage: dict[str, Any]) -> None:
    await conn.execute(
        text(
            """
            UPDATE query_runs
            SET usage = usage || CAST(:usage AS jsonb)
            WHERE id = :id
            """
        ),
        {"id": query_run_id, "usage": json_dumps(usage)},
    )


async def fail_query_run(conn: AsyncConnection, *, query_run_id: str, error_code: str) -> None:
    await conn.execute(
        text(
            """
            UPDATE query_runs
            SET status = 'failed', error_code = :error_code, completed_at = now()
            WHERE id = :id AND status IN ('received', 'running')
            """
        ),
        {"id": query_run_id, "error_code": error_code},
    )


async def cancel_query_run(conn: AsyncConnection, *, query_run_id: str, error_code: str = "CANCELLED") -> None:
    await conn.execute(
        text(
            """
            UPDATE query_runs
            SET status = 'cancelled', error_code = :error_code, completed_at = now()
            WHERE id = :id AND status IN ('received', 'running')
            """
        ),
        {"id": query_run_id, "error_code": error_code},
    )


async def recover_stale_chat_query_runs(conn: AsyncConnection, *, max_age_seconds: int) -> int:
    """Terminalize interrupted streaming chat runs after an API restart.

    Deep Research owns its own durable lease recovery and is intentionally
    excluded.  The conditional status predicate keeps repeated startup calls
    idempotent and never alters completed runs.
    """

    result = await conn.execute(
        text(
            """
            UPDATE query_runs
            SET status = 'failed', error_code = 'STALE_QUERY_RUN_RECOVERED', completed_at = now()
            WHERE status IN ('received', 'running')
              AND mode IN ('normal', 'extended')
              AND COALESCE(started_at, created_at) < now() - make_interval(secs => :max_age_seconds)
            """
        ),
        {"max_age_seconds": max(1, int(max_age_seconds))},
    )
    return max(0, int(getattr(result, "rowcount", 0) or 0))


async def create_research_plan(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_ids: list[str] | None = None,
    user_id: str,
    topic: str,
    retrieval_profile: str,
    tool_mode: str,
    retrieval_overrides: dict[str, Any],
    context_policy: dict[str, Any],
    questions: list[dict[str, Any]],
    notes: str,
) -> uuid.UUID:
    plan_id = new_uuid()
    scope_ids = list(dict.fromkeys(str(item) for item in (knowledge_base_ids or [knowledge_base_id]) if str(item)))
    if knowledge_base_id not in scope_ids:
        scope_ids.insert(0, knowledge_base_id)
    await conn.execute(
        text(
            """
            INSERT INTO research_plans(
              id, tenant_id, knowledge_base_id, user_id, topic, knowledge_base_ids,
              retrieval_profile, tool_mode, retrieval_overrides, context_policy, notes, questions, status
            )
            VALUES (
              :id, :tenant_id, :kb_id, :user_id, :topic, CAST(:knowledge_base_ids AS jsonb),
              :retrieval_profile, :tool_mode, CAST(:retrieval_overrides AS jsonb), CAST(:context_policy AS jsonb),
              :notes, CAST(:questions AS jsonb), 'draft'
            )
            """
        ),
        {
            "id": str(plan_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "user_id": user_id,
            "topic": topic,
            "knowledge_base_ids": json_dumps(scope_ids),
            "retrieval_profile": retrieval_profile,
            "tool_mode": tool_mode,
            "retrieval_overrides": json_dumps(retrieval_overrides),
            "context_policy": json_dumps(context_policy),
            "notes": notes[:4000],
            "questions": json_dumps(questions),
        },
    )
    return plan_id


async def list_research_plans(conn: AsyncConnection, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, tenant_id, knowledge_base_id, user_id, topic, knowledge_base_ids, retrieval_profile,
                   tool_mode, notes, questions, status, approved_run_id, approved_at, created_at, updated_at
            FROM research_plans
            WHERE tenant_id = :tenant_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def get_research_plan(conn: AsyncConnection, *, tenant_id: str, research_plan_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text("SELECT * FROM research_plans WHERE tenant_id = :tenant_id AND id = :id"),
        {"tenant_id": tenant_id, "id": research_plan_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def update_research_plan(
    conn: AsyncConnection,
    *,
    research_plan_id: str,
    topic: str | None = None,
    knowledge_base_id: str | None = None,
    knowledge_base_ids: list[str] | None = None,
    retrieval_profile: str | None = None,
    tool_mode: str | None = None,
    retrieval_overrides: dict[str, Any] | None = None,
    context_policy: dict[str, Any] | None = None,
    questions: list[dict[str, Any]] | None = None,
    notes: str | None = None,
) -> None:
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"id": research_plan_id}
    if topic is not None:
        assignments.append("topic = :topic")
        params["topic"] = topic
    if knowledge_base_id is not None:
        assignments.append("knowledge_base_id = :knowledge_base_id")
        params["knowledge_base_id"] = knowledge_base_id
    if knowledge_base_ids is not None:
        assignments.append("knowledge_base_ids = CAST(:knowledge_base_ids AS jsonb)")
        params["knowledge_base_ids"] = json_dumps(knowledge_base_ids)
    if retrieval_profile is not None:
        assignments.append("retrieval_profile = :retrieval_profile")
        params["retrieval_profile"] = retrieval_profile
    if tool_mode is not None:
        assignments.append("tool_mode = :tool_mode")
        params["tool_mode"] = tool_mode
    if retrieval_overrides is not None:
        assignments.append("retrieval_overrides = CAST(:retrieval_overrides AS jsonb)")
        params["retrieval_overrides"] = json_dumps(retrieval_overrides)
    if context_policy is not None:
        assignments.append("context_policy = CAST(:context_policy AS jsonb)")
        params["context_policy"] = json_dumps(context_policy)
    if questions is not None:
        assignments.append("questions = CAST(:questions AS jsonb)")
        params["questions"] = json_dumps(questions)
    if notes is not None:
        assignments.append("notes = :notes")
        params["notes"] = notes[:4000]
    await conn.execute(
        text(f"UPDATE research_plans SET {', '.join(assignments)} WHERE id = :id"),  # noqa: S608
        params,
    )


async def approve_research_plan(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_plan_id: str,
    approved_by_user_id: str,
    run_id: str,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE research_plans
            SET status = 'approved',
                approved_run_id = :run_id,
                approved_at = now(),
                approved_by_user_id = :approved_by_user_id,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :id
            """
        ),
        {
            "tenant_id": tenant_id,
            "id": research_plan_id,
            "approved_by_user_id": approved_by_user_id,
            "run_id": run_id,
        },
    )


async def create_research_run(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_ids: list[str] | None = None,
    user_id: str,
    topic: str,
    retrieval_profile: str,
    tool_mode: str,
    retrieval_overrides: dict[str, Any],
    context_policy: dict[str, Any],
    questions: list[str],
    research_plan_id: str | None = None,
    model_config_revision_id: str | None = None,
    model_config_hash: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = new_uuid()
    job_id = new_uuid()
    scope_ids = list(dict.fromkeys(str(item) for item in (knowledge_base_ids or [knowledge_base_id]) if str(item)))
    if knowledge_base_id not in scope_ids:
        scope_ids.insert(0, knowledge_base_id)
    if not 1 <= len(scope_ids) <= 3:
        raise ValueError("research run scope must contain between one and three knowledge bases")
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress,
              model_config_revision_id, model_config_hash)
            VALUES (:id, :tenant_id, :kb_id, 'deep_research', 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb),
                    :model_config_revision_id, :model_config_hash)
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "config": json_dumps(
                {
                    "research_run_id": str(run_id),
                    "knowledge_base_ids": scope_ids,
                    "tool_mode": tool_mode,
                    "research_plan_id": research_plan_id,
                }
            ),
            "progress": json_dumps(
                {
                    "stage": "received",
                    "research_run_id": str(run_id),
                    "knowledge_base_ids": scope_ids,
                    "tool_mode": tool_mode,
                    "research_plan_id": research_plan_id,
                }
            ),
            "model_config_revision_id": model_config_revision_id,
            "model_config_hash": model_config_hash,
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO research_runs(
              id, tenant_id, knowledge_base_id, user_id, active_job_id, topic, retrieval_profile,
              tool_mode, retrieval_overrides, status, progress, checkpoint, context_policy, research_plan_id,
              model_config_revision_id, model_config_hash
            )
            VALUES (
              :id, :tenant_id, :kb_id, :user_id, :job_id, :topic, :profile,
              :tool_mode, CAST(:overrides AS jsonb), 'received', CAST(:progress AS jsonb),
              CAST(:checkpoint AS jsonb), CAST(:context_policy AS jsonb), :research_plan_id,
              :model_config_revision_id, :model_config_hash
            )
            """
        ),
        {
            "id": str(run_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "user_id": user_id,
            "job_id": str(job_id),
            "topic": topic,
            "profile": retrieval_profile,
            "tool_mode": tool_mode,
            "overrides": json_dumps(retrieval_overrides),
            "progress": json_dumps(
                {
                    "stage": "received",
                    "questions_total": len(questions),
                    "tool_mode": tool_mode,
                    "research_plan_id": research_plan_id,
                }
            ),
            "checkpoint": json_dumps({}),
            "context_policy": json_dumps(context_policy),
            "research_plan_id": research_plan_id,
            "model_config_revision_id": model_config_revision_id,
            "model_config_hash": model_config_hash,
        },
    )
    for ordinal, scope_id in enumerate(scope_ids, start=1):
        await conn.execute(
            text(
                """
                INSERT INTO research_run_scopes(
                  id, tenant_id, research_run_id, knowledge_base_id, ordinal, access_snapshot
                )
                VALUES (:id, :tenant_id, :run_id, :kb_id, :ordinal, CAST(:snapshot AS jsonb))
                """
            ),
            {
                "id": str(new_uuid()),
                "tenant_id": tenant_id,
                "run_id": str(run_id),
                "kb_id": scope_id,
                "ordinal": ordinal,
                "snapshot": json_dumps({"version": "research_scope_snapshot_v1", "role": "viewer"}),
            },
        )
    for ordinal, question in enumerate(questions, start=1):
        await conn.execute(
            text(
                """
                INSERT INTO research_questions(
                  id, tenant_id, research_run_id, question, ordinal, kind, status,
                  execution_state, outcome, attempt_count, rewrite_count, depth, budget, acceptance, metadata
                )
                VALUES (
                  :id, :tenant_id, :run_id, :question, :ordinal, :kind, 'open',
                  'pending', NULL, 0, 0, 0, CAST(:budget AS jsonb),
                  CAST(:acceptance AS jsonb), CAST(:metadata AS jsonb)
                )
                """
            ),
            {
                "id": str(new_uuid()),
                "tenant_id": tenant_id,
                "run_id": str(run_id),
                "question": question,
                "ordinal": ordinal,
                "kind": "primary" if ordinal == 1 else "decomposition",
                "acceptance": json_dumps({"requires_evidence": True, "priority": "required"}),
                "budget": json_dumps({}),
                "metadata": json_dumps({}),
            },
        )
    return run_id, job_id


async def create_research_resume_job(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    research_run_id: str,
    knowledge_base_ids: list[str] | None = None,
    tool_mode: str,
) -> uuid.UUID:
    existing = await conn.execute(
        text(
            """
            SELECT id
            FROM ingestion_jobs
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND kind = 'deep_research'
              AND status IN ('received','running')
              AND cancel_requested = false
              AND config ->> 'research_run_id' = :run_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "run_id": research_run_id},
    )
    row = existing.mappings().first()
    if row is not None:
        return uuid.UUID(str(row["id"]))
    job_id = new_uuid()
    scope_ids = list(dict.fromkeys(str(item) for item in (knowledge_base_ids or [knowledge_base_id]) if str(item)))
    if knowledge_base_id not in scope_ids:
        scope_ids.insert(0, knowledge_base_id)
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress)
            VALUES (:id, :tenant_id, :kb_id, 'deep_research', 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb))
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "config": json_dumps(
                {"research_run_id": research_run_id, "knowledge_base_ids": scope_ids, "tool_mode": tool_mode}
            ),
            "progress": json_dumps(
                {
                    "stage": "resume_received",
                    "research_run_id": research_run_id,
                    "knowledge_base_ids": scope_ids,
                    "tool_mode": tool_mode,
                }
            ),
        },
    )
    await conn.execute(
        text(
            """
            UPDATE research_runs
            SET active_job_id = :job_id,
                status = 'received',
                pause_requested = false,
                cancel_requested = false,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :run_id
            """
        ),
        {"job_id": str(job_id), "tenant_id": tenant_id, "run_id": research_run_id},
    )
    return job_id


async def list_research_runs(conn: AsyncConnection, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, tenant_id, knowledge_base_id, user_id, active_job_id, topic, retrieval_profile,
                   tool_mode, status, progress, stop_reason, error_code, created_at, updated_at, completed_at,
                   research_plan_id
            FROM research_runs
            WHERE tenant_id = :tenant_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def load_research_run_scopes(
    conn: AsyncConnection, *, tenant_id: str, research_run_id: str
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, tenant_id, research_run_id, knowledge_base_id, ordinal, access_snapshot, created_at
            FROM research_run_scopes
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY ordinal
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id},
    )
    return [dict(row) for row in result.mappings()]


async def get_research_run(conn: AsyncConnection, *, tenant_id: str, research_run_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text("SELECT * FROM research_runs WHERE tenant_id = :tenant_id AND id = :id"),
        {"tenant_id": tenant_id, "id": research_run_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def load_research_run_questions(
    conn: AsyncConnection, *, tenant_id: str, research_run_id: str
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, question, ordinal, kind, status, acceptance, metadata,
                   execution_state, outcome, attempt_count, rewrite_count, depth, budget, created_at, updated_at
            FROM research_questions
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY ordinal
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id},
    )
    return [dict(row) for row in result.mappings()]


async def load_next_research_question(
    conn: AsyncConnection, *, tenant_id: str, research_run_id: str
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM research_questions
            WHERE tenant_id = :tenant_id
              AND research_run_id = :run_id
              AND execution_state IN ('pending','running')
              AND status IN ('open','running','partial','conflicting')
            ORDER BY created_at, id
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id},
    )
    return select_next_question([dict(row) for row in result.mappings()])


async def load_resumable_research_episode(
    conn: AsyncConnection, *, tenant_id: str, research_run_id: str
) -> dict[str, Any] | None:
    """Return the oldest non-terminal episode before selecting a new question.

    A worker restart must continue the durable episode instead of deriving a
    new episode index from the number of rows already visible.  The row is
    intentionally not locked here: the run lease is the cross-worker guard;
    stage transitions use their own compare-and-set predicate.
    """
    result = await conn.execute(
        text(
            """
            SELECT id, query_run_id, episode_index, question_id, status, stage,
                   context_summary, metrics, error_code, created_at, completed_at
            FROM research_episodes
            WHERE tenant_id = :tenant_id
              AND research_run_id = :run_id
              AND status NOT IN ('completed','failed','cancelled')
              AND stage NOT IN ('completed','failed')
            ORDER BY episode_index, created_at, id
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


def select_next_question(questions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the next executable question with deterministic priority and tie-breaks."""
    active = [
        row
        for row in questions
        if str(row.get("execution_state") or "pending") in {"pending", "running"}
        and str(row.get("status") or "open") in {"open", "running", "partial", "conflicting"}
    ]
    if not active:
        return None
    return sorted(
        active,
        key=lambda row: (
            _research_question_priority(row),
            _stable_created_at(row.get("created_at")),
            str(row.get("id") or ""),
        ),
    )[0]


def _research_question_priority(row: dict[str, Any]) -> int:
    raw_acceptance: Any = row.get("acceptance")
    raw_metadata: Any = row.get("metadata")
    acceptance: dict[str, Any] = raw_acceptance if isinstance(raw_acceptance, dict) else {}
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    explicit = str(acceptance.get("priority") or metadata.get("priority") or "").casefold()
    if explicit == "required":
        return 0
    if explicit == "bridge":
        return 1
    if explicit == "normal":
        return 2
    if str(row.get("kind") or "").casefold() in {"primary", "decomposition"}:
        return 0
    rationale = str(metadata.get("rationale") or "").casefold()
    needed = " ".join(str(item) for item in acceptance.get("needed_evidence") or []).casefold()
    if "bridge" in rationale or any(token in f"{rationale} {needed}" for token in ("blocking", "approval", "owner")):
        return 1
    return 2


def _stable_created_at(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


async def transition_research_question(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    question_id: str,
    execution_state: str,
    outcome: str | None,
    reason: str | None = None,
    error_code: str | None = None,
) -> None:
    if execution_state not in {"pending", "running", "done"}:
        raise ValueError("invalid research question execution_state")
    if outcome not in {None, "covered", "partial", "exhausted", "failed"}:
        raise ValueError("invalid research question outcome")
    if (execution_state == "done") != (outcome is not None):
        raise ValueError("done questions require an outcome and non-done questions require null outcome")
    status = str(outcome) if execution_state == "done" else ("running" if execution_state == "running" else "open")
    metadata_update: dict[str, str] = {}
    if reason:
        metadata_update["termination_reason"] = reason[:240]
    if error_code:
        metadata_update["error_code"] = error_code[:120]
    await conn.execute(
        text(
            """
            UPDATE research_questions
            SET execution_state = :execution_state,
                outcome = :outcome,
                status = :status,
                metadata = CASE WHEN CAST(:metadata_update AS jsonb) = '{}'::jsonb
                                THEN metadata ELSE metadata || CAST(:metadata_update AS jsonb) END,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id AND id = :question_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "question_id": question_id,
            "execution_state": execution_state,
            "outcome": outcome,
            "status": status,
            "metadata_update": json_dumps(metadata_update),
        },
    )


async def initialize_research_question_budget(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    question_id: str,
    budget: dict[str, int],
) -> None:
    safe_budget = {key: int(budget[key]) for key in ("max_attempts", "max_rewrites", "max_depth") if key in budget}
    await conn.execute(
        text(
            """
            UPDATE research_questions
            SET budget = CASE WHEN budget = '{}'::jsonb THEN CAST(:budget AS jsonb) ELSE budget END,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id AND id = :question_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "question_id": question_id,
            "budget": json_dumps(safe_budget),
        },
    )


async def record_research_question_attempt(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    question_id: str,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE research_questions
            SET execution_state = 'running',
                outcome = NULL,
                status = 'running',
                attempt_count = attempt_count + 1,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id AND id = :question_id
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id, "question_id": question_id},
    )


async def record_research_question_rewrites(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    question_id: str,
    rewrite_count: int,
) -> None:
    if rewrite_count < 0:
        raise ValueError("rewrite_count must be non-negative")
    if rewrite_count == 0:
        return
    await conn.execute(
        text(
            """
            UPDATE research_questions
            SET rewrite_count = rewrite_count + :rewrite_count,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id AND id = :question_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "question_id": question_id,
            "rewrite_count": rewrite_count,
        },
    )


async def terminalize_research_questions(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    reason: str,
    required_only: bool = False,
    outcome: str = "exhausted",
) -> int:
    if outcome not in {"exhausted", "failed"}:
        raise ValueError("terminal question outcome must be exhausted or failed")
    questions = await load_research_run_questions(conn, tenant_id=tenant_id, research_run_id=research_run_id)
    count = 0
    for row in questions:
        if str(row.get("execution_state") or "pending") == "done":
            continue
        if required_only and _research_question_priority(row) != 0:
            continue
        await transition_research_question(
            conn,
            tenant_id=tenant_id,
            research_run_id=research_run_id,
            question_id=str(row["id"]),
            execution_state="done",
            outcome=outcome,
            reason=reason,
        )
        count += 1
    return count


async def append_research_questions(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    questions: list[Any],
    source_episode_id: str | None = None,
    source_evidence_ids: list[str] | None = None,
    max_total_questions: int = 24,
    max_append: int = 5,
    source_depth: int = 0,
    max_depth: int = 3,
) -> list[str]:
    if max_total_questions < 1:
        raise ValueError("max_total_questions must be >= 1")
    if max_append < 1:
        return []
    existing = await load_research_run_questions(conn, tenant_id=tenant_id, research_run_id=research_run_id)
    seen = {
        _research_question_duplicate_key(str(row.get("question") or ""))
        for row in existing
        if str(row.get("question") or "")
    }
    ordinal_result = await conn.execute(
        text(
            """
            SELECT COALESCE(MAX(ordinal), 0) AS max_ordinal, COUNT(*) AS total
            FROM research_questions
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id},
    )
    ordinal_row = ordinal_result.mappings().first()
    max_ordinal = int(ordinal_row["max_ordinal"]) if ordinal_row is not None else len(existing)
    total = int(ordinal_row["total"]) if ordinal_row is not None else len(existing)
    remaining = max(0, max_total_questions - total)
    inserted: list[str] = []
    next_ordinal = max_ordinal + 1
    for raw in questions:
        if len(inserted) >= min(max_append, remaining):
            break
        payload = _research_question_payload(raw)
        question = str(payload.get("question") or "").strip()
        if not question:
            continue
        duplicate_key = _research_question_duplicate_key(question)
        if not duplicate_key or duplicate_key in seen:
            continue
        question_id = str(stable_uuid(["research_question_v2", research_run_id, duplicate_key]))
        question_depth = int(payload.get("depth") or source_depth + 1)
        if question_depth > max_depth:
            continue
        priority = _derived_question_priority(payload)
        acceptance = {
            "requires_evidence": True,
            "priority": priority,
            "needed_evidence": list(payload.get("needed_evidence") or []),
        }
        metadata = _assert_public_safe_metadata(
            {
                "source": "research_planner_v1",
                "lineage": {
                    "source_episode_id": source_episode_id,
                    "source_evidence_ids": list(source_evidence_ids or []),
                },
                "duplicate_key": duplicate_key,
                "rationale": str(payload.get("rationale") or "")[:1000],
                "priority": priority,
                "depth": question_depth,
            }
        )
        await conn.execute(
            text(
                """
                INSERT INTO research_questions(
                  id, tenant_id, research_run_id, question, ordinal, kind, status,
                  execution_state, outcome, attempt_count, rewrite_count, depth, budget, acceptance, metadata
                )
                VALUES (
                  :id, :tenant_id, :run_id, :question, :ordinal, 'derived', 'open',
                  'pending', NULL, 0, 0, :depth, CAST(:budget AS jsonb),
                  CAST(:acceptance AS jsonb), CAST(:metadata AS jsonb)
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": question_id,
                "tenant_id": tenant_id,
                "run_id": research_run_id,
                "question": question,
                "ordinal": next_ordinal,
                "depth": question_depth,
                "budget": json_dumps({}),
                "acceptance": json_dumps(acceptance),
                "metadata": json_dumps(metadata),
            },
        )
        inserted.append(question_id)
        seen.add(duplicate_key)
        next_ordinal += 1
    return inserted


async def create_research_tool_call(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    episode_id: str | None,
    question_id: str | None,
    query_run_id: str | None,
    tool_name: str,
    tool_query_hash: str,
    tool_args_hash: str | None = None,
    validated_args: dict[str, Any] | None = None,
    safe_metadata: dict[str, Any],
) -> uuid.UUID:
    call_id = stable_uuid(
        [
            "research_tool_call_v1",
            research_run_id,
            episode_id or "",
            question_id or "",
            query_run_id or "",
            tool_name,
            tool_query_hash,
            tool_args_hash or "",
        ]
    )
    insert_result = await conn.execute(
        text(
            """
            INSERT INTO research_tool_calls(
              id, tenant_id, research_run_id, episode_id, question_id, query_run_id,
              tool_name, tool_query_hash, tool_args_hash, status, safe_metadata, validated_args,
              execution_attempts, last_heartbeat_at, started_at
            )
            VALUES (
              :id, :tenant_id, :run_id, :episode_id, :question_id, :query_run_id,
              :tool_name, :tool_query_hash, :tool_args_hash, 'running', CAST(:safe_metadata AS jsonb),
              CAST(:validated_args AS jsonb), 1, now(), now()
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": str(call_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "episode_id": episode_id,
            "question_id": question_id,
            "query_run_id": query_run_id,
            "tool_name": tool_name,
            "tool_query_hash": tool_query_hash,
            "tool_args_hash": tool_args_hash,
            "safe_metadata": json_dumps(_assert_public_safe_metadata(safe_metadata)),
            "validated_args": json_dumps(validated_args or {}),
        },
    )
    inserted = insert_result.mappings().first() is not None
    existing = await conn.execute(
        text(
            """
            SELECT tool_name, tool_query_hash, tool_args_hash
            FROM research_tool_calls
            WHERE id = :id
            """
        ),
        {"id": str(call_id)},
    )
    existing_row = existing.mappings().first()
    if existing_row is not None:
        if (
            str(existing_row.get("tool_name") or "") != tool_name
            or str(existing_row.get("tool_query_hash") or "") != tool_query_hash
            or str(existing_row.get("tool_args_hash") or "") != str(tool_args_hash or "")
        ):
            raise ValueError("research tool call identity does not match the existing ledger row")
        if not inserted:
            await conn.execute(
                text(
                    """
                    UPDATE research_tool_calls
                    SET execution_attempts = execution_attempts + 1,
                        last_heartbeat_at = now(),
                        updated_at = now()
                    WHERE id = :id AND status IN ('running','stalled')
                    """
                ),
                {"id": str(call_id)},
            )
    return call_id


async def update_research_tool_call(
    conn: AsyncConnection,
    *,
    tool_call_id: str,
    status: str,
    result_summary: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE research_tool_calls
            SET status = :status,
                result_summary = CASE
                  WHEN CAST(:result_summary AS jsonb) = '{}'::jsonb THEN result_summary
                  ELSE CAST(:result_summary AS jsonb)
                END,
                error_code = :error_code,
                error_message = :error_message,
                completed_at = CASE
                  WHEN :status IN ('completed','failed','cancelled','stalled') THEN now()
                  ELSE completed_at
                END,
                updated_at = now()
            WHERE id = :id
              AND (status <> 'completed' OR :status = 'completed')
            """
        ),
        {
            "id": tool_call_id,
            "status": status,
            "result_summary": json_dumps(_assert_public_safe_metadata(result_summary or {})),
            "error_code": error_code[:120] if error_code else None,
            "error_message": error_message[:1000] if error_message else None,
        },
    )


async def touch_research_heartbeat(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    episode_id: str | None = None,
    tool_call_id: str | None = None,
    lease_id: str | None = None,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE research_runs
            SET last_heartbeat_at = now(),
                controller_lease_expires_at = CASE
                  WHEN CAST(:lease_id AS text) IS NULL
                       OR controller_lease_id <> CAST(:lease_id AS text)
                    THEN controller_lease_expires_at
                  ELSE now() + interval '120 seconds'
                END,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :run_id
              AND (CAST(:lease_id AS text) IS NULL OR controller_lease_id = CAST(:lease_id AS text))
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id, "lease_id": lease_id},
    )
    if episode_id:
        await conn.execute(
            text("UPDATE research_episodes SET updated_at = now() WHERE tenant_id = :tenant_id AND id = :id"),
            {"tenant_id": tenant_id, "id": episode_id},
        )
    if tool_call_id:
        await conn.execute(
            text(
                """
                UPDATE research_tool_calls
                SET last_heartbeat_at = now(), updated_at = now()
                WHERE tenant_id = :tenant_id AND id = :id AND status = 'running'
                """
            ),
            {"tenant_id": tenant_id, "id": tool_call_id},
        )


async def mark_stalled_research_tool_calls(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    heartbeat_seconds: int,
) -> int:
    result = await conn.execute(
        text(
            """
            UPDATE research_tool_calls
            SET status = 'stalled',
                error_code = 'research_heartbeat_missing',
                error_message = 'research tool heartbeat was not observed',
                completed_at = now(),
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND research_run_id = :run_id
              AND status = 'running'
              AND COALESCE(last_heartbeat_at, started_at) < now() - (:heartbeat_seconds * interval '1 second')
            """
        ),
        {
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "heartbeat_seconds": heartbeat_seconds,
        },
    )
    return int(result.rowcount or 0)


async def acquire_research_run_lease(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    lease_id: str,
    lease_seconds: int = 120,
) -> bool:
    result = await conn.execute(
        text(
            """
            UPDATE research_runs
            SET controller_lease_id = :lease_id,
                controller_lease_expires_at = now() + (:lease_seconds * interval '1 second'),
                controller_lease_epoch = controller_lease_epoch + 1,
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND id = :run_id
              AND status IN ('received','running','paused')
              AND (
                controller_lease_id IS NULL
                OR controller_lease_expires_at IS NULL
                OR controller_lease_expires_at < now()
                OR controller_lease_id = :lease_id
              )
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "lease_id": lease_id,
            "lease_seconds": max(15, int(lease_seconds)),
        },
    )
    return result.mappings().first() is not None


async def assert_research_run_lease(
    conn: AsyncConnection, *, tenant_id: str, research_run_id: str, lease_id: str
) -> int:
    """Fence a stale worker before a durable controller write."""
    result = await conn.execute(
        text(
            """
            SELECT controller_lease_epoch
            FROM research_runs
            WHERE tenant_id = :tenant_id
              AND id = :run_id
              AND controller_lease_id = :lease_id
              AND controller_lease_expires_at > now()
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id, "lease_id": lease_id},
    )
    row = result.mappings().first()
    if row is None:
        raise PermissionError("research controller lease is no longer owned")
    return int(row.get("controller_lease_epoch") or 0)


async def release_research_run_lease(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    lease_id: str,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE research_runs
            SET controller_lease_id = NULL,
                controller_lease_expires_at = NULL,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :run_id AND controller_lease_id = :lease_id
            """
        ),
        {"tenant_id": tenant_id, "run_id": research_run_id, "lease_id": lease_id},
    )


async def update_research_run(
    conn: AsyncConnection,
    *,
    research_run_id: str,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    final_report: dict[str, Any] | None = None,
    stop_reason: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    clear_error: bool = False,
    pause_requested: bool | None = None,
    cancel_requested: bool | None = None,
) -> None:
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"id": research_run_id}
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status
        if status == "running":
            assignments.append("started_at = COALESCE(started_at, now())")
        if status in {"completed", "failed", "cancelled"}:
            assignments.append("completed_at = now()")
    for key, value in {
        "progress": progress,
        "checkpoint": checkpoint,
        "final_report": final_report,
    }.items():
        if value is not None:
            assignments.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json_dumps(value)
    if stop_reason is not None:
        assignments.append("stop_reason = :stop_reason")
        params["stop_reason"] = stop_reason[:120]
    if error_code is not None:
        assignments.append("error_code = :error_code")
        params["error_code"] = error_code[:120]
    if error_message is not None:
        assignments.append("error_message = :error_message")
        params["error_message"] = error_message[:1000]
    if clear_error:
        # Do not emit two assignments for the same column.  PostgreSQL rejects
        # `error_code = :error_code, error_code = NULL` with ProgrammingError;
        # an explicit error wins over a request to clear stale errors.
        if error_code is None:
            assignments.append("error_code = NULL")
        if error_message is None:
            assignments.append("error_message = NULL")
    if pause_requested is not None:
        assignments.append("pause_requested = :pause_requested")
        params["pause_requested"] = pause_requested
    if cancel_requested is not None:
        assignments.append("cancel_requested = :cancel_requested")
        params["cancel_requested"] = cancel_requested
    await conn.execute(
        text(f"UPDATE research_runs SET {', '.join(assignments)} WHERE id = :id"),  # noqa: S608
        params,
    )


async def request_research_pause(conn: AsyncConnection, *, tenant_id: str, research_run_id: str) -> None:
    await conn.execute(
        text(
            """
            UPDATE research_runs
            SET pause_requested = true,
                status = CASE WHEN status = 'received' THEN 'paused' ELSE status END,
                progress = CASE
                  WHEN status = 'received' THEN jsonb_build_object('stage', 'paused')
                  ELSE progress
                END,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :id AND status IN ('received','running')
            """
        ),
        {"tenant_id": tenant_id, "id": research_run_id},
    )
    await conn.execute(
        text(
            """
            UPDATE ingestion_jobs
            SET cancel_requested = true,
                status = CASE WHEN status = 'received' THEN 'cancelled' ELSE status END,
                progress = CASE
                  WHEN status = 'received' THEN jsonb_build_object('stage', 'paused_before_start')
                  ELSE progress
                END,
                completed_at = CASE WHEN status = 'received' THEN now() ELSE completed_at END,
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND kind = 'deep_research'
              AND status IN ('received','running')
              AND config ->> 'research_run_id' = :id
            """
        ),
        {"tenant_id": tenant_id, "id": research_run_id},
    )


async def request_research_cancel(conn: AsyncConnection, *, tenant_id: str, research_run_id: str) -> None:
    await conn.execute(
        text(
            """
            UPDATE research_runs
            SET cancel_requested = true,
                pause_requested = false,
                status = CASE WHEN status IN ('received','paused') THEN 'cancelled' ELSE status END,
                progress = CASE
                  WHEN status IN ('received','paused') THEN jsonb_build_object('stage', 'cancelled')
                  ELSE progress
                END,
                completed_at = CASE WHEN status IN ('received','paused') THEN now() ELSE completed_at END,
                updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :id AND status IN ('received','running','paused')
            """
        ),
        {"tenant_id": tenant_id, "id": research_run_id},
    )
    await conn.execute(
        text(
            """
            UPDATE ingestion_jobs
            SET cancel_requested = true,
                status = CASE WHEN status = 'received' THEN 'cancelled' ELSE status END,
                progress = CASE
                  WHEN status = 'received' THEN jsonb_build_object('stage', 'cancelled_before_start')
                  ELSE progress
                END,
                completed_at = CASE WHEN status = 'received' THEN now() ELSE completed_at END,
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND kind = 'deep_research'
              AND status IN ('received','running')
              AND config ->> 'research_run_id' = :id
            """
        ),
        {"tenant_id": tenant_id, "id": research_run_id},
    )


async def create_research_episode(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    episode_index: int,
    question_id: str | None,
    query_run_id: str | None,
    context_summary: dict[str, Any],
) -> uuid.UUID:
    episode_id = stable_uuid(["research_episode_v1", research_run_id, episode_index])
    result = await conn.execute(
        text(
            """
            INSERT INTO research_episodes(
              id, tenant_id, research_run_id, query_run_id, episode_index, question_id,
              status, stage, context_summary, started_at
            )
            VALUES (
              :id, :tenant_id, :run_id, :query_run_id, :episode_index, :question_id,
              'running', 'claimed', CAST(:context_summary AS jsonb), now()
            )
            ON CONFLICT (research_run_id, episode_index) DO NOTHING
            RETURNING id, question_id, query_run_id, status, stage
            """
        ),
        {
            "id": str(episode_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "query_run_id": query_run_id,
            "episode_index": episode_index,
            "question_id": question_id,
            "context_summary": json_dumps(context_summary),
        },
    )
    mappings = getattr(result, "mappings", None)
    row = mappings().first() if callable(mappings) else None
    inserted = row is not None
    if row is None:
        existing = await conn.execute(
            text(
                """
                SELECT id, question_id, query_run_id, status, stage
                FROM research_episodes
                WHERE tenant_id = :tenant_id AND research_run_id = :run_id AND episode_index = :episode_index
                """
            ),
            {"tenant_id": tenant_id, "run_id": research_run_id, "episode_index": episode_index},
        )
        row = existing.mappings().first()
        # Lightweight unit-test connections do not implement RETURNING.  A
        # real PostgreSQL connection will always return either the inserted
        # row or the existing conflict row above.
        if row is None:
            inserted = True
    if row is not None:
        existing_question_id = row.get("question_id")
        if existing_question_id is not None and str(existing_question_id) != str(question_id):
            raise ValueError("research episode question does not match the existing episode index")
        existing_query_run_id = row.get("query_run_id")
        if existing_query_run_id is not None and str(existing_query_run_id) != str(query_run_id):
            raise ValueError("research episode query run does not match the existing episode index")
        if row.get("id") is not None:
            episode_id = uuid.UUID(str(row["id"]))
        if str(row.get("status") or "") == "completed":
            return episode_id
    # The attempt counter is part of the episode claim.  It is changed only
    # when the episode row was newly inserted, so a retry cannot spend an
    # extra attempt before it has a durable episode to resume.
    if inserted and question_id is not None:
        await conn.execute(
            text(
                """
                UPDATE research_questions
                SET status = 'running',
                    execution_state = 'running',
                    outcome = NULL,
                    attempt_count = attempt_count + 1,
                    updated_at = now()
                WHERE tenant_id = :tenant_id AND research_run_id = :run_id AND id = :question_id
                RETURNING id
                """
            ),
            {"tenant_id": tenant_id, "run_id": research_run_id, "question_id": question_id},
        )
    return episode_id


async def update_research_episode(
    conn: AsyncConnection,
    *,
    episode_id: str,
    status: str,
    stage: str,
    metrics: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    expected_stage: str | None = None,
) -> None:
    if not str(episode_id).strip():
        raise ValueError("episode_id is required when updating a research episode")
    if stage not in {
        "claimed",
        "tool_registered",
        "retrieving",
        "evidence_persisted",
        "evaluated",
        "completed",
        "failed",
    }:
        raise ValueError("invalid research episode stage")
    result = await conn.execute(
        text(
            """
            UPDATE research_episodes
            SET status = CASE
                  WHEN :status IN ('running','completed','failed','cancelled') THEN :status
                  ELSE status
                END,
                stage = CASE
                  WHEN :status = 'failed' THEN 'failed'
                  ELSE :stage
                END,
                metrics = CASE WHEN CAST(:metrics AS jsonb) = '{}'::jsonb THEN metrics ELSE CAST(:metrics AS jsonb) END,
                error_code = :error_code,
                error_message = :error_message,
                completed_at = CASE WHEN :status IN ('completed','failed','cancelled') THEN now() ELSE completed_at END,
                updated_at = now()
            WHERE id = :id
              AND NOT (status = 'completed' AND :status <> 'completed')
              AND (
                :status = 'failed'
                OR array_position(
                  ARRAY['received','claimed','tool_registered','retrieving','evidence_persisted','evaluated','completed'],
                  :stage
                ) >= COALESCE(array_position(
                  ARRAY['received','claimed','tool_registered','retrieving','evidence_persisted','evaluated','completed'],
                  stage
                ), 1)
              )
              AND (CAST(:expected_stage AS text) IS NULL OR stage = CAST(:expected_stage AS text))
            """
        ),
        {
            "id": episode_id,
            "status": status,
            "stage": stage,
            "metrics": json_dumps(metrics or {}),
            "error_code": error_code,
            "error_message": error_message[:1000] if error_message else None,
            "expected_stage": expected_stage,
        },
    )
    rowcount = getattr(result, "rowcount", None)
    if expected_stage is not None and rowcount == 0:
        current = await conn.execute(
            text("SELECT status, stage FROM research_episodes WHERE id = :id"),
            {"id": episode_id},
        )
        row = current.mappings().first()
        if row is None:
            raise LookupError("research episode does not exist")
        stage_order = {
            "received": 0,
            "claimed": 1,
            "tool_registered": 2,
            "retrieving": 3,
            "evidence_persisted": 4,
            "evaluated": 5,
            "completed": 6,
        }
        current_stage = str(row.get("stage") or "")
        if str(row.get("status") or "") == "completed" or (
            current_stage in stage_order and stage_order[current_stage] >= stage_order.get(stage, -1)
        ):
            return
        raise RuntimeError("research episode stage changed before compare-and-set transition")


async def upsert_research_evidence_record(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    question_id: str | None,
    chunk_id: str,
    document_id: str | None,
    document_version_id: str | None,
    knowledge_base_id: str,
    title: str,
    source_url: str,
    section_path: list[str],
    content_abstract: str,
    support_status: str,
    score: float | None,
    metadata: dict[str, Any],
    evidence_fingerprint: str | None = None,
    evidence_ref: str | None = None,
) -> uuid.UUID:
    fingerprint = evidence_fingerprint or research_evidence_fingerprint(
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        document_version_id=document_version_id,
        chunk_id=chunk_id,
        source_url=source_url,
        title=title,
        content_abstract=content_abstract,
    )
    record_id = stable_uuid([research_run_id, chunk_id])
    computed_evidence_ref = research_evidence_ref(record_id)
    # Kept as a compatibility-only argument for older callers; persistence
    # always uses the server-derived value.
    evidence_ref = computed_evidence_ref
    result = await conn.execute(
        text(
            """
            INSERT INTO research_evidence_records(
              id, tenant_id, research_run_id, question_id, chunk_id, document_id, document_version_id,
              knowledge_base_id, evidence_ref, title, source_url, section_path, content_abstract,
              support_status, score, evidence_fingerprint, metadata
            )
            VALUES (
              :id, :tenant_id, :run_id, :question_id, :chunk_id, :document_id, :document_version_id,
              :kb_id, :evidence_ref, :title, :source_url, :section_path, :abstract,
              :support_status, :score, :evidence_fingerprint, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (research_run_id, chunk_id) DO NOTHING
            RETURNING id, evidence_fingerprint
            """
        ),
        {
            "id": str(record_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "question_id": question_id,
            "chunk_id": chunk_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "kb_id": knowledge_base_id,
            "evidence_ref": evidence_ref,
            "title": title,
            "source_url": source_url,
            "section_path": section_path,
            "abstract": content_abstract,
            "support_status": support_status,
            "score": score,
            "evidence_fingerprint": fingerprint,
            "metadata": json_dumps(metadata),
        },
    )
    row = result.mappings().first()
    if row is None:
        existing = await conn.execute(
            text(
                """
                SELECT id, question_id, document_id, document_version_id,
                       knowledge_base_id, evidence_ref, evidence_fingerprint
                FROM research_evidence_records
                WHERE tenant_id = :tenant_id
                  AND research_run_id = :run_id
                  AND chunk_id = :chunk_id
                """
            ),
            {"tenant_id": tenant_id, "run_id": research_run_id, "chunk_id": chunk_id},
        )
        row = existing.mappings().first()
    if row is not None:
        stored_ref = str(row.get("evidence_ref") or "")
        if stored_ref and stored_ref != evidence_ref:
            raise ValueError("evidence citation conflicts with the existing record identity")
        stored_fingerprint = str(row.get("evidence_fingerprint") or "")
        if stored_fingerprint and stored_fingerprint != fingerprint:
            raise ValueError("evidence identity conflicts with the existing chunk record")
        if str(row.get("knowledge_base_id") or knowledge_base_id) != knowledge_base_id:
            raise ValueError("evidence identity conflicts with the existing knowledge-base scope")
        if row.get("document_id") is not None and str(row["document_id"]) != str(document_id or ""):
            raise ValueError("evidence identity conflicts with the existing document")
        if row.get("id") is not None:
            record_id = uuid.UUID(str(row["id"]))
        await conn.execute(
            text(
                """
                UPDATE research_evidence_records
                SET question_id = COALESCE(question_id, :question_id),
                    evidence_fingerprint = COALESCE(evidence_fingerprint, :evidence_fingerprint),
                    support_status = :support_status,
                    score = :score,
                    metadata = CAST(:metadata AS jsonb),
                    updated_at = now()
                WHERE id = :id
                  AND (evidence_fingerprint IS NULL OR evidence_fingerprint = :evidence_fingerprint)
                """
            ),
            {
                "id": str(record_id),
                "question_id": question_id,
                "evidence_fingerprint": fingerprint,
                "support_status": support_status,
                "score": score,
                "metadata": json_dumps(metadata),
            },
        )
    return record_id


def research_evidence_fingerprint(
    *,
    knowledge_base_id: str,
    document_id: str | None,
    document_version_id: str | None,
    chunk_id: str | None,
    source_url: str,
    title: str,
    content_abstract: str,
) -> str:
    """Return a stable, non-reversible identity for evidence deduplication."""
    stable_document = document_version_id or document_id or ""
    stable_chunk = chunk_id or ""
    if not stable_chunk:
        stable_chunk = stable_hash(
            ["fallback", " ".join(source_url.split()), " ".join(title.split()), " ".join(content_abstract.split())],
            40,
        )
    return stable_hash(["research_evidence_fingerprint_v1", knowledge_base_id, stable_document, stable_chunk], 40)


async def insert_research_claim_record(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    question_id: str | None,
    claim_text: str,
    support_status: str,
    evidence_ids: list[str],
    metadata: dict[str, Any],
    verification_input_hash: str | None = None,
) -> uuid.UUID:
    claim_id = stable_uuid([research_run_id, question_id or "", claim_text])
    result = await conn.execute(
        text(
            """
            INSERT INTO research_claim_records(
              id, tenant_id, research_run_id, question_id, claim_text, support_status, evidence_ids, metadata,
              verification_input_hash
            )
            VALUES (
              :id, :tenant_id, :run_id, :question_id, :claim_text, :support_status,
              CAST(:evidence_ids AS uuid[]), CAST(:metadata AS jsonb), :verification_input_hash
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": str(claim_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "question_id": question_id,
            "claim_text": claim_text,
            "support_status": support_status,
            "evidence_ids": evidence_ids,
            "metadata": json_dumps(metadata),
            "verification_input_hash": verification_input_hash,
        },
    )
    row = result.mappings().first()
    if row is None:
        existing = await conn.execute(
            text("SELECT verification_input_hash FROM research_claim_records WHERE id = :id"),
            {"id": str(claim_id)},
        )
        existing_row = existing.mappings().first()
        if existing_row is not None:
            stored_hash = str(existing_row.get("verification_input_hash") or "")
            if stored_hash and stored_hash != str(verification_input_hash or ""):
                raise ValueError("claim verification input does not match the existing claim")
            await conn.execute(
                text(
                    """
                    UPDATE research_claim_records
                    SET support_status = :support_status,
                        evidence_ids = CAST(:evidence_ids AS uuid[]),
                        metadata = CAST(:metadata AS jsonb),
                        verification_input_hash = COALESCE(verification_input_hash, :verification_input_hash),
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": str(claim_id),
                    "support_status": support_status,
                    "evidence_ids": evidence_ids,
                    "metadata": json_dumps(metadata),
                    "verification_input_hash": verification_input_hash,
                },
            )
    return claim_id


async def upsert_research_coverage_record(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    question_id: str,
    status: str,
    required_evidence_count: int,
    linked_evidence_ids: list[str],
    reason: str,
    metrics: dict[str, Any],
) -> uuid.UUID:
    coverage_id = stable_uuid([research_run_id, question_id, "coverage"])
    await conn.execute(
        text(
            """
            INSERT INTO research_coverage_records(
              id, tenant_id, research_run_id, question_id, status, required_evidence_count,
              linked_evidence_ids, reason, metrics
            )
            VALUES (
              :id, :tenant_id, :run_id, :question_id, :status, :required_count,
              CAST(:evidence_ids AS uuid[]), :reason, CAST(:metrics AS jsonb)
            )
            ON CONFLICT (research_run_id, question_id) DO UPDATE
            SET status = CASE
                  WHEN research_coverage_records.status = 'covered'
                    AND EXCLUDED.status IN ('missing','partial')
                    THEN research_coverage_records.status
                  ELSE EXCLUDED.status
                END,
                required_evidence_count = EXCLUDED.required_evidence_count,
                linked_evidence_ids = EXCLUDED.linked_evidence_ids,
                reason = EXCLUDED.reason,
                metrics = EXCLUDED.metrics,
                updated_at = now()
            """
        ),
        {
            "id": str(coverage_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "question_id": question_id,
            "status": status,
            "required_count": required_evidence_count,
            "evidence_ids": linked_evidence_ids,
            "reason": reason,
            "metrics": json_dumps(metrics),
        },
    )
    return coverage_id


async def insert_research_reflection(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    episode_id: str | None,
    body: str,
    metadata: dict[str, Any],
    reflection_type: str = "operational",
) -> uuid.UUID:
    reflection_id = stable_uuid(["research_reflection_v2", research_run_id, episode_id or "", reflection_type])
    await conn.execute(
        text(
            """
            INSERT INTO research_reflections(
              id, tenant_id, research_run_id, episode_id, reflection_type, body, metadata
            )
            VALUES (
              :id, :tenant_id, :run_id, :episode_id, :reflection_type, :body, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (id) DO UPDATE
            SET body = EXCLUDED.body,
                metadata = EXCLUDED.metadata
            """
        ),
        {
            "id": str(reflection_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "episode_id": episode_id,
            "reflection_type": reflection_type,
            "body": body,
            "metadata": json_dumps(metadata),
        },
    )
    return reflection_id


async def insert_research_decision(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    episode_id: str | None,
    question_id: str | None,
    decision_type: str,
    selected_strategy: str,
    reason: str,
    evidence_gain: int,
    metadata: dict[str, Any],
) -> uuid.UUID:
    decision_id = stable_uuid(
        ["research_decision_v2", research_run_id, episode_id or "", question_id or "", decision_type]
    )
    await conn.execute(
        text(
            """
            INSERT INTO research_decisions(
              id, tenant_id, research_run_id, episode_id, question_id, decision_type,
              selected_strategy, reason, evidence_gain, metadata
            )
            VALUES (
              :id, :tenant_id, :run_id, :episode_id, :question_id, :decision_type,
              :selected_strategy, :reason, :evidence_gain, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (id) DO UPDATE
            SET selected_strategy = EXCLUDED.selected_strategy,
                reason = EXCLUDED.reason,
                evidence_gain = EXCLUDED.evidence_gain,
                metadata = EXCLUDED.metadata
            """
        ),
        {
            "id": str(decision_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "episode_id": episode_id,
            "question_id": question_id,
            "decision_type": decision_type[:120],
            "selected_strategy": selected_strategy[:120],
            "reason": reason[:1000],
            "evidence_gain": max(0, evidence_gain),
            "metadata": json_dumps(_assert_public_safe_metadata(metadata)),
        },
    )
    return decision_id


async def insert_research_claim_relation(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    research_run_id: str,
    source_claim_id: str,
    target_claim_id: str,
    relation: str,
    metadata: dict[str, Any],
) -> uuid.UUID:
    relation_id = stable_uuid([research_run_id, source_claim_id, target_claim_id, relation])
    await conn.execute(
        text(
            """
            INSERT INTO research_claim_relations(
              id, tenant_id, research_run_id, source_claim_id, target_claim_id, relation, metadata
            )
            VALUES (
              :id, :tenant_id, :run_id, :source_claim_id, :target_claim_id, :relation, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (research_run_id, source_claim_id, target_claim_id, relation) DO NOTHING
            """
        ),
        {
            "id": str(relation_id),
            "tenant_id": tenant_id,
            "run_id": research_run_id,
            "source_claim_id": source_claim_id,
            "target_claim_id": target_claim_id,
            "relation": relation,
            "metadata": json_dumps(_assert_public_safe_metadata(metadata)),
        },
    )
    return relation_id


async def load_research_detail_records(
    conn: AsyncConnection, *, tenant_id: str, research_run_id: str
) -> dict[str, list[dict[str, Any]]]:
    queries = {
        "questions": """
            SELECT id, question, ordinal, kind, status, acceptance, metadata,
                   execution_state, outcome, attempt_count, rewrite_count, depth, budget
            FROM research_questions
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY ordinal
        """,
        "episodes": """
            SELECT id, query_run_id, episode_index, question_id, status, stage, context_summary,
                   metrics, error_code, created_at, completed_at
            FROM research_episodes
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY episode_index
        """,
        "tool_calls": """
            SELECT id, episode_id, question_id, query_run_id, tool_name, tool_query_hash, status,
                   result_summary, safe_metadata, error_code, started_at, completed_at
            FROM research_tool_calls
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY created_at, id
        """,
        # Internal controller input only.  Keep validated source handles out
        # of the public tool-call projection while allowing deterministic
        # routing to survive pause/resume.
        "tool_routing_history": """
            SELECT question_id, tool_name, validated_args, status, created_at
            FROM research_tool_calls
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY created_at, id
        """,
        "evidence": """
            SELECT e.id, e.question_id, e.chunk_id, e.document_id, e.document_version_id, e.knowledge_base_id,
                   e.evidence_ref, e.title, e.source_url, e.section_path, e.content_abstract, e.support_status,
                   e.score, e.evidence_fingerprint, e.metadata,
                   d.metadata AS current_document_metadata,
                   CASE WHEN d.id IS NOT NULL
                              AND d.lifecycle_state='active'
                              AND e.document_version_id=(d.metadata->>'current_version_id')
                              AND dv.status='published' AND dv.lifecycle_state='active'
                              AND c.publication_status='published'
                        THEN true ELSE false END AS current_retrievable
            FROM research_evidence_records AS e
            LEFT JOIN documents AS d ON d.id=e.document_id AND d.tenant_id=e.tenant_id
              AND d.knowledge_base_id=e.knowledge_base_id
            LEFT JOIN document_versions AS dv ON dv.id=e.document_version_id AND dv.document_id=d.id
              AND dv.tenant_id=d.tenant_id AND dv.knowledge_base_id=d.knowledge_base_id
            LEFT JOIN chunks AS c ON c.id=e.chunk_id AND c.document_id=d.id
              AND c.document_version_id=e.document_version_id AND c.tenant_id=d.tenant_id
              AND c.knowledge_base_id=d.knowledge_base_id
            WHERE e.tenant_id = :tenant_id AND e.research_run_id = :run_id
            ORDER BY e.created_at, e.evidence_ref
        """,
        "claims": """
            SELECT id, question_id, claim_text, support_status, evidence_ids, metadata
            FROM research_claim_records
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY created_at
        """,
        "relations": """
            SELECT id, source_claim_id, target_claim_id, relation, metadata, created_at
            FROM research_claim_relations
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY created_at
        """,
        "coverage": """
            SELECT id, question_id, status, required_evidence_count, linked_evidence_ids, reason, metrics
            FROM research_coverage_records
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY created_at
        """,
        "reflections": """
            SELECT id, episode_id, reflection_type, body, metadata, created_at
            FROM research_reflections
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY created_at
        """,
        "decisions": """
            SELECT id, episode_id, question_id, decision_type, selected_strategy, reason,
                   evidence_gain, metadata, created_at
            FROM research_decisions
            WHERE tenant_id = :tenant_id AND research_run_id = :run_id
            ORDER BY created_at
        """,
    }
    records: dict[str, list[dict[str, Any]]] = {}
    for key, sql in queries.items():
        result = await conn.execute(text(sql), {"tenant_id": tenant_id, "run_id": research_run_id})
        records[key] = [dict(row) for row in result.mappings()]
    for evidence in records["evidence"]:
        metadata = dict(evidence.get("metadata") or {})
        metadata["document_metadata"] = dict(evidence.pop("current_document_metadata") or {})
        evidence["metadata"] = metadata
        evidence["current_retrievable"] = bool(evidence.get("current_retrievable"))
    return records


async def list_knowledge_bases(conn: AsyncConnection, tenant_id: str) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, name, active_index, created_at
            FROM knowledge_bases
            WHERE tenant_id = :tenant_id
            ORDER BY created_at
            """
        ),
        {"tenant_id": tenant_id},
    )
    return [dict(row) for row in result.mappings()]


async def get_knowledge_base(conn: AsyncConnection, tenant_id: str, knowledge_base_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT id, name, active_index, created_at
            FROM knowledge_bases
            WHERE tenant_id = :tenant_id AND id = :kb_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


def _research_question_payload(value: Any) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    if isinstance(value, dict):
        return dict(value)
    return {"question": str(value)}


def _research_question_duplicate_key(value: str) -> str:
    return " ".join(value.casefold().replace("?", " ").replace(".", " ").split())


def _derived_question_priority(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("priority") or "").casefold()
    if explicit in {"required", "bridge", "normal"}:
        return explicit
    rationale = str(payload.get("rationale") or "").casefold()
    needed = " ".join(str(item) for item in payload.get("needed_evidence") or []).casefold()
    if "bridge" in rationale or "alias" in rationale:
        return "bridge"
    if any(token in f"{rationale} {needed}" for token in ("blocking", "approval", "owner")):
        return "bridge"
    return "normal"


def _assert_public_safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    serialized = json_dumps(value)
    leaked = [token for token in UNSAFE_PUBLIC_METADATA_TOKENS if token in serialized]
    if leaked:
        raise ValueError(f"unsafe public metadata tokens: {leaked}")
    return value


async def set_knowledge_base_active_index(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    active_index: str,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE knowledge_bases
            SET active_index = :active_index, updated_at = now()
            WHERE tenant_id = :tenant_id AND id = :kb_id
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "active_index": active_index},
    )
