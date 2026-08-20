from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import wikipediarag.worker as worker
from wikipediarag.config import Settings
from wikipediarag.db import connect, ensure_schema
from wikipediarag.repository import (
    cleanup_completed_search_projection_events,
    enqueue_document_publication_projection,
    mark_search_projection_reconciliation_due,
    save_index_version,
    schedule_historical_search_projection_reconciliations,
    upsert_chunk,
)
from wikipediarag.search_index import (
    bulk_index_chunks,
    delete_exact_projection_documents,
    ensure_index,
    get_client,
    read_document_projection,
)
from wikipediarag.wiki_dump import Chunk

_TEST_INDEXES: set[str] = set()


@dataclass(frozen=True)
class _Case:
    tenant_id: str
    knowledge_base_id: str
    physical_index: str
    read_alias: str
    write_alias: str
    document_ids: tuple[str, ...]
    chunks_by_document: dict[str, tuple[Chunk, ...]]


def _integration_database_url() -> str:
    value = os.getenv("WIKIPEDIARAG_INTEGRATION_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("set WIKIPEDIARAG_INTEGRATION_DATABASE_URL to a localhost PostgreSQL URL")
    return value


def _integration_opensearch_url() -> str:
    value = os.getenv("WIKIPEDIARAG_INTEGRATION_OPENSEARCH_URL", "").strip()
    if not value:
        pytest.fail("set WIKIPEDIARAG_INTEGRATION_OPENSEARCH_URL to a reachable OpenSearch URL")
    return value


@pytest_asyncio.fixture
async def isolated_settings() -> Any:
    base = make_url(_integration_database_url())
    database_name = f"wikipediarag_auth003_{uuid.uuid4().hex[:16]}"
    admin_url = base.set(database="postgres")
    admin_engine = create_async_engine(admin_url.render_as_string(hide_password=False), pool_pre_ping=True)
    async with admin_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    await admin_engine.dispose()

    database_url = base.set(database=database_name).render_as_string(hide_password=False)
    settings = Settings(
        database_url=database_url,
        opensearch_url=_integration_opensearch_url(),
        retrieval_profile="test_mock",
        embedding_dimensions=3,
        search_projection_reconcile_batch_size=2,
        search_projection_reconcile_mutation_batch_size=1,
        search_projection_event_retention_batch_size=1,
        search_projection_event_retention_days=30,
        search_projection_reconcile_interval_seconds=1,
        worker_job_lease_seconds=30,
    )
    await ensure_schema(settings)
    try:
        yield settings
    finally:
        client = get_client(settings)
        for physical_index in sorted(_TEST_INDEXES):
            if client.indices.exists(index=physical_index):
                client.indices.delete(index=physical_index)
        _TEST_INDEXES.clear()
        from wikipediarag.db import get_engine

        await get_engine(settings).dispose()
        drop_engine = create_async_engine(admin_url.render_as_string(hide_password=False), pool_pre_ping=True)
        async with drop_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        await drop_engine.dispose()


async def _seed_case(settings: Settings, *, document_count: int = 1, chunks_per_document: int = 1) -> _Case:
    tenant_id = str(uuid.uuid4())
    knowledge_base_id = str(uuid.uuid4())
    suffix = uuid.uuid4().hex[:16]
    physical_index = f"auth003-test-{suffix}"
    read_alias = f"auth003-read-{suffix}"
    write_alias = f"auth003-write-{suffix}"
    index_version_id = f"auth003-index-{suffix}"
    ensure_index(
        settings,
        physical_index=physical_index,
        read_alias=read_alias,
        write_alias=write_alias,
        dimensions=3,
    )
    _TEST_INDEXES.add(physical_index)
    async with connect(settings) as conn:
        await conn.execute(
            text("INSERT INTO tenants(id, slug, name) VALUES (:id, :slug, 'AUTH-003 integration tenant')"),
            {"id": tenant_id, "slug": f"auth003-{suffix}"},
        )
        await conn.execute(
            text(
                "INSERT INTO knowledge_bases(id, tenant_id, name, active_index) VALUES (:id, :tenant, 'AUTH-003', :idx)"
            ),
            {"id": knowledge_base_id, "tenant": tenant_id, "idx": read_alias},
        )
        await save_index_version(
            conn,
            index_version_id=index_version_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            source_type="integration",
            snapshot_id=suffix,
            retrieval_profile="test_mock",
            embedding_alias="test_embedding",
            embedding_dimensions=3,
            physical_index=physical_index,
            read_alias=read_alias,
            write_alias=write_alias,
            metadata={},
        )
        document_ids: list[str] = []
        chunks_by_document: dict[str, tuple[Chunk, ...]] = {}
        for document_number in range(document_count):
            document_id = f"auth003-document-{suffix}-{document_number}"
            version_id = f"{document_id}-version-1"
            document_ids.append(document_id)
            await conn.execute(
                text(
                    "INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata) "
                    "VALUES (:id, :tenant, :kb, 'integration', :title, :uri, CAST(:metadata AS jsonb))"
                ),
                {
                    "id": document_id,
                    "tenant": tenant_id,
                    "kb": knowledge_base_id,
                    "title": document_id,
                    "uri": f"integration://{document_id}",
                    "metadata": json.dumps({"current_version_id": version_id}),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO document_versions(id, document_id, tenant_id, knowledge_base_id, version_ordinal, "
                    "status, content_hash, original_artifact_key, lifecycle_state, published_at) "
                    "VALUES (:id, :document, :tenant, :kb, 1, 'published', :hash, :artifact, 'active', now())"
                ),
                {
                    "id": version_id,
                    "document": document_id,
                    "tenant": tenant_id,
                    "kb": knowledge_base_id,
                    "hash": f"version-hash-{document_id}",
                    "artifact": f"integration/{document_id}/original",
                },
            )
            chunks: list[Chunk] = []
            for chunk_number in range(chunks_per_document):
                chunk_id = f"{document_id}-chunk-{chunk_number}"
                chunks.append(
                    Chunk(
                        id=chunk_id,
                        document_id=document_id,
                        page_id=chunk_number + 1,
                        revision_id=0,
                        title=document_id,
                        section_path=("integration",),
                        content=f"canonical content {document_id} {chunk_number}",
                        parent_chunk_id=None,
                        prev_chunk_id=None,
                        next_chunk_id=None,
                        source_uri=f"integration://{document_id}#{chunk_number}",
                        source_url=f"https://integration.invalid/{document_id}/{chunk_number}",
                        content_hash=f"chunk-hash-{document_id}-{chunk_number}",
                        embedding=[1.0, 0.0, 0.0],
                        metadata={
                            "document_version_id": version_id,
                            "chunk_ordinal": chunk_number,
                            "publication_status": "published",
                        },
                    )
                )
            for chunk in chunks:
                await upsert_chunk(conn, tenant_id=tenant_id, knowledge_base_id=knowledge_base_id, chunk=chunk)
            chunks_by_document[document_id] = tuple(chunks)
    return _Case(
        tenant_id,
        knowledge_base_id,
        physical_index,
        read_alias,
        write_alias,
        tuple(document_ids),
        chunks_by_document,
    )


async def _set_scan_complete(settings: Settings) -> None:
    async with connect(settings) as conn:
        await conn.execute(
            text(
                "UPDATE search_projection_reconciliation_scan_state "
                "SET cursor_document_id = 'zzzzzzzz', completed_at = now() WHERE generation = 1"
            )
        )


async def _projection_event_count(settings: Settings, *, tenant_id: str, event_kind: str) -> int:
    async with connect(settings) as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM search_projection_events WHERE tenant_id=:tenant AND event_kind=:kind"),
                    {"tenant": tenant_id, "kind": event_kind},
                )
            ).scalar_one()
        )


async def _insert_reconciliation_due(settings: Settings, case: _Case, document_id: str) -> None:
    async with connect(settings) as conn:
        await mark_search_projection_reconciliation_due(
            conn,
            tenant_id=case.tenant_id,
            knowledge_base_id=case.knowledge_base_id,
            document_id=document_id,
        )


@pytest.mark.asyncio
async def test_real_reconciliation_repairs_missing_and_extra_projection_and_is_idempotent(
    isolated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = isolated_settings
    case = await _seed_case(settings, chunks_per_document=2)
    document_id = case.document_ids[0]
    chunks = list(case.chunks_by_document[document_id])
    async with connect(settings) as conn:
        await enqueue_document_publication_projection(
            conn,
            tenant_id=case.tenant_id,
            knowledge_base_id=case.knowledge_base_id,
            document_id=document_id,
            document_version_id=str(chunks[0].metadata["document_version_id"]),
            chunks=chunks,
        )
    bulk_index_chunks(
        chunks,
        knowledge_base_id=case.knowledge_base_id,
        settings=settings,
        write_alias=case.write_alias,
        physical_index=case.physical_index,
        read_alias=case.read_alias,
        dimensions=3,
        refresh="wait_for",
    )
    delete_exact_projection_documents(
        document_ids=[f"{case.tenant_id}:{case.knowledge_base_id}:{chunks[0].id}"],
        settings=settings,
        read_alias=case.read_alias,
        refresh="wait_for",
    )
    extra = Chunk(
        id=f"{document_id}-extra",
        document_id=document_id,
        page_id=99,
        revision_id=0,
        title="extra",
        section_path=("integration",),
        content="extra",
        parent_chunk_id=None,
        prev_chunk_id=None,
        next_chunk_id=None,
        source_uri="integration://extra",
        source_url="https://integration.invalid/extra",
        content_hash="extra-hash",
        embedding=[1.0, 0.0, 0.0],
        metadata={
            "document_version_id": str(chunks[0].metadata["document_version_id"]),
            "publication_status": "published",
        },
    )
    bulk_index_chunks(
        [extra],
        knowledge_base_id=case.knowledge_base_id,
        settings=settings,
        write_alias=case.write_alias,
        physical_index=case.physical_index,
        read_alias=case.read_alias,
        dimensions=3,
        refresh="wait_for",
    )
    before_events = await _projection_event_count(settings, tenant_id=case.tenant_id, event_kind="document_publication")
    await _set_scan_complete(settings)
    first = await worker._process_search_projection_reconciliation_once(settings, lease_id="exact-owner")
    assert first == 1
    observed = read_document_projection(
        knowledge_base_id=case.knowledge_base_id,
        document_id=document_id,
        limit=10,
        settings=settings,
        read_alias=case.read_alias,
    )
    assert {str(row["_source"]["chunk_id"]) for row in observed} == {chunk.id for chunk in chunks}
    assert (
        await _projection_event_count(settings, tenant_id=case.tenant_id, event_kind="document_publication")
        == before_events
    )

    await _insert_reconciliation_due(settings, case, document_id)
    mutation_counts = {"delete": 0, "index": 0}
    worker_api = cast(Any, worker)
    original_delete = worker_api.delete_exact_projection_documents
    original_index = worker_api.bulk_index_chunks

    def counted_delete(*args: Any, **kwargs: Any) -> int:
        mutation_counts["delete"] += 1
        return int(original_delete(*args, **kwargs))

    def counted_index(*args: Any, **kwargs: Any) -> int:
        mutation_counts["index"] += 1
        return int(original_index(*args, **kwargs))

    monkeypatch.setattr(worker_api, "delete_exact_projection_documents", counted_delete)
    monkeypatch.setattr(worker_api, "bulk_index_chunks", counted_index)
    assert await worker._process_search_projection_reconciliation_once(settings, lease_id="exact-owner-2") == 1
    assert mutation_counts == {"delete": 0, "index": 0}


@pytest.mark.asyncio
async def test_historical_scheduling_is_bounded_idempotent_and_concurrent_safe(isolated_settings: Settings) -> None:
    settings = isolated_settings
    case = await _seed_case(settings, document_count=5)
    generation = 9001
    async with connect(settings) as conn:
        await conn.execute(
            text("INSERT INTO search_projection_reconciliation_scan_state(generation) VALUES (:generation)"),
            {"generation": generation},
        )

    async def schedule_once() -> int:
        async with connect(settings) as conn:
            return int(
                await schedule_historical_search_projection_reconciliations(conn, batch_size=2, generation=generation)
            )

    first_pair = await asyncio.gather(schedule_once(), schedule_once())
    assert all(value <= 2 for value in first_pair)
    scheduled_counts = [*first_pair]
    while True:
        count = await schedule_once()
        scheduled_counts.append(count)
        if count == 0:
            break
    assert max(scheduled_counts) <= 2
    assert sum(scheduled_counts) == len(case.document_ids)
    assert await schedule_once() == 0
    async with connect(settings) as conn:
        row = await conn.execute(
            text(
                "SELECT count(*) AS count, count(DISTINCT document_id) AS distinct_count "
                "FROM search_projection_reconciliation "
                "WHERE tenant_id=:tenant AND reconciliation_generation=:generation"
            ),
            {"tenant": case.tenant_id, "generation": generation},
        )
        result = dict(row.mappings().one())
    assert result == {"count": len(case.document_ids), "distinct_count": len(case.document_ids)}


@pytest.mark.asyncio
async def test_lease_loss_reclaim_converges_after_successful_previous_mutation(
    isolated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = isolated_settings
    case = await _seed_case(settings, chunks_per_document=2)
    document_id = case.document_ids[0]
    chunks = list(case.chunks_by_document[document_id])
    bulk_index_chunks(
        chunks,
        knowledge_base_id=case.knowledge_base_id,
        settings=settings,
        write_alias=case.write_alias,
        physical_index=case.physical_index,
        read_alias=case.read_alias,
        dimensions=3,
        refresh="wait_for",
    )
    delete_exact_projection_documents(
        document_ids=[f"{case.tenant_id}:{case.knowledge_base_id}:{chunks[0].id}"],
        settings=settings,
        read_alias=case.read_alias,
        refresh="wait_for",
    )
    extra = Chunk(
        id=f"{document_id}-extra",
        document_id=document_id,
        page_id=99,
        revision_id=0,
        title="extra",
        section_path=("integration",),
        content="extra",
        parent_chunk_id=None,
        prev_chunk_id=None,
        next_chunk_id=None,
        source_uri="integration://extra",
        source_url="https://integration.invalid/extra",
        content_hash="extra-hash",
        embedding=[1.0, 0.0, 0.0],
        metadata={
            "document_version_id": str(chunks[0].metadata["document_version_id"]),
            "publication_status": "published",
        },
    )
    bulk_index_chunks(
        [extra],
        knowledge_base_id=case.knowledge_base_id,
        settings=settings,
        write_alias=case.write_alias,
        physical_index=case.physical_index,
        read_alias=case.read_alias,
        dimensions=3,
        refresh="wait_for",
    )
    await _insert_reconciliation_due(settings, case, document_id)
    await _set_scan_complete(settings)
    worker_api = cast(Any, worker)
    original_renew = worker_api.renew_search_projection_reconciliation_lease
    renewals = 0

    async def expire_after_delete(*args: Any, **kwargs: Any) -> bool:
        nonlocal renewals
        result = await original_renew(*args, **kwargs)
        renewals += 1
        if renewals == 2:
            connection = args[0]
            await connection.execute(
                text(
                    "UPDATE search_projection_reconciliation SET worker_lease_expires_at=now()-interval '1 second' "
                    "WHERE document_id=:document"
                ),
                {"document": document_id},
            )
        return bool(result)

    monkeypatch.setattr(worker_api, "renew_search_projection_reconciliation_lease", expire_after_delete)
    assert await worker._process_search_projection_reconciliation_once(settings, lease_id="lost-owner") == 1
    async with connect(settings) as conn:
        state = dict(
            (
                await conn.execute(
                    text(
                        "SELECT status, worker_lease_id FROM search_projection_reconciliation "
                        "WHERE document_id=:document"
                    ),
                    {"document": document_id},
                )
            )
            .mappings()
            .one()
        )
    assert state["status"] == "running"
    assert state["worker_lease_id"] == "lost-owner"

    monkeypatch.setattr(worker_api, "renew_search_projection_reconciliation_lease", original_renew)
    assert await worker._process_search_projection_reconciliation_once(settings, lease_id="reclaimer") == 1
    observed = read_document_projection(
        knowledge_base_id=case.knowledge_base_id,
        document_id=document_id,
        limit=10,
        settings=settings,
        read_alias=case.read_alias,
    )
    assert {str(row["_source"]["chunk_id"]) for row in observed} == {chunk.id for chunk in chunks}
    async with connect(settings) as conn:
        status = await conn.execute(
            text("SELECT status FROM search_projection_reconciliation WHERE document_id=:document"),
            {"document": document_id},
        )
        assert status.scalar_one() == "ok"


@pytest.mark.asyncio
async def test_retention_is_bounded_and_preserves_noneligible_events_and_canonical_state(
    isolated_settings: Settings,
) -> None:
    settings = isolated_settings
    case = await _seed_case(settings)
    document_id = case.document_ids[0]
    event_ids = {name: str(uuid.uuid4()) for name in ("old_a", "old_b", "recent", "received", "running", "failed")}
    now = datetime.now(UTC)
    async with connect(settings) as conn:
        for name, event_id in event_ids.items():
            status = "completed" if name.startswith("old") or name == "recent" else name
            completed_at = now - timedelta(days=31) if name.startswith("old") else now
            await conn.execute(
                text(
                    "INSERT INTO search_projection_events(id, tenant_id, knowledge_base_id, document_id, event_kind, "
                    "dedupe_key, status, completed_at, next_attempt_at) VALUES (:id, :tenant, :kb, :document, "
                    "'document_access', :dedupe, :status, :completed, now())"
                ),
                {
                    "id": event_id,
                    "tenant": case.tenant_id,
                    "kb": case.knowledge_base_id,
                    "document": document_id,
                    "dedupe": f"auth003-retention-{name}",
                    "status": status,
                    "completed": completed_at if status == "completed" else None,
                },
            )
        before = dict(
            (
                await conn.execute(
                    text("SELECT content_hash, publication_status FROM chunks WHERE document_id=:document"),
                    {"document": document_id},
                )
            )
            .mappings()
            .one()
        )
    deleted_counts: list[int] = []
    for _ in range(3):
        async with connect(settings) as conn:
            deleted_counts.append(
                await cleanup_completed_search_projection_events(conn, retention_days=30, batch_size=1)
            )
    assert deleted_counts == [1, 1, 0]
    async with connect(settings) as conn:
        rows = [
            {**dict(row), "id": str(row["id"])}
            for row in (
                await conn.execute(
                    text("SELECT id, status FROM search_projection_events WHERE tenant_id=:tenant ORDER BY id"),
                    {"tenant": case.tenant_id},
                )
            ).mappings()
        ]
        after = dict(
            (
                await conn.execute(
                    text("SELECT content_hash, publication_status FROM chunks WHERE document_id=:document"),
                    {"document": document_id},
                )
            )
            .mappings()
            .one()
        )
    assert {row["id"] for row in rows} == {
        event_ids["recent"],
        event_ids["received"],
        event_ids["running"],
        event_ids["failed"],
    }
    assert {row["status"] for row in rows} == {"completed", "received", "running", "failed"}
    assert after == before
