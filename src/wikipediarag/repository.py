from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

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
              id, tenant_id, knowledge_base_id, status, filename, content_type, size_bytes,
              checksum_sha256, object_key, parser_profile, metadata, expires_at
            )
            VALUES (
              :id, :tenant_id, :kb_id, 'created', :filename, :content_type, :size_bytes,
              :checksum_sha256, :object_key, :parser_profile, CAST(:metadata AS jsonb), :expires_at
            )
            """
        ),
        {
            "id": str(session_id),
            "tenant_id": tenant_id,
            "kb_id": knowledge_base_id,
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
    batch_id = new_uuid()
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
            WHERE d.tenant_id = :tenant_id AND d.id = :document_id
            """
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


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
            SELECT id, title, section_path, content, source_url, embedding, page_id, document_id, metadata
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
    user_id: str,
    request_id: str,
    client_request_id: str | None,
    mode: str,
    input_text: str,
    trace_id: str,
) -> uuid.UUID:
    query_run_id = new_uuid()
    await conn.execute(
        text(
            """
            INSERT INTO query_runs(
              id, tenant_id, user_id, request_id, client_request_id, mode, status,
              input_text, trace_id, started_at
            )
            VALUES (:id, :tenant_id, :user_id, :request_id, :client_request_id, :mode,
                    'running', :input_text, :trace_id, now())
            """
        ),
        {
            "id": str(query_run_id),
            "tenant_id": tenant_id,
            "user_id": user_id,
            "request_id": request_id,
            "client_request_id": client_request_id,
            "mode": mode,
            "input_text": input_text,
            "trace_id": trace_id,
        },
    )
    return query_run_id


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
                usage = CAST(:usage AS jsonb),
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
