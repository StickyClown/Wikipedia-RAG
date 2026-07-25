from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.db import json_dumps
from wikipediarag.ids import new_uuid
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
    await conn.execute(
        text(
            """
            INSERT INTO chunks(
              id, tenant_id, knowledge_base_id, document_id, page_id, revision_id, title,
              section_path, content, parent_chunk_id, prev_chunk_id, next_chunk_id,
              source_uri, source_url, embedding, content_hash, metadata
            )
            VALUES (
              :id, :tenant_id, :kb_id, :document_id, :page_id, :revision_id, :title,
              :section_path, :content, :parent_chunk_id, :prev_chunk_id, :next_chunk_id,
              :source_uri, :source_url, CAST(:embedding AS jsonb), :content_hash,
              CAST(:metadata AS jsonb)
            )
            ON CONFLICT (id) DO UPDATE
            SET content = EXCLUDED.content,
                prev_chunk_id = EXCLUDED.prev_chunk_id,
                next_chunk_id = EXCLUDED.next_chunk_id,
                embedding = EXCLUDED.embedding,
                content_hash = EXCLUDED.content_hash,
                metadata = EXCLUDED.metadata
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
            "metadata": json_dumps({}),
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
            SELECT id, title, section_path, content, source_url, embedding, page_id, document_id
            FROM chunks
            WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "kb_id": knowledge_base_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


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
) -> None:
    await conn.execute(
        text(
            """
            UPDATE query_runs
            SET status = 'completed',
                answer = :answer,
                usage = CAST(:usage AS jsonb),
                completed_at = now()
            WHERE id = :id
            """
        ),
        {"id": query_run_id, "answer": answer, "usage": json_dumps(usage)},
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
