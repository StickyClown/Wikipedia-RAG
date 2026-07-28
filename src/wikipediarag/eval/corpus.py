from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.config import Settings, get_settings
from wikipediarag.db import connect
from wikipediarag.eval.artifacts import ARTIFACT_ROOT
from wikipediarag.eval.hashing import cached_file_sha256, stable_json_hash
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.zim_dump import resolve_zim_path


@dataclass(frozen=True)
class CorpusSnapshot:
    snapshot_id: str
    index_version: str
    physical_index: str
    read_alias: str
    retrieval_profile: str
    retrieval_profile_hash: str
    embedding_alias: str
    embedding_dimensions: int
    zim_checksum: str
    zim_path: Path


@dataclass(frozen=True)
class CorpusChunk:
    chunk_id: str
    document_id: str
    section_id: str
    title: str
    content: str
    source_url: str
    section_path: tuple[str, ...]
    parent_chunk_id: str
    prev_chunk_id: str | None
    next_chunk_id: str | None
    metadata: dict[str, Any]


async def load_corpus_snapshot(settings: Settings | None = None) -> CorpusSnapshot:
    resolved = settings or get_settings()
    async with connect(resolved) as conn:
        result = await conn.execute(
            text(
                """
                SELECT id, snapshot_id, retrieval_profile, embedding_alias, embedding_dimensions,
                       physical_index, read_alias
                FROM index_versions
                WHERE source_type IN ('wikipedia_zim', 'zim')
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        )
        row = result.mappings().first()
    if row is None:
        raise RuntimeError("no active wikipedia_zim index_version found; import ZIM first")
    profile = get_retrieval_profile(str(row["retrieval_profile"]), resolved)
    profile_hash = stable_json_hash(profile.model_dump(mode="json"))
    zim_path = resolve_zim_path(resolved.zim_dir, resolved.zim_filename)
    checksum = cached_file_sha256(zim_path, ARTIFACT_ROOT / "cache" / "zim-checksums.json")
    return CorpusSnapshot(
        snapshot_id=str(row["snapshot_id"]),
        index_version=str(row["id"]),
        physical_index=str(row["physical_index"]),
        read_alias=str(row["read_alias"]),
        retrieval_profile=str(row["retrieval_profile"]),
        retrieval_profile_hash=profile_hash,
        embedding_alias=str(row["embedding_alias"]),
        embedding_dimensions=int(row["embedding_dimensions"]),
        zim_checksum=checksum,
        zim_path=zim_path,
    )


async def load_candidate_chunks(limit: int = 1200, *, settings: Settings | None = None) -> list[CorpusChunk]:
    resolved = settings or get_settings()
    async with connect(resolved) as conn:
        result = await conn.execute(
            text(
                """
                SELECT c.id, c.document_id, c.title, c.section_path, c.content, c.source_url,
                       c.parent_chunk_id, c.prev_chunk_id, c.next_chunk_id, c.metadata
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.source_type = 'wikipedia_zim'
                  AND length(c.content) >= 450
                  AND c.title !~ '^[!@#$%^&*()_+={}\\[\\]|;:'',.<>/?`~ -]+$'
                  AND c.title !~ '^[0-9]+\\s+год( до н\\. э\\.)?$'
                  AND c.title !~ '^[0-9]+-е$'
                  AND c.title NOT ILIKE '%(значения)%'
                  AND c.title NOT ILIKE '%список%'
                  AND c.content !~ '^[0-9[:space:][:punct:]]+$'
                ORDER BY md5(c.id)
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    return [_chunk_from_row(dict(row)) for row in result.mappings()]


async def load_alias_chunks(limit: int = 300, *, settings: Settings | None = None) -> list[tuple[str, CorpusChunk]]:
    resolved = settings or get_settings()
    async with connect(resolved) as conn:
        result = await conn.execute(
            text(
                """
                SELECT r.title AS alias_title,
                       c.id, c.document_id, c.title, c.section_path, c.content, c.source_url,
                       c.parent_chunk_id, c.prev_chunk_id, c.next_chunk_id, c.metadata
                FROM documents r
                JOIN documents d
                  ON d.source_type = 'wikipedia_zim'
                 AND d.metadata->>'zim_entry_path' = r.metadata->>'redirect_target'
                JOIN chunks c ON c.document_id = d.id
                WHERE r.source_type = 'wikipedia_zim_redirect'
                  AND length(c.content) >= 450
                  AND r.title <> c.title
                ORDER BY md5(r.id)
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    return [(str(row["alias_title"]), _chunk_from_row(dict(row))) for row in result.mappings()]


async def load_chunk_refs(chunk_ids: Iterable[str], *, settings: Settings | None = None) -> dict[str, CorpusChunk]:
    ids = sorted(set(chunk_ids))
    if not ids:
        return {}
    resolved = settings or get_settings()
    async with connect(resolved) as conn:
        return await load_chunk_refs_conn(conn, ids)


async def load_chunk_refs_conn(conn: AsyncConnection, chunk_ids: Iterable[str]) -> dict[str, CorpusChunk]:
    ids = sorted(set(chunk_ids))
    if not ids:
        return {}
    result = await conn.execute(
        text(
            """
            SELECT id, document_id, title, section_path, content, source_url,
                   parent_chunk_id, prev_chunk_id, next_chunk_id, metadata
            FROM chunks
            WHERE id = ANY(:ids)
            """
        ),
        {"ids": ids},
    )
    chunks = [_chunk_from_row(dict(row)) for row in result.mappings()]
    return {chunk.chunk_id: chunk for chunk in chunks}


def _chunk_from_row(row: dict[str, Any]) -> CorpusChunk:
    section_path = tuple(str(item) for item in row.get("section_path") or [])
    parent = str(row.get("parent_chunk_id") or "")
    return CorpusChunk(
        chunk_id=str(row["id"]),
        document_id=str(row["document_id"]),
        section_id=parent or str(row["id"]),
        title=str(row["title"]),
        content=str(row["content"]),
        source_url=str(row["source_url"]),
        section_path=section_path,
        parent_chunk_id=parent,
        prev_chunk_id=str(row["prev_chunk_id"]) if row.get("prev_chunk_id") else None,
        next_chunk_id=str(row["next_chunk_id"]) if row.get("next_chunk_id") else None,
        metadata=dict(row.get("metadata") or {}),
    )
