from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.auth import ActorContext, KnowledgeBaseRole, PlatformRole, TenantRole, effective_knowledge_base_role
from wikipediarag.db import json_dumps
from wikipediarag.ids import new_uuid, stable_uuid
from wikipediarag.schemas import JobStatus
from wikipediarag.wiki_dump import Chunk, WikiPage


async def create_ingestion_job(
    conn: AsyncConnection,
    tenant_id: str,
    knowledge_base_id: str,
    kind: str,
    config: dict[str, Any],
) -> uuid.UUID:
    job_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO ingestion_jobs(id, tenant_id, knowledge_base_id, kind, status, config, progress)
            VALUES (:id, :tenant_id, :knowledge_base_id, :kind, 'received',
                    CAST(:config AS jsonb), CAST(:progress AS jsonb))
            """
        ),
        {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "kind": kind,
            "config": json_dumps(config),
            "progress": json_dumps({"pages_seen": 0, "pages_imported": 0, "chunks_indexed": 0}),
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


async def get_job(conn: AsyncConnection, job_id: str) -> dict[str, Any] | None:
    result = await conn.execute(text("SELECT * FROM ingestion_jobs WHERE id = :id"), {"id": job_id})
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def claim_next_job(conn: AsyncConnection) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT * FROM ingestion_jobs
            WHERE status IN ('received','running') AND cancel_requested = false
              AND (
                kind <> 'document_delete'
                OR config ->> 'purge_after' IS NULL
                OR (config ->> 'purge_after')::timestamptz <= now()
              )
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
    )
    row = result.mappings().first()
    if row is None:
        return None
    await conn.execute(
        text(
            """
            UPDATE ingestion_jobs
            SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": row["id"]},
    )
    return dict(row)


async def update_job(
    conn: AsyncConnection,
    job_id: str,
    *,
    status: JobStatus | None = None,
    progress: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"id": job_id}
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status.value
        if status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
            assignments.append("completed_at = now()")
        if status == JobStatus.completed:
            assignments.append("error_code = NULL")
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
    await conn.execute(
        text(f"UPDATE ingestion_jobs SET {', '.join(assignments)} WHERE id = :id"),  # noqa: S608
        params,
    )


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
            WHERE id = :id
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
                   j.status AS job_status
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
) -> uuid.UUID:
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
    return job_id


async def claim_next_ingestion_job_item(conn: AsyncConnection, job_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM ingestion_job_items
            WHERE job_id = :job_id
              AND (
                status = 'received'
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
                claimed_at = COALESCE(claimed_at, now()),
                updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": row["id"]},
    )
    return dict(row)


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
) -> None:
    assignments = ["updated_at = now()"]
    params: dict[str, Any] = {"id": item_id}
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status.value
        if status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
            assignments.append("completed_at = now()")
        if status == JobStatus.completed:
            assignments.append("error_code = NULL")
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
    document_id = f"wiki:{snapshot_id}:{page.page_id}"
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
            INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata)
            VALUES (:id, :tenant_id, :kb_id, 'wikipedia_xml', :title, :source_uri,
                    CAST(:metadata AS jsonb))
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
            "title": page.title,
            "source_uri": f"wikipedia://{snapshot_id}/{page.page_id}",
            "metadata": json_dumps(metadata),
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
              document_version_id, chunk_ordinal, locator, publication_status
            )
            VALUES (
              :id, :tenant_id, :kb_id, :document_id, :page_id, :revision_id, :title,
              :section_path, :content, :parent_chunk_id, :prev_chunk_id, :next_chunk_id,
              :source_uri, :source_url, CAST(:embedding AS jsonb), :content_hash,
              CAST(:metadata AS jsonb), :document_version_id, :chunk_ordinal,
              CAST(:locator AS jsonb), :publication_status
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
                publication_status = EXCLUDED.publication_status
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
            "document_version_id": metadata.get("document_version_id"),
            "chunk_ordinal": int(chunk_ordinal) if isinstance(chunk_ordinal, int | float | str) else None,
            "locator": json_dumps(locator),
            "publication_status": str(metadata.get("publication_status") or "published"),
        },
    )


async def fetch_chunks_for_dense_scan(
    conn: AsyncConnection,
    tenant_id: str,
    knowledge_base_id: str,
    limit: int = 2500,
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, knowledge_base_id, title, section_path, content, source_url,
                   embedding, page_id, document_id, metadata
            FROM chunks
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND publication_status = 'published'
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


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
                   parent_chunk_id, prev_chunk_id, next_chunk_id, metadata
            FROM chunks
            WHERE tenant_id = :tenant_id
              AND knowledge_base_id = :kb_id
              AND id = :chunk_id
              AND publication_status = 'published'
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "chunk_id": chunk_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


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
            INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata)
            VALUES (:id, :tenant_id, :kb_id, :source_type, :title, :source_uri, CAST(:metadata AS jsonb))
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                source_uri = EXCLUDED.source_uri,
                metadata = EXCLUDED.metadata,
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
              embedding_alias, embedding_dimensions, physical_index, read_alias, write_alias, metadata
            )
            VALUES (
              :id, :tenant_id, :kb_id, :source_type, :snapshot_id, :retrieval_profile,
              :embedding_alias, :embedding_dimensions, :physical_index, :read_alias, :write_alias,
              CAST(:metadata AS jsonb)
            )
            ON CONFLICT (id) DO UPDATE
            SET status = 'active',
                metadata = EXCLUDED.metadata,
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
            "payload": json_dumps(payload),
        },
    )


async def load_retrieval_events(conn: AsyncConnection, tenant_id: str, query_run_id: str) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT event_type, stage, payload, created_at
            FROM retrieval_events
            WHERE tenant_id = :tenant_id AND query_run_id = :query_run_id
            ORDER BY created_at
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
) -> uuid.UUID:
    query_run_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO query_runs(
              id, tenant_id, knowledge_base_id, user_id, request_id, client_request_id, mode, status,
              input_text, usage, trace_id, started_at
            )
            VALUES (:id, :tenant_id, :knowledge_base_id, :user_id, :request_id, :client_request_id, :mode,
                    'running', :input_text, CAST(:usage AS jsonb), :trace_id, now())
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
        },
    )
    return query_run_id


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
            WHERE id = :id
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
            WHERE id = :id
            """
        ),
        {"id": query_run_id, "error_code": error_code},
    )


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
