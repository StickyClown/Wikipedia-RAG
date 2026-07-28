from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from wikipediarag.config import Settings, get_settings
from wikipediarag.db import connect
from wikipediarag.model_client import embeddings
from wikipediarag.model_registry import get_model_registry
from wikipediarag.repository import (
    get_job,
    save_index_version,
    set_knowledge_base_active_index,
    update_job,
    upsert_chunk,
    upsert_document,
    upsert_wiki_page_and_chunks,
)
from wikipediarag.retrieval_contract import build_index_contract, index_contract_metadata
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import JobStatus
from wikipediarag.search_index import (
    PHYSICAL_INDEX,
    READ_ALIAS,
    WRITE_ALIAS,
    build_index_names,
    bulk_index_chunks,
    ensure_index,
)
from wikipediarag.storage import put_text
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
        index_version_id = f"wikipedia_xml:{snapshot_id}:{profile.name}:{embed_alias}:{dimensions}"
        index_contract = build_index_contract(
            index_version=index_version_id,
            source_type="wikipedia_xml",
            snapshot_id=snapshot_id,
            physical_index=PHYSICAL_INDEX,
            read_alias=READ_ALIAS,
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
        await asyncio.to_thread(ensure_index, resolved)
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
                physical_index=PHYSICAL_INDEX,
                read_alias=READ_ALIAS,
                write_alias=WRITE_ALIAS,
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
            await set_knowledge_base_active_index(
                conn,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                active_index=READ_ALIAS,
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
                error_code=type(exc).__name__,
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
                error_code=type(exc).__name__,
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
    async with connect() as conn:
        for redirect in redirects:
            document_id = f"zim-redirect:{snapshot_id}:{stable_zim_id(redirect.zim_entry_path)}"
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
                    "entry_index": redirect.entry_index,
                },
            )
        for page, embedded_page_chunks in embedded_by_page:
            document_id = (
                embedded_page_chunks[0].document_id
                if embedded_page_chunks
                else f"zim:{snapshot_id}:{stable_zim_id(page.zim_entry_path)}"
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
                    "redirect_target": page.redirect_target,
                    "source_url": page.source_url,
                },
            )
            for chunk in embedded_page_chunks:
                await upsert_chunk(conn, tenant_id=tenant_id, knowledge_base_id=kb_id, chunk=chunk)
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


async def process_job(job: dict[str, Any], settings: Settings | None = None) -> None:
    if job["kind"] == "wikipedia_xml":
        await process_wiki_import(job, settings)
    elif job["kind"] == "wikipedia_zim":
        await process_zim_import(job, settings)
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
