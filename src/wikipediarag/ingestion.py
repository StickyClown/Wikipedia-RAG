from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import text

from wikipediarag.config import Settings, get_settings
from wikipediarag.db import connect
from wikipediarag.document_access import normalize_document_access
from wikipediarag.document_ingestion import (
    ParserServiceError,
    UploadValidationError,
    chunks_for_normalized_document,
    normalize_uploaded_document,
    normalized_document_hash,
    safe_public_metadata,
    sha256_hex,
    validate_upload_bytes,
)
from wikipediarag.ids import scoped_id, stable_hash
from wikipediarag.model_client import embeddings
from wikipediarag.model_registry import get_model_registry
from wikipediarag.reliability import is_retryable_exception, safe_failure_from_exception
from wikipediarag.repository import (
    claim_next_ingestion_job_item,
    create_document_deletion_job,
    create_source_document_ingestion_item,
    finish_knowledge_source_sync,
    get_document_public,
    get_job,
    get_knowledge_base,
    get_knowledge_source,
    get_source_document_state,
    insert_document_artifact,
    list_document_artifact_keys,
    list_source_document_states,
    load_document_version,
    load_index_version_by_read_alias,
    mark_document_purge_failed,
    mark_document_purged,
    mark_document_version_chunks_deleted,
    mark_source_document_tombstone,
    next_ingestion_job_item_retry_delay_seconds,
    replace_document_sections_from_chunks,
    save_index_version,
    set_knowledge_base_active_index,
    soft_delete_document,
    summarize_ingestion_job_items,
    update_document_access_metadata,
    update_document_version,
    update_ingestion_job_item,
    update_job,
    update_source_sync_run,
    upsert_chunk,
    upsert_document,
    upsert_source_document_state,
    upsert_wiki_page_and_chunks,
)
from wikipediarag.retrieval_contract import build_index_contract, index_contract_metadata
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import JobStatus
from wikipediarag.search_index import (
    READ_ALIAS,
    build_index_names,
    bulk_index_chunks,
    delete_document_chunks,
    delete_document_version_chunks,
    ensure_index,
    update_document_access,
)
from wikipediarag.source_connectors import ConnectorError, SourceDocument, connector_for_kind
from wikipediarag.storage import delete_objects, get_bytes, put_bytes, put_text
from wikipediarag.wiki_dump import (
    Chunk,
    chunks_for_page,
    iter_stream_groups,
    parse_pages_fragment,
    read_bzip2_stream,
    validate_index,
)
from wikipediarag.zim_dump import ZimArchiveAdapter, ZimPage, ZimRedirect, chunks_for_zim_page, resolve_zim_path

EMBEDDING_INPUT_BATCH_SIZE = 96
EMBEDDING_REQUEST_CONCURRENCY = 8
ZIM_IMPORT_PAGE_BATCH_SIZE = 384


def _safe_ingestion_error_code(exc: BaseException, *, stage: str) -> str:
    """Return a canonical, content-free code for persisted worker failures."""
    return safe_failure_from_exception(exc, stage=stage).error_code


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
        profile = get_retrieval_profile(str(config.get("retrieval_profile") or resolved.retrieval_profile), resolved)
        embed_alias = profile.model_aliases.embed
        dimensions = profile.embedding_dimensions(resolved.embedding_dimensions)
        index_names = build_index_names(
            source_type="wikipedia_xml",
            snapshot_id=snapshot_id,
            retrieval_profile=profile.name,
            embedding_alias=embed_alias,
            embedding_dimensions=dimensions,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
        )
        index_version_id = index_names["version_id"]
        index_contract = build_index_contract(
            index_version=index_version_id,
            source_type="wikipedia_xml",
            snapshot_id=snapshot_id,
            physical_index=index_names["physical"],
            read_alias=index_names["read_alias"],
            embedding_alias=embed_alias,
            embedding_dimensions=dimensions,
            profile=profile,
            settings=resolved,
        )
        await _mark_running(job_id)
        if not checkpoint.get("index_validated"):
            stats = await asyncio.to_thread(validate_index, index_path, xml_path)
            checkpoint["index_validated"] = True
            checkpoint["index_stats"] = stats
            await _save_progress(job_id, pages_seen, pages_imported, chunks_indexed, checkpoint)
        await asyncio.to_thread(
            ensure_index,
            resolved,
            physical_index=index_names["physical"],
            read_alias=index_names["read_alias"],
            write_alias=index_names["write_alias"],
            dimensions=dimensions,
        )
        async with connect() as conn:
            await save_index_version(
                conn,
                index_version_id=index_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_type="wikipedia_xml",
                snapshot_id=snapshot_id,
                retrieval_profile=profile.name,
                embedding_alias=embed_alias,
                embedding_dimensions=dimensions,
                physical_index=index_names["physical"],
                read_alias=index_names["read_alias"],
                write_alias=index_names["write_alias"],
                metadata=index_contract_metadata(index_contract),
            )

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
                    chunks = chunks_for_page(
                        page,
                        snapshot_id,
                        resolved.embedding_dimensions,
                        tenant_id=tenant_id,
                        knowledge_base_id=kb_id,
                    )
                    await upsert_wiki_page_and_chunks(
                        conn,
                        tenant_id=tenant_id,
                        knowledge_base_id=kb_id,
                        snapshot_id=snapshot_id,
                        page=page,
                        chunks=chunks,
                    )
                    document_id = chunks[0].document_id if chunks else f"wiki:{snapshot_id}:{page.page_id}"
                    await replace_document_sections_from_chunks(
                        conn,
                        tenant_id=tenant_id,
                        knowledge_base_id=kb_id,
                        document_id=document_id,
                        document_version_id=None,
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
                write_alias=index_names["write_alias"],
                physical_index=index_names["physical"],
                read_alias=index_names["read_alias"],
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
            await set_knowledge_base_active_index(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                active_index=index_names["read_alias"],
            )
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
                error_code=_safe_ingestion_error_code(exc, stage="wiki_import"),
                error_message=str(exc),
            )
        raise


async def process_zim_import(job: dict[str, Any], settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    job_id = str(job["id"])
    config = dict(job["config"])
    tenant_id = str(job["tenant_id"])
    kb_id = str(job["knowledge_base_id"])
    progress = dict(job.get("progress") or {})
    checkpoint = dict(job.get("checkpoint") or {})
    pages_imported = int(progress.get("pages_imported") or 0)
    chunks_indexed = int(progress.get("chunks_indexed") or 0)
    entries_scanned = int(progress.get("entries_scanned") or 0)
    redirects_seen = int(progress.get("redirects_seen") or 0)
    skipped_entries = int(progress.get("skipped_entries") or 0)
    last_completed_entry_index = int(checkpoint.get("last_completed_entry_index") or -1)
    batch_pages: list[ZimPage] = []
    batch_redirects: list[ZimRedirect] = []

    try:
        profile = get_retrieval_profile(str(config.get("retrieval_profile") or resolved.retrieval_profile), resolved)
        model_registry = get_model_registry(resolved)
        embed_alias = profile.model_aliases.embed
        embed_model = model_registry.require(embed_alias, "embedding")
        dimensions = int(embed_model.dimensions or profile.embedding_dimensions(resolved.embedding_dimensions))
        zim_path = resolve_zim_path(
            Path(str(config.get("zim_dir") or resolved.zim_dir)),
            str(config.get("zim_filename") or resolved.zim_filename),
            str(config["zim_path"]) if config.get("zim_path") else None,
        )
        limit = config.get("limit")
        page_limit = int(limit) if limit is not None else 10000
        adapter = ZimArchiveAdapter(
            zim_path,
            public_base_url=str(config.get("kiwix_public_base_url") or resolved.kiwix_public_base_url),
            book_name_override=str(config.get("kiwix_book_name") or resolved.kiwix_book_name),
        )
        archive_info = await asyncio.to_thread(adapter.info)
        snapshot_id = str(config.get("snapshot_id") or archive_info.archive_id)
        index_names = build_index_names(
            source_type="zim",
            snapshot_id=snapshot_id,
            retrieval_profile=profile.name,
            embedding_alias=embed_alias,
            embedding_dimensions=dimensions,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
        )
        index_contract = build_index_contract(
            index_version=index_names["version_id"],
            source_type="zim",
            snapshot_id=snapshot_id,
            physical_index=index_names["physical"],
            read_alias=index_names["read_alias"],
            embedding_alias=embed_alias,
            embedding_dimensions=dimensions,
            profile=profile,
            settings=resolved,
        )
        checkpoint.update(
            {
                "zim_archive_id": archive_info.archive_id,
                "zim_filename": archive_info.filename,
                "zim_book_name": archive_info.book_name,
                "snapshot_id": snapshot_id,
                "retrieval_profile": profile.name,
                "embedding_alias": embed_alias,
                "embedding_dimensions": dimensions,
                "index_version_id": index_names["version_id"],
            }
        )
        await _mark_running(job_id)
        await asyncio.to_thread(
            ensure_index,
            resolved,
            physical_index=index_names["physical"],
            read_alias=index_names["read_alias"],
            write_alias=index_names["write_alias"],
            dimensions=dimensions,
        )
        async with connect() as conn:
            await save_index_version(
                conn,
                index_version_id=index_names["version_id"],
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_type="zim",
                snapshot_id=snapshot_id,
                retrieval_profile=profile.name,
                embedding_alias=embed_alias,
                embedding_dimensions=dimensions,
                physical_index=index_names["physical"],
                read_alias=index_names["read_alias"],
                write_alias=index_names["write_alias"],
                metadata={"archive": archive_info.__dict__, **index_contract_metadata(index_contract)},
            )
        await _save_zim_progress(
            job_id,
            pages_imported,
            chunks_indexed,
            entries_scanned,
            redirects_seen,
            skipped_entries,
            checkpoint,
        )

        iterator = adapter.iter_items(start_after_index=last_completed_entry_index)
        while True:
            item = await asyncio.to_thread(_next_zim_item, iterator)
            if item is None:
                break
            entries_scanned += 1
            if item.skipped:
                skipped_entries += 1
                last_completed_entry_index = item.entry_index
                continue
            if item.redirect is not None:
                redirects_seen += 1
                batch_redirects.append(item.redirect)
                last_completed_entry_index = item.entry_index
            elif item.page is not None:
                batch_pages.append(item.page)
                last_completed_entry_index = item.entry_index
            if await _cancel_requested(job_id):
                imported, indexed = await _flush_zim_batch(
                    batch_pages,
                    batch_redirects,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    snapshot_id=snapshot_id,
                    profile=profile,
                    dimensions=dimensions,
                    settings=resolved,
                    write_alias=index_names["write_alias"],
                    physical_index=index_names["physical"],
                    read_alias=index_names["read_alias"],
                )
                pages_imported += imported
                chunks_indexed += indexed
                checkpoint["last_completed_entry_index"] = last_completed_entry_index
                await _set_zim_cancelled(
                    job_id,
                    pages_imported,
                    chunks_indexed,
                    entries_scanned,
                    redirects_seen,
                    skipped_entries,
                    checkpoint,
                )
                return
            if len(batch_pages) >= ZIM_IMPORT_PAGE_BATCH_SIZE or (
                batch_pages and pages_imported + len(batch_pages) >= page_limit
            ):
                imported, indexed = await _flush_zim_batch(
                    batch_pages[: max(0, page_limit - pages_imported)],
                    batch_redirects,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    snapshot_id=snapshot_id,
                    profile=profile,
                    dimensions=dimensions,
                    settings=resolved,
                    write_alias=index_names["write_alias"],
                    physical_index=index_names["physical"],
                    read_alias=index_names["read_alias"],
                )
                pages_imported += imported
                chunks_indexed += indexed
                batch_pages = []
                batch_redirects = []
                checkpoint["last_completed_entry_index"] = last_completed_entry_index
                checkpoint["last_completed_entry_path"] = item.page.zim_entry_path if item.page is not None else ""
                await _save_zim_progress(
                    job_id,
                    pages_imported,
                    chunks_indexed,
                    entries_scanned,
                    redirects_seen,
                    skipped_entries,
                    checkpoint,
                )
            if pages_imported >= page_limit:
                break

        if pages_imported < page_limit and batch_pages:
            imported, indexed = await _flush_zim_batch(
                batch_pages[: max(0, page_limit - pages_imported)],
                batch_redirects,
                tenant_id=tenant_id,
                kb_id=kb_id,
                snapshot_id=snapshot_id,
                profile=profile,
                dimensions=dimensions,
                settings=resolved,
                write_alias=index_names["write_alias"],
                physical_index=index_names["physical"],
                read_alias=index_names["read_alias"],
            )
            pages_imported += imported
            chunks_indexed += indexed
            checkpoint["last_completed_entry_index"] = last_completed_entry_index
            await _save_zim_progress(
                job_id,
                pages_imported,
                chunks_indexed,
                entries_scanned,
                redirects_seen,
                skipped_entries,
                checkpoint,
            )

        if pages_imported != page_limit:
            raise ValueError(f"ZIM import ended with {pages_imported} articles, expected {page_limit}")
        async with connect() as conn:
            await set_knowledge_base_active_index(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                active_index=index_names["read_alias"],
            )
            await update_job(
                conn,
                job_id,
                status=JobStatus.completed,
                progress={
                    "pages_imported": pages_imported,
                    "chunks_indexed": chunks_indexed,
                    "entries_scanned": entries_scanned,
                    "redirects_seen": redirects_seen,
                    "skipped_entries": skipped_entries,
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
                    "pages_imported": pages_imported,
                    "chunks_indexed": chunks_indexed,
                    "entries_scanned": entries_scanned,
                    "redirects_seen": redirects_seen,
                    "skipped_entries": skipped_entries,
                },
                checkpoint=checkpoint,
                error_code=_safe_ingestion_error_code(exc, stage="zim_import"),
                error_message=str(exc),
            )
        raise


async def _flush_zim_batch(
    pages: list[ZimPage],
    redirects: list[ZimRedirect],
    *,
    tenant_id: str,
    kb_id: str,
    snapshot_id: str,
    profile: RetrievalProfile,
    dimensions: int,
    settings: Settings,
    write_alias: str,
    physical_index: str,
    read_alias: str,
) -> tuple[int, int]:
    page_chunk_sets = [
        (
            page,
            chunks_for_zim_page(
                page,
                snapshot_id=snapshot_id,
                dimensions=dimensions,
                child_tokens_min=profile.chunking.child_tokens_min,
                child_tokens_max=profile.chunking.child_tokens_max,
                parent_tokens_min=profile.chunking.parent_tokens_min,
                parent_tokens_max=profile.chunking.parent_tokens_max,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
            ),
        )
        for page in pages
    ]
    unembedded_chunks = [chunk for _page, chunks in page_chunk_sets for chunk in chunks]
    embedded_chunks = await _embed_chunks(unembedded_chunks, profile, dimensions, settings)
    embedded_by_page: list[tuple[ZimPage, list[Chunk]]] = []
    offset = 0
    for page, page_chunks in page_chunk_sets:
        next_offset = offset + len(page_chunks)
        embedded_by_page.append((page, embedded_chunks[offset:next_offset]))
        offset = next_offset

    all_chunks: list[Chunk] = []
    aliases_by_target: dict[str, list[str]] = {}
    for redirect in redirects:
        aliases_by_target.setdefault(redirect.redirect_target, []).append(redirect.title)
    async with connect() as conn:
        for redirect in redirects:
            native_redirect_id = f"zim-redirect:{snapshot_id}:{stable_zim_id(redirect.zim_entry_path)}"
            document_id = scoped_id(
                "zim-redirect-document",
                native_redirect_id,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_type="wikipedia_zim_redirect",
                snapshot_id=snapshot_id,
            )
            await upsert_document(
                conn,
                document_id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_type="wikipedia_zim_redirect",
                title=redirect.title,
                source_uri=f"zim://{snapshot_id}/{redirect.zim_entry_path}",
                metadata={
                    "zim_entry_path": redirect.zim_entry_path,
                    "redirect_target": redirect.redirect_target,
                    "snapshot_id": snapshot_id,
                    "source_document_id": native_redirect_id,
                    "entry_index": redirect.entry_index,
                },
            )
        for page, embedded_page_chunks in embedded_by_page:
            document_id = (
                embedded_page_chunks[0].document_id
                if embedded_page_chunks
                else scoped_id(
                    "zim-document",
                    f"zim:{snapshot_id}:{stable_zim_id(page.zim_entry_path)}",
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    source_type="zim",
                    snapshot_id=snapshot_id,
                )
            )
            await upsert_document(
                conn,
                document_id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_type="wikipedia_zim",
                title=page.title,
                source_uri=f"zim://{snapshot_id}/{page.zim_entry_path}",
                metadata={
                    **page.metadata,
                    "aliases": sorted(set(aliases_by_target.get(page.title, []))),
                    "redirect_target": page.redirect_target,
                    "source_url": page.source_url,
                },
            )
            for chunk in embedded_page_chunks:
                chunk.metadata["aliases"] = sorted(set(aliases_by_target.get(page.title, [])))
                await upsert_chunk(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, chunk=chunk)
            await replace_document_sections_from_chunks(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                document_version_id=None,
                chunks=embedded_page_chunks,
            )
            all_chunks.extend(embedded_page_chunks)
    await asyncio.to_thread(
        bulk_index_chunks,
        all_chunks,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
        settings=settings,
        write_alias=write_alias,
        physical_index=physical_index,
        read_alias=read_alias,
        dimensions=dimensions,
    )
    if pages:
        await asyncio.to_thread(
            put_text,
            f"zim/{snapshot_id}/entries/{pages[-1].entry_index}.jsonl",
            "\n".join(
                json.dumps(
                    {
                        "title": page.title,
                        "zim_entry_path": page.zim_entry_path,
                        "source_url": page.source_url,
                    },
                    ensure_ascii=False,
                )
                for page in pages
            ),
            settings,
        )
    return len(pages), len(all_chunks)


async def _embed_chunks(
    chunks: list[Chunk],
    profile: RetrievalProfile,
    dimensions: int,
    settings: Settings,
) -> list[Chunk]:
    if not chunks:
        return []
    batches = [
        chunks[start : start + EMBEDDING_INPUT_BATCH_SIZE]
        for start in range(0, len(chunks), EMBEDDING_INPUT_BATCH_SIZE)
    ]
    semaphore = asyncio.Semaphore(EMBEDDING_REQUEST_CONCURRENCY)

    async def embed_batch(batch: list[Chunk]) -> list[list[float]]:
        documents = [f"{chunk.title}\n{' / '.join(chunk.section_path)}\n{chunk.content}" for chunk in batch]
        async with semaphore:
            batch_vectors, _usage = await embeddings(
                documents,
                settings,
                alias=profile.model_aliases.embed,
                dimensions=dimensions,
            )
        return batch_vectors

    vectors = [
        vector
        for batch_vectors in await asyncio.gather(*(embed_batch(batch) for batch in batches))
        for vector in batch_vectors
    ]
    if any(len(vector) != dimensions for vector in vectors):
        raise ValueError(f"embedding endpoint returned vectors with dimensions other than {dimensions}")
    return [
        replace(chunk, embedding=[float(value) for value in vector])
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


def stable_zim_id(path: str) -> str:
    from wikipediarag.ids import stable_hash

    return stable_hash([path], 24)


def _next_zim_item(iterator: Iterator[Any]) -> Any | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


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


async def _save_zim_progress(
    job_id: str,
    pages_imported: int,
    chunks_indexed: int,
    entries_scanned: int,
    redirects_seen: int,
    skipped_entries: int,
    checkpoint: dict[str, Any],
) -> None:
    async with connect() as conn:
        await update_job(
            conn,
            job_id,
            progress={
                "pages_imported": pages_imported,
                "chunks_indexed": chunks_indexed,
                "entries_scanned": entries_scanned,
                "redirects_seen": redirects_seen,
                "skipped_entries": skipped_entries,
            },
            checkpoint=checkpoint,
        )


async def _set_zim_cancelled(
    job_id: str,
    pages_imported: int,
    chunks_indexed: int,
    entries_scanned: int,
    redirects_seen: int,
    skipped_entries: int,
    checkpoint: dict[str, Any],
) -> None:
    async with connect() as conn:
        await update_job(
            conn,
            job_id,
            status=JobStatus.cancelled,
            progress={
                "pages_imported": pages_imported,
                "chunks_indexed": chunks_indexed,
                "entries_scanned": entries_scanned,
                "redirects_seen": redirects_seen,
                "skipped_entries": skipped_entries,
            },
            checkpoint=checkpoint,
        )


async def process_document_upload(job: dict[str, Any], settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    job_id = str(job["id"])
    await _mark_running(job_id)
    while True:
        if await _cancel_requested(job_id):
            await _cancel_remaining_items(job_id)
            await _finalize_document_upload_job(job_id)
            return
        items: list[dict[str, Any]] = []
        async with connect() as conn:
            for _ in range(max(1, resolved.document_ingestion_item_concurrency)):
                item = await claim_next_ingestion_job_item(conn, job_id)
                if item is None:
                    break
                items.append(item)
        if not items:
            async with connect() as conn:
                retry_delay = await next_ingestion_job_item_retry_delay_seconds(conn, job_id)
            if retry_delay is not None:
                await asyncio.sleep(min(3.0, max(0.1, retry_delay)))
                continue
            await _finalize_document_upload_job(job_id)
            return
        await asyncio.gather(*(_process_document_upload_item(item, resolved) for item in items))
        await _save_document_upload_job_progress(job_id, "processing")


async def _process_document_upload_item(item: dict[str, Any], settings: Settings) -> None:
    item_id = str(item["id"])
    job_id = str(item["job_id"])
    document_id = str(item["document_id"])
    document_version_id = str(item["document_version_id"])
    tenant_id = str(item["tenant_id"])
    kb_id = str(item["knowledge_base_id"])
    stage = "received"
    try:
        version = await _load_required_document_version(tenant_id, document_version_id)
        original_key = str(version["original_artifact_key"])
        source_metadata = dict(version.get("source_metadata") or {})
        public_metadata = dict(version.get("public_metadata") or {})
        filename = str(public_metadata.get("filename") or source_metadata.get("filename") or document_id)
        content_type = str(public_metadata.get("content_type") or "application/octet-stream")
        expected_size = int(public_metadata.get("size_bytes") or 0)
        expected_sha256 = str(public_metadata.get("checksum_sha256") or version["content_hash"])
        parser_options = dict(version.get("parser_options") or {})
        parser_profile = str(parser_options.get("profile") or "standard")

        stage = "read_original"
        await _update_item_stage(item_id, stage, {"stage": stage})
        data = await asyncio.to_thread(get_bytes, original_key, settings)
        stage = "validating"
        validation = validate_upload_bytes(
            data,
            filename=filename,
            supplied_content_type=content_type,
            expected_size_bytes=expected_size,
            expected_sha256=expected_sha256,
            settings=settings,
        )
        await _update_item_stage(
            item_id,
            stage,
            {
                "stage": stage,
                "bytes_received": validation.size_bytes,
                "detected_mime": validation.detected_mime,
            },
        )
        async with connect() as conn:
            await update_document_version(
                conn,
                document_version_id,
                status="validating",
                validation=validation.model_dump(mode="json"),
            )

        stage = "parsing"
        await _update_item_stage(item_id, stage, {"stage": stage})
        async with connect() as conn:
            await update_document_version(conn, document_version_id, status="parsing")
        normalized = await normalize_uploaded_document(
            data,
            validation=validation,
            parser_profile=parser_profile,
            settings=settings,
        )
        parser_runtime_progress = _parser_runtime_progress(normalized)
        await _update_item_stage(
            item_id,
            stage,
            {"stage": stage, "parser_route": normalized.parser_route, **parser_runtime_progress},
        )
        normalized_hash = normalized_document_hash(normalized)
        normalized_key = f"documents/{tenant_id}/{document_id}/{document_version_id}/normalized.json"
        normalized_payload = normalized.model_dump_json(indent=2)
        parser_report_key = f"documents/{tenant_id}/{document_id}/{document_version_id}/parser-report.json"
        parser_report_payload = json.dumps(
            {
                "schema_version": "parser_report_v1",
                "parser_route": normalized.parser_route,
                "parser_name": normalized.parser_name,
                "parser_version": normalized.parser_version,
                "parser_options": normalized.parser_options,
                "warnings": normalized.warnings,
                "metadata": normalized.metadata.model_dump(mode="json"),
                "source_metadata": normalized.source_metadata,
            },
            ensure_ascii=False,
            indent=2,
        )
        await asyncio.to_thread(put_text, normalized_key, normalized_payload, settings)
        await asyncio.to_thread(put_text, parser_report_key, parser_report_payload, settings)
        public = safe_public_metadata(
            validation=validation,
            metadata=normalized.metadata,
            normalized_hash=normalized_hash,
            parser_route=normalized.parser_route,
            parser_name=normalized.parser_name,
            parser_version=normalized.parser_version,
            warnings=normalized.warnings,
        )
        async with connect() as conn:
            await update_document_version(
                conn,
                document_version_id,
                status="normalized",
                normalized_hash=normalized_hash,
                normalized_artifact_key=normalized_key,
                parser_route=normalized.parser_route,
                parser_name=normalized.parser_name,
                parser_version=normalized.parser_version,
                parser_options=normalized.parser_options,
                extracted_metadata=normalized.metadata.model_dump(mode="json"),
                public_metadata=public,
                warnings=normalized.warnings,
            )
            await insert_document_artifact(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                document_version_id=document_version_id,
                kind="normalized",
                object_key=normalized_key,
                content_type="application/json; charset=utf-8",
                size_bytes=len(normalized_payload.encode("utf-8")),
                checksum_sha256=sha256_hex(normalized_payload.encode("utf-8")),
                metadata={"schema_version": normalized.schema_version},
            )
            await insert_document_artifact(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                document_version_id=document_version_id,
                kind="parser_report",
                object_key=parser_report_key,
                content_type="application/json; charset=utf-8",
                size_bytes=len(parser_report_payload.encode("utf-8")),
                checksum_sha256=sha256_hex(parser_report_payload.encode("utf-8")),
                metadata={"schema_version": "parser_report_v1"},
            )

        stage = "chunking"
        await _update_item_stage(item_id, stage, {"stage": stage})
        target = await _resolve_upload_index_target(tenant_id, kb_id, settings)
        source_url = f"{settings.api_public_base_url.rstrip('/')}/api/v1/documents/{quote(document_id, safe='')}"
        document_access = _document_access_for_ingestion(document_id=document_id, source_metadata=source_metadata)
        chunks = await asyncio.to_thread(
            chunks_for_normalized_document,
            normalized,
            document_id=document_id,
            document_version_id=document_version_id,
            source_url=source_url,
            dimensions=target["embedding_dimensions"],
        )
        chunks = _with_document_access(chunks, document_access)
        await _update_item_stage(item_id, stage, {"stage": stage, "chunks_staged": len(chunks)})

        stage = "embedding"
        embedded_chunks = await _embed_chunks(
            chunks,
            target["profile"],
            int(target["embedding_dimensions"]),
            settings,
        )

        stage = "indexing"
        await _update_item_stage(item_id, stage, {"stage": stage, "chunks_staged": len(embedded_chunks)})
        staged_chunks = _with_publication_status(embedded_chunks, "staged")
        async with connect() as conn:
            for chunk in staged_chunks:
                await upsert_chunk(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, chunk=chunk)
        published_chunks = _with_publication_status(embedded_chunks, "published")
        indexed = await asyncio.to_thread(
            bulk_index_chunks,
            published_chunks,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            settings=settings,
            write_alias=str(target["write_alias"]),
            physical_index=str(target["physical_index"]),
            read_alias=str(target["read_alias"]),
            dimensions=int(target["embedding_dimensions"]),
        )
        if indexed != len(embedded_chunks):
            raise RuntimeError("indexed chunk count did not match staged chunk count")

        stage = "published"
        async with connect() as conn:
            for chunk in published_chunks:
                await upsert_chunk(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, chunk=chunk)
            await replace_document_sections_from_chunks(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                document_version_id=document_version_id,
                chunks=published_chunks,
            )
            await update_document_version(conn, document_version_id, status="published")
            await update_ingestion_job_item(
                conn,
                item_id,
                status=JobStatus.completed,
                stage=stage,
                progress={
                    "stage": stage,
                    "parser_route": normalized.parser_route,
                    **parser_runtime_progress,
                    "chunks_staged": len(embedded_chunks),
                    "chunks_published": indexed,
                },
            )
        await _save_document_upload_job_progress(job_id, stage)
    except asyncio.CancelledError:
        raise
    except UploadValidationError as exc:
        await _fail_document_upload_item(item_id, document_version_id, stage, exc.code, exc.safe_message)
    except ParserServiceError as exc:
        if _retryable_document_ingestion_error(exc, item=item, settings=settings):
            await _retry_document_upload_item(item, stage, exc.code)
        else:
            await _fail_document_upload_item(item_id, document_version_id, stage, exc.code, exc.safe_message)
    except Exception as exc:
        if _retryable_document_ingestion_error(exc, item=item, settings=settings):
            await _retry_document_upload_item(item, stage, _safe_ingestion_error_code(exc, stage=stage))
        else:
            await _fail_document_upload_item(
                item_id,
                document_version_id,
                stage,
                _safe_ingestion_error_code(exc, stage=stage),
                "document ingestion failed",
            )


async def _resolve_upload_index_target(tenant_id: str, kb_id: str, settings: Settings) -> dict[str, Any]:
    async with connect() as conn:
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        if kb is None:
            raise ValueError("knowledge base is not available")
        read_alias = str(kb.get("active_index") or READ_ALIAS)
        row = await load_index_version_by_read_alias(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            read_alias=read_alias,
        )
        if row is not None:
            profile = get_retrieval_profile(settings.retrieval_profile, settings)
            embedding_alias = str(row["embedding_alias"])
            if profile.model_aliases.embed != embedding_alias:
                profile = profile.model_copy(deep=True)
                profile.model_aliases.embed = embedding_alias
            return {
                "profile": profile,
                "embedding_dimensions": int(row["embedding_dimensions"]),
                "physical_index": str(row["physical_index"]),
                "read_alias": str(row["read_alias"]),
                "write_alias": str(row["write_alias"]),
            }

        profile_name = _upload_profile_name_for_settings(settings)
        profile = get_retrieval_profile(profile_name, settings)
        embed_alias = profile.model_aliases.embed
        dimensions = profile.embedding_dimensions(settings.embedding_dimensions)
        upload_snapshot_id = f"default-upload-{stable_hash([tenant_id, kb_id], 16)}"
        index_names = build_index_names(
            source_type="upload",
            snapshot_id=upload_snapshot_id,
            retrieval_profile=profile.name,
            embedding_alias=embed_alias,
            embedding_dimensions=dimensions,
        )
        index_contract = build_index_contract(
            index_version=index_names["version_id"],
            source_type="upload",
            snapshot_id=upload_snapshot_id,
            physical_index=index_names["physical"],
            read_alias=index_names["read_alias"],
            embedding_alias=embed_alias,
            embedding_dimensions=dimensions,
            profile=profile,
            settings=settings,
        )
        await save_index_version(
            conn,
            index_version_id=index_names["version_id"],
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_type="upload",
            snapshot_id=upload_snapshot_id,
            retrieval_profile=profile.name,
            embedding_alias=embed_alias,
            embedding_dimensions=dimensions,
            physical_index=index_names["physical"],
            read_alias=index_names["read_alias"],
            write_alias=index_names["write_alias"],
            metadata=index_contract_metadata(index_contract),
        )
        await set_knowledge_base_active_index(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            active_index=index_names["read_alias"],
        )
        return {
            "profile": profile,
            "embedding_dimensions": dimensions,
            "physical_index": index_names["physical"],
            "read_alias": index_names["read_alias"],
            "write_alias": index_names["write_alias"],
        }


def _upload_profile_name_for_settings(settings: Settings) -> str:
    if settings.retrieval_profile in {"sota_mvp", "sota_mvp_verified", "upload_sota_mvp"}:
        return "upload_sota_mvp"
    return "upload_mock"


async def _load_required_document_version(tenant_id: str, document_version_id: str) -> dict[str, Any]:
    async with connect() as conn:
        version = await load_document_version(conn, tenant_id, document_version_id)
    if version is None:
        raise ValueError("document version is not available")
    return version


async def _update_item_stage(item_id: str, stage: str, progress: dict[str, Any]) -> None:
    async with connect() as conn:
        await update_ingestion_job_item(conn, item_id, stage=stage, progress=progress)


async def _fail_document_upload_item(
    item_id: str,
    document_version_id: str,
    stage: str,
    code: str,
    safe_message: str,
) -> None:
    async with connect() as conn:
        await update_document_version(conn, document_version_id, status="failed", warnings=[code])
        await update_ingestion_job_item(
            conn,
            item_id,
            status=JobStatus.failed,
            stage=stage,
            progress={"stage": stage, "safe_error_code": code},
            error_code=code,
            error_message=safe_message,
        )


def _retryable_document_ingestion_error(exc: BaseException, *, item: dict[str, Any], settings: Settings) -> bool:
    """Allow one durable retry only for a classified transport/provider failure."""
    if int(item.get("attempts") or 0) >= max(1, settings.safe_external_retry_attempts):
        return False
    if isinstance(exc, ParserServiceError):
        return exc.code in {
            "ConnectError",
            "ConnectTimeout",
            "ReadTimeout",
            "ReadError",
            "PoolTimeout",
            "HTTP_429",
            "HTTP_502",
            "HTTP_503",
            "HTTP_504",
        }
    return is_retryable_exception(exc)


async def _retry_document_upload_item(item: dict[str, Any], stage: str, code: str) -> None:
    """Checkpoint the transient failure before making the item claimable again."""
    attempts = max(1, int(item.get("attempts") or 1))
    delay_seconds = min(15.0, float(2 ** max(0, attempts - 1)))
    async with connect() as conn:
        await update_ingestion_job_item(
            conn,
            str(item["id"]),
            status=JobStatus.received,
            stage="retry_wait",
            progress={"stage": "retry_wait", "safe_error_code": code, "attempt": attempts},
            checkpoint={"last_stage": stage, "last_error_code": code, "retry_scheduled": True},
            error_code=code,
            error_message="transient document ingestion dependency failure",
            retry_after_seconds=delay_seconds,
        )


async def _cancel_remaining_items(job_id: str) -> None:
    async with connect() as conn:
        await conn.execute(
            text(
                """
                UPDATE ingestion_job_items
                SET status = 'cancelled',
                    stage = 'cancelled',
                    completed_at = now(),
                    updated_at = now()
                WHERE job_id = :job_id AND status IN ('received','running')
                """
            ),
            {"job_id": job_id},
        )


async def _save_document_upload_job_progress(job_id: str, stage: str) -> None:
    async with connect() as conn:
        summary = await summarize_ingestion_job_items(conn, job_id)
        latest = await _latest_document_upload_item_progress(conn, job_id)
        await update_job(
            conn,
            job_id,
            progress={
                "stage": stage,
                "documents_total": summary["total"],
                "documents_completed": summary["completed"],
                "documents_failed": summary["failed"],
                "documents_cancelled": summary["cancelled"],
                **latest,
            },
        )


async def _finalize_document_upload_job(job_id: str) -> None:
    async with connect() as conn:
        summary = await summarize_ingestion_job_items(conn, job_id)
        if summary["received"] or summary["running"]:
            return
        status = (
            JobStatus.failed
            if summary["failed"]
            else JobStatus.cancelled
            if summary["cancelled"]
            else JobStatus.completed
        )
        latest = await _latest_document_upload_item_progress(conn, job_id)
        await update_job(
            conn,
            job_id,
            status=status,
            progress={
                "stage": "terminal",
                "documents_total": summary["total"],
                "documents_completed": summary["completed"],
                "documents_failed": summary["failed"],
                "documents_cancelled": summary["cancelled"],
                **latest,
            },
        )


async def _latest_document_upload_item_progress(conn: Any, job_id: str) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            SELECT stage, progress, error_code
            FROM ingestion_job_items
            WHERE job_id = :job_id
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"job_id": job_id},
    )
    row = result.mappings().first()
    if row is None:
        return {}
    latest = dict(row)
    progress = dict(latest["progress"] or {})
    safe: dict[str, Any] = {"stage": latest["stage"]}
    for key in (
        "bytes_received",
        "parser_route",
        "parser_queue_wait_ms",
        "parser_latency_ms",
        "parser_endpoint_pool_size",
        "chunks_staged",
        "chunks_published",
        "safe_error_code",
        "detected_mime",
    ):
        if key in progress:
            safe[key] = progress[key]
    if latest.get("error_code"):
        safe["safe_error_code"] = latest["error_code"]
    return safe


def _parser_runtime_progress(normalized: Any) -> dict[str, int]:
    options = dict(getattr(normalized, "parser_options", {}) or {})
    mapping = {
        "queue_wait_ms": "parser_queue_wait_ms",
        "parser_latency_ms": "parser_latency_ms",
        "endpoint_pool_size": "parser_endpoint_pool_size",
    }
    progress: dict[str, int] = {}
    for source, target in mapping.items():
        value = options.get(source)
        if isinstance(value, int | float):
            progress[target] = max(0, int(value))
    return progress


def _with_publication_status(chunks: list[Chunk], status: str) -> list[Chunk]:
    return [replace(chunk, metadata={**chunk.metadata, "publication_status": status}) for chunk in chunks]


def _with_document_access(chunks: list[Chunk], document_access: dict[str, Any]) -> list[Chunk]:
    access = normalize_document_access(document_access)
    return [
        replace(
            chunk,
            metadata={
                **chunk.metadata,
                "document_access": access,
                "document_access_origin": str(document_access.get("document_access_origin") or ""),
            },
        )
        for chunk in chunks
    ]


def _document_access_for_ingestion(*, document_id: str, source_metadata: dict[str, Any]) -> dict[str, Any]:
    if document_id.startswith("src:"):
        return normalize_document_access(source_metadata.get("document_access"))
    return normalize_document_access(None)


def _source_document_access(
    *,
    document_metadata: dict[str, Any],
    source_default: dict[str, Any] | None,
    existing_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    if existing_metadata and existing_metadata.get("document_access_origin") == "manual":
        return normalize_document_access(existing_metadata.get("document_access")), "manual"
    if source_default is not None:
        return normalize_document_access(source_default), "source_default"
    if "document_access" in document_metadata:
        return normalize_document_access(document_metadata.get("document_access")), "connector"
    return normalize_document_access(None), "default"


async def process_source_sync(job: dict[str, Any], settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    job_id = str(job["id"])
    tenant_id = str(job["tenant_id"])
    kb_id = str(job["knowledge_base_id"])
    config = dict(job.get("config") or {})
    source_id = str(config.get("source_id") or "")
    sync_run_id = str(config.get("sync_run_id") or "")
    mode = str(config.get("mode") or "incremental")
    await _mark_running(job_id)
    if not source_id or not sync_run_id:
        async with connect() as conn:
            await update_job(
                conn,
                job_id,
                status=JobStatus.failed,
                progress={"stage": "failed", "safe_error_code": "SOURCE_SYNC_CONFIG_INVALID"},
                error_code="SOURCE_SYNC_CONFIG_INVALID",
                error_message="source sync job config is invalid",
            )
        return

    stats: dict[str, int] = {
        "documents_seen": 0,
        "documents_skipped": 0,
        "documents_changed": 0,
        "documents_published": 0,
        "documents_failed": 0,
        "tombstones_seen": 0,
        "tombstones_applied": 0,
    }
    cursor_after: dict[str, Any] = {}
    refresh_interval_seconds: int | None = None
    try:
        async with connect() as conn:
            source = await get_knowledge_source(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_id=source_id,
                include_credentials=True,
            )
            if source is None:
                raise ConnectorError("SOURCE_NOT_FOUND", "source is not available")
            await update_source_sync_run(conn, run_id=sync_run_id, status="running", checkpoint={"stage": "connecting"})
            known_states = await list_source_document_states(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_id=source_id,
            )
        refresh_interval_seconds = (
            int(source["refresh_interval_seconds"]) if source.get("refresh_interval_seconds") is not None else None
        )
        source_metadata = dict(source.get("metadata") or {})
        source_default_access = (
            normalize_document_access(source_metadata.get("document_access_default"))
            if "document_access_default" in source_metadata
            else None
        )
        connector = connector_for_kind(
            str(source["kind"]),
            dict(source.get("config") or {}),
            _decrypt_connector_credentials(resolved, dict(source.get("encrypted_credentials") or {})),
        )
        payload = await connector.sync(
            mode=mode,
            cursor=dict(source.get("sync_cursor") or {}),
            known_external_ids={str(row["external_id"]) for row in known_states if row.get("status") == "active"},
        )
        cursor_after = payload.next_cursor
        stats.update({key: int(value) for key, value in payload.stats.items() if isinstance(value, int)})
        stats["documents_seen"] = len(payload.documents)
        stats["tombstones_seen"] = len(payload.tombstones)
        await _save_source_sync_progress(job_id, sync_run_id, "documents", stats)

        for document in payload.documents:
            if await _cancel_requested(job_id):
                raise asyncio.CancelledError
            result = await _ingest_source_document(
                job_id=job_id,
                sync_run_id=sync_run_id,
                source_id=source_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                document=document,
                source_default_access=source_default_access,
                settings=resolved,
            )
            stats[result] += 1
            if result == "documents_changed":
                stats["documents_published"] += 1
            await _save_source_sync_progress(job_id, sync_run_id, "documents", stats)

        for tombstone in payload.tombstones:
            if await _cancel_requested(job_id):
                raise asyncio.CancelledError
            applied = await _apply_source_tombstone(
                sync_run_id=sync_run_id,
                source_id=source_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                external_id=tombstone.external_id,
                tombstone_version=tombstone.source_version,
                metadata=tombstone.metadata,
                settings=resolved,
            )
            if applied:
                stats["tombstones_applied"] += 1
            await _save_source_sync_progress(job_id, sync_run_id, "tombstones", stats)

        async with connect() as conn:
            await update_source_sync_run(
                conn,
                run_id=sync_run_id,
                status="completed",
                cursor_after=cursor_after,
                checkpoint={"stage": "completed"},
                stats=stats,
            )
            await finish_knowledge_source_sync(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_id=source_id,
                run_id=sync_run_id,
                status="completed",
                cursor_after=cursor_after,
                refresh_interval_seconds=refresh_interval_seconds,
            )
            await update_job(conn, job_id, status=JobStatus.completed, progress={"stage": "completed", **stats})
    except asyncio.CancelledError:
        async with connect() as conn:
            await update_source_sync_run(
                conn,
                run_id=sync_run_id,
                status="cancelled",
                cursor_after=cursor_after,
                checkpoint={"stage": "cancelled"},
                stats=stats,
            )
            await finish_knowledge_source_sync(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_id=source_id,
                run_id=sync_run_id,
                status="cancelled",
                cursor_after=cursor_after,
                refresh_interval_seconds=refresh_interval_seconds,
            )
            await update_job(conn, job_id, status=JobStatus.cancelled, progress={"stage": "cancelled", **stats})
    except ConnectorError as exc:
        await _fail_source_sync(
            job_id,
            sync_run_id,
            source_id,
            tenant_id,
            kb_id,
            cursor_after,
            stats,
            refresh_interval_seconds,
            exc.code,
            exc.safe_message,
        )
    except Exception as exc:
        await _fail_source_sync(
            job_id,
            sync_run_id,
            source_id,
            tenant_id,
            kb_id,
            cursor_after,
            stats,
            refresh_interval_seconds,
            _safe_ingestion_error_code(exc, stage="source_sync"),
            "source sync failed",
        )


def _decrypt_connector_credentials(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    from wikipediarag.oidc_service import decrypt_server_tokens

    return decrypt_server_tokens(settings, {str(key): str(value) for key, value in payload.items()})


async def _ingest_source_document(
    *,
    job_id: str,
    sync_run_id: str,
    source_id: str,
    tenant_id: str,
    kb_id: str,
    document: SourceDocument,
    source_default_access: dict[str, Any] | None,
    settings: Settings,
) -> str:
    parser_profile = str(document.metadata.get("parser_profile") or "standard")
    async with connect() as conn:
        existing = await get_source_document_state(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            external_id=document.external_id,
        )
    if (
        existing is not None
        and existing.get("status") == "active"
        and str(existing.get("source_version")) == document.source_version
        and str(existing.get("content_hash")) == document.content_hash
    ):
        existing_document_metadata: dict[str, Any] = {}
        async with connect() as conn:
            existing_document = (
                await get_document_public(conn, tenant_id, str(existing["document_id"]))
                if existing.get("document_id")
                else None
            )
            existing_document_metadata = dict((existing_document or {}).get("metadata") or {})
            document_access, document_access_origin = _source_document_access(
                document_metadata=document.metadata,
                source_default=source_default_access,
                existing_metadata=existing_document_metadata,
            )
            await upsert_source_document_state(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                source_id=source_id,
                sync_run_id=sync_run_id,
                external_id=document.external_id,
                title=document.title,
                source_uri=document.source_uri,
                source_url=document.source_url,
                source_version=document.source_version,
                content_hash=document.content_hash,
                document_id=str(existing["document_id"]),
                document_version_id=str(existing["document_version_id"]),
                metadata={
                    **document.metadata,
                    "document_access": document_access,
                    "document_access_origin": document_access_origin,
                },
            )
            await update_document_access_metadata(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=str(existing["document_id"]),
                document_version_id=str(existing["document_version_id"]),
                document_access=document_access,
                origin=document_access_origin,
            )
            kb = await get_knowledge_base(conn, tenant_id, kb_id)
            read_alias = str((kb or {}).get("active_index") or READ_ALIAS)
        await asyncio.to_thread(
            update_document_access,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=str(existing["document_id"]),
            document_access=document_access,
            origin=document_access_origin,
            settings=settings,
            read_alias=read_alias,
        )
        return "documents_skipped"

    object_key = (
        f"sources/{tenant_id}/{kb_id}/{source_id}/{stable_hash([document.external_id], 24)}/{document.content_hash}"
    )
    await asyncio.to_thread(
        put_bytes,
        object_key,
        document.content_bytes,
        content_type=document.content_type,
        settings=settings,
    )
    existing_document_metadata = {}
    if existing is not None and existing.get("document_id"):
        async with connect() as conn:
            existing_document = await get_document_public(conn, tenant_id, str(existing["document_id"]))
            existing_document_metadata = dict((existing_document or {}).get("metadata") or {})
    document_access, document_access_origin = _source_document_access(
        document_metadata=document.metadata,
        source_default=source_default_access,
        existing_metadata=existing_document_metadata,
    )
    async with connect() as conn:
        document_id, document_version_id, item_id = await create_source_document_ingestion_item(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            sync_run_id=sync_run_id,
            job_id=job_id,
            external_id=document.external_id,
            title=document.title,
            source_uri=document.source_uri,
            source_url=document.source_url,
            source_version=document.source_version,
            content_hash=document.content_hash,
            object_key=object_key,
            content_type=document.content_type,
            size_bytes=len(document.content_bytes),
            parser_profile=parser_profile,
            metadata={
                **document.metadata,
                "document_access": document_access,
                "document_access_origin": document_access_origin,
            },
        )
    await _process_document_upload_item(
        {
            "id": str(item_id),
            "job_id": job_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "tenant_id": tenant_id,
            "knowledge_base_id": kb_id,
        },
        settings,
    )
    async with connect() as conn:
        item = await conn.execute(text("SELECT status FROM ingestion_job_items WHERE id = :id"), {"id": str(item_id)})
        item_row = item.mappings().first()
        if item_row is None or str(item_row["status"]) != JobStatus.completed.value:
            return "documents_failed"
        old_version_id = str(existing.get("document_version_id")) if existing is not None else ""
        await upsert_source_document_state(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            sync_run_id=sync_run_id,
            external_id=document.external_id,
            title=document.title,
            source_uri=document.source_uri,
            source_url=document.source_url,
            source_version=document.source_version,
            content_hash=document.content_hash,
            document_id=document_id,
            document_version_id=document_version_id,
            metadata=document.metadata,
        )
        if old_version_id and old_version_id != document_version_id:
            await mark_document_version_chunks_deleted(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_version_id=old_version_id,
            )
            kb = await get_knowledge_base(conn, tenant_id, kb_id)
            read_alias = str(kb.get("active_index") or READ_ALIAS) if kb else READ_ALIAS
            await asyncio.to_thread(
                delete_document_version_chunks,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_version_id=old_version_id,
                settings=settings,
                read_alias=read_alias,
            )
    return "documents_changed"


async def _apply_source_tombstone(
    *,
    sync_run_id: str,
    source_id: str,
    tenant_id: str,
    kb_id: str,
    external_id: str,
    tombstone_version: str,
    metadata: dict[str, Any],
    settings: Settings,
) -> bool:
    async with connect() as conn:
        state = await mark_source_document_tombstone(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            sync_run_id=sync_run_id,
            external_id=external_id,
            tombstone_version=tombstone_version,
            metadata=metadata,
        )
        if state is None or not state.get("document_id"):
            return False
        document_id = str(state["document_id"])
        purge_after = datetime.now(UTC).replace(microsecond=0) + _source_delete_retention(settings)
        await soft_delete_document(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
            deleted_by_user_id=settings.default_user_id,
            purge_after=purge_after,
            deletion_reason="source_tombstone",
        )
        await create_document_deletion_job(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
            purge_after=purge_after,
        )
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        read_alias = str(kb.get("active_index") or READ_ALIAS) if kb else READ_ALIAS
    await asyncio.to_thread(
        delete_document_chunks,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
        document_id=document_id,
        settings=settings,
        read_alias=read_alias,
    )
    return True


def _source_delete_retention(settings: Settings) -> timedelta:
    return timedelta(days=max(0, settings.document_soft_delete_retention_days))


async def _save_source_sync_progress(job_id: str, sync_run_id: str, stage: str, stats: dict[str, int]) -> None:
    progress = {"stage": stage, "sync_run_id": sync_run_id, **stats}
    async with connect() as conn:
        await update_source_sync_run(
            conn,
            run_id=sync_run_id,
            status="running",
            checkpoint={"stage": stage},
            stats=stats,
        )
        await update_job(conn, job_id, progress=progress)


async def _fail_source_sync(
    job_id: str,
    sync_run_id: str,
    source_id: str,
    tenant_id: str,
    kb_id: str,
    cursor_after: dict[str, Any],
    stats: dict[str, int],
    refresh_interval_seconds: int | None,
    error_code: str,
    error_message: str,
) -> None:
    async with connect() as conn:
        await update_source_sync_run(
            conn,
            run_id=sync_run_id,
            status="failed",
            cursor_after=cursor_after,
            checkpoint={"stage": "failed", "safe_error_code": error_code[:120]},
            stats=stats,
            error_code=error_code,
            error_message=error_message,
        )
        await finish_knowledge_source_sync(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            source_id=source_id,
            run_id=sync_run_id,
            status="failed",
            cursor_after=cursor_after,
            refresh_interval_seconds=refresh_interval_seconds,
        )
        await update_job(
            conn,
            job_id,
            status=JobStatus.failed,
            progress={"stage": "failed", "safe_error_code": error_code[:120], **stats},
            error_code=error_code,
            error_message=error_message,
        )


async def process_document_delete(job: dict[str, Any], settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    job_id = str(job["id"])
    tenant_id = str(job["tenant_id"])
    kb_id = str(job["knowledge_base_id"])
    config = dict(job.get("config") or {})
    document_id = str(config.get("document_id") or "")
    purge_after = _parse_due_time(config.get("purge_after"))
    if not document_id:
        async with connect() as conn:
            await update_job(
                conn,
                job_id,
                status=JobStatus.failed,
                progress={"stage": "failed", "safe_error_code": "DOCUMENT_ID_MISSING"},
                error_code="DOCUMENT_ID_MISSING",
                error_message="document deletion job is invalid",
            )
        return
    if purge_after is not None and purge_after > datetime.now(UTC):
        async with connect() as conn:
            await update_job(conn, job_id, progress={"stage": "scheduled", "purge_after": purge_after.isoformat()})
        return

    await _mark_running(job_id)
    try:
        async with connect() as conn:
            await update_job(conn, job_id, progress={"stage": "loading_artifacts"})
            artifact_keys = await list_document_artifact_keys(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
            )
            kb = await get_knowledge_base(conn, tenant_id, kb_id)
        deleted_objects = await asyncio.to_thread(delete_objects, artifact_keys, resolved)
        read_alias = str(kb.get("active_index") or READ_ALIAS) if kb else READ_ALIAS
        deleted_search_chunks = await asyncio.to_thread(
            delete_document_chunks,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
            settings=resolved,
            read_alias=read_alias,
        )
        async with connect() as conn:
            deleted_db_chunks = await mark_document_purged(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
            )
            await update_job(
                conn,
                job_id,
                status=JobStatus.completed,
                progress={
                    "stage": "purged",
                    "objects_deleted": deleted_objects,
                    "search_chunks_deleted": deleted_search_chunks,
                    "db_chunks_deleted": deleted_db_chunks,
                },
            )
    except Exception as exc:
        safe_error_code = _safe_ingestion_error_code(exc, stage="purge")
        async with connect() as conn:
            await mark_document_purge_failed(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                safe_error_code=safe_error_code,
            )
            await update_job(
                conn,
                job_id,
                status=JobStatus.failed,
                progress={"stage": "purge_failed", "safe_error_code": safe_error_code},
                error_code=safe_error_code,
                error_message="document purge failed",
            )


def _parse_due_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def process_job(job: dict[str, Any], settings: Settings | None = None) -> None:
    if job["kind"] == "wikipedia_xml":
        await process_wiki_import(job, settings)
    elif job["kind"] == "wikipedia_zim":
        await process_zim_import(job, settings)
    elif job["kind"] == "document_upload":
        await process_document_upload(job, settings)
    elif job["kind"] == "source_sync":
        await process_source_sync(job, settings)
    elif job["kind"] == "document_delete":
        await process_document_delete(job, settings)
    elif job["kind"] == "deep_research":
        from wikipediarag.deep_research import process_deep_research

        await process_deep_research(job, settings)
    else:
        raise ValueError(f"unsupported ingestion job kind {job['kind']}")


async def claim_and_process_once(
    settings: Settings | None = None,
    *,
    allowed_kinds: list[str] | tuple[str, ...] | None = None,
    lease_id: str | None = None,
) -> bool:
    from wikipediarag.repository import (
        StaleWorkerLeaseError,
        claim_next_job,
        heartbeat_job_lease,
        reset_worker_lease_context,
        set_worker_lease_context,
    )

    resolved = settings or get_settings()
    lease_id = lease_id or str(uuid.uuid4())
    async with connect(resolved) as conn:
        job = await claim_next_job(
            conn,
            lease_id=lease_id,
            allowed_kinds=allowed_kinds,
            lease_seconds=resolved.worker_job_lease_seconds,
        )
    if job is None:
        return False
    job_id = str(job["id"])
    heartbeat_lost = asyncio.Event()
    lease_lost = asyncio.Event()

    async def heartbeat_loop() -> None:
        try:
            while True:
                await asyncio.sleep(max(resolved.worker_job_heartbeat_seconds, 1))
                async with connect(resolved) as heartbeat_conn:
                    alive = await heartbeat_job_lease(
                        heartbeat_conn,
                        job_id=job_id,
                        lease_id=lease_id,
                        lease_seconds=resolved.worker_job_lease_seconds,
                    )
                if not alive:
                    heartbeat_lost.set()
                    lease_lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            heartbeat_lost.set()
            lease_lost.set()

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    job_task: asyncio.Task[None] | None = None
    context_token = set_worker_lease_context(lease_id)
    try:

        async def run_job() -> None:
            await process_job(job, resolved)

        job_task = asyncio.create_task(run_job())
        done, _ = await asyncio.wait({job_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED)
        if lease_lost.is_set() and not job_task.done():
            job_task.cancel()
            await asyncio.gather(job_task, return_exceptions=True)
            raise StaleWorkerLeaseError(f"job lease lost: {job_id}")
        if job_task in done:
            await job_task
            if lease_lost.is_set():
                raise StaleWorkerLeaseError(f"job lease lost: {job_id}")
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        if job_task is not None and not job_task.done():
            job_task.cancel()
            await asyncio.gather(job_task, return_exceptions=True)
        reset_worker_lease_context(context_token)
    return True
