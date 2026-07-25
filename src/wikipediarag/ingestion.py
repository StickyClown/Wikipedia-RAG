from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from wikipediarag.config import Settings, get_settings
from wikipediarag.db import connect
from wikipediarag.repository import get_job, update_job, upsert_wiki_page_and_chunks
from wikipediarag.schemas import JobStatus
from wikipediarag.search_index import bulk_index_chunks, ensure_index
from wikipediarag.storage import put_text
from wikipediarag.wiki_dump import (
    Chunk,
    chunks_for_page,
    iter_stream_groups,
    parse_pages_fragment,
    read_bzip2_stream,
    validate_index,
)


async def process_wiki_import(job: dict[str, Any], settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    job_id = str(job["id"])
    config = dict(job["config"])
    tenant_id = str(job["tenant_id"])
    kb_id = str(job["knowledge_base_id"])
    xml_path = Path(str(config.get("xml_path") or resolved.wiki_xml_path))
    index_path = Path(str(config.get("index_path") or resolved.wiki_index_path))
    snapshot_id = str(config.get("snapshot_id") or resolved.wiki_snapshot_id)
    limit = config.get("limit")
    page_limit = int(limit) if limit is not None else None
    progress = dict(job.get("progress") or {})
    checkpoint = dict(job.get("checkpoint") or {})
    pages_seen = int(progress.get("pages_seen") or 0)
    pages_imported = int(progress.get("pages_imported") or 0)
    chunks_indexed = int(progress.get("chunks_indexed") or 0)
    last_completed_offset = int(checkpoint.get("last_completed_offset") or -1)

    try:
        await _mark_running(job_id)
        if not checkpoint.get("index_validated"):
            stats = await asyncio.to_thread(validate_index, index_path, xml_path)
            checkpoint["index_validated"] = True
            checkpoint["index_stats"] = stats
            await _save_progress(job_id, pages_seen, pages_imported, chunks_indexed, checkpoint)
        await asyncio.to_thread(ensure_index, resolved)

        for group in iter_stream_groups(index_path, limit=page_limit):
            if group.offset <= last_completed_offset:
                continue
            if await _cancel_requested(job_id):
                await _set_cancelled(job_id, pages_seen, pages_imported, chunks_indexed, checkpoint)
                return
            fragment = await asyncio.to_thread(read_bzip2_stream, xml_path, group.offset)
            pages = await asyncio.to_thread(parse_pages_fragment, fragment)
            stream_chunks: list[Chunk] = []
            stream_artifacts: list[dict[str, Any]] = []
            async with connect() as conn:
                for page in pages:
                    pages_seen += 1
                    if page.namespace != 0:
                        continue
                    chunks = chunks_for_page(page, snapshot_id, resolved.embedding_dimensions)
                    await upsert_wiki_page_and_chunks(
                        conn,
                        tenant_id=tenant_id,
                        knowledge_base_id=kb_id,
                        snapshot_id=snapshot_id,
                        page=page,
                        chunks=chunks,
                    )
                    pages_imported += 1
                    chunks_indexed += len(chunks)
                    stream_chunks.extend(chunks)
                    stream_artifacts.append(
                        {
                            "page_id": page.page_id,
                            "revision_id": page.revision_id,
                            "title": page.title,
                            "timestamp": page.timestamp,
                            "redirect_target": page.redirect_target,
                            "chunk_ids": [chunk.id for chunk in chunks],
                        }
                    )
                    if page_limit is not None and pages_imported >= page_limit:
                        break
            await asyncio.to_thread(
                bulk_index_chunks,
                stream_chunks,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                settings=resolved,
            )
            if stream_artifacts:
                await asyncio.to_thread(
                    put_text,
                    f"wiki/{snapshot_id}/streams/{group.offset}.jsonl",
                    "\n".join(json.dumps(item, ensure_ascii=False) for item in stream_artifacts),
                    resolved,
                )
            last_completed_offset = group.offset
            checkpoint["last_completed_offset"] = last_completed_offset
            await _save_progress(job_id, pages_seen, pages_imported, chunks_indexed, checkpoint)
            if page_limit is not None and pages_imported >= page_limit:
                break

        async with connect() as conn:
            await update_job(
                conn,
                job_id,
                status=JobStatus.completed,
                progress={
                    "pages_seen": pages_seen,
                    "pages_imported": pages_imported,
                    "chunks_indexed": chunks_indexed,
                },
                checkpoint=checkpoint,
            )
    except Exception as exc:
        async with connect() as conn:
            await update_job(
                conn,
                job_id,
                status=JobStatus.failed,
                progress={
                    "pages_seen": pages_seen,
                    "pages_imported": pages_imported,
                    "chunks_indexed": chunks_indexed,
                },
                checkpoint=checkpoint,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
        raise


async def _mark_running(job_id: str) -> None:
    async with connect() as conn:
        await update_job(conn, job_id, status=JobStatus.running)


async def _save_progress(
    job_id: str,
    pages_seen: int,
    pages_imported: int,
    chunks_indexed: int,
    checkpoint: dict[str, Any],
) -> None:
    async with connect() as conn:
        await update_job(
            conn,
            job_id,
            progress={
                "pages_seen": pages_seen,
                "pages_imported": pages_imported,
                "chunks_indexed": chunks_indexed,
            },
            checkpoint=checkpoint,
        )


async def _cancel_requested(job_id: str) -> bool:
    async with connect() as conn:
        job = await get_job(conn, job_id)
        return bool(job and job["cancel_requested"])


async def _set_cancelled(
    job_id: str,
    pages_seen: int,
    pages_imported: int,
    chunks_indexed: int,
    checkpoint: dict[str, Any],
) -> None:
    async with connect() as conn:
        await update_job(
            conn,
            job_id,
            status=JobStatus.cancelled,
            progress={
                "pages_seen": pages_seen,
                "pages_imported": pages_imported,
                "chunks_indexed": chunks_indexed,
            },
            checkpoint=checkpoint,
        )


async def process_job(job: dict[str, Any], settings: Settings | None = None) -> None:
    if job["kind"] == "wikipedia_xml":
        await process_wiki_import(job, settings)
    else:
        raise ValueError(f"unsupported ingestion job kind {job['kind']}")


async def claim_and_process_once(settings: Settings | None = None) -> bool:
    from wikipediarag.repository import claim_next_job

    async with connect() as conn:
        job = await claim_next_job(conn)
    if job is None:
        return False
    await process_job(job, settings)
    return True
