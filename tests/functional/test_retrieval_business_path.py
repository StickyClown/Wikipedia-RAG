"""Small, live business path for retrieval; it never permits an external model."""

# ruff: noqa: ASYNC212

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from wikipediarag.auth_service import hash_password
from wikipediarag.config import Settings
from wikipediarag.db import connect
from wikipediarag.repository import (
    fetch_current_retrieval_chunks,
    load_current_document_projection,
    load_index_version_by_read_alias,
)
from wikipediarag.search_index import bm25_search, bulk_index_chunks, dense_search

API = os.getenv("WIKIPEDIARAG_FUNCTIONAL_API", "http://localhost:8000").rstrip("/")


def _functional_settings() -> Settings:
    return Settings(
        database_url=os.getenv(
            "WIKIPEDIARAG_FUNCTIONAL_DATABASE_URL",
            "postgresql+asyncpg://rag:change-me-local-only@localhost:5432/rag",
        ),
        opensearch_url=os.getenv("WIKIPEDIARAG_FUNCTIONAL_OPENSEARCH_URL", "http://localhost:9200"),
    )


def _blocked(reason: str) -> None:
    message = f"BLOCKED: {reason}"
    if os.getenv("WIKIPEDIARAG_REQUIRE_FUNCTIONAL") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _wait_ready(client: httpx.Client, timeout_seconds: int = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{API}/ready")
            last_status = f"HTTP {response.status_code}"
            if response.status_code == 200:
                payload = dict(response.json())
                if payload.get("status") == "ok":
                    return payload
        except httpx.HTTPError as exc:
            last_status = type(exc).__name__
        time.sleep(0.5)
    _blocked(f"local stack did not become ready within {timeout_seconds}s: {last_status}")
    raise AssertionError("unreachable")


def _login(client: httpx.Client, *, username: str | None = None, password: str | None = None) -> dict[str, Any]:
    response = client.post(
        f"{API}/api/v1/auth/local/login",
        json={
            "username": username or os.getenv("WIKIPEDIARAG_FUNCTIONAL_ADMIN_USERNAME", "admin"),
            "password": password or os.getenv("WIKIPEDIARAG_FUNCTIONAL_ADMIN_PASSWORD", "admin"),
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"local administrator login failed: HTTP {response.status_code}")
    session = client.get(f"{API}/api/v1/auth/session").json()
    csrf = session.get("csrf_token")
    if csrf:
        client.headers["X-CSRF-Token"] = str(csrf)
    return dict(session)


async def _create_editor(*, tenant_id: str, kb_id: str, suffix: str) -> tuple[str, str, str]:
    user_id = str(uuid.uuid4())
    username = f"functional-editor-{suffix}"
    password = f"functional-password-{suffix}"
    async with connect(_functional_settings()) as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users(id, email, username, display_name, platform_role, password_hash)
                VALUES (:id, :email, :username, :display_name, 'USER', :password_hash)
                """
            ),
            {
                "id": user_id,
                "email": f"{username}@example.test",
                "username": username,
                "display_name": username,
                "password_hash": hash_password(password),
            },
        )
        await conn.execute(
            text("INSERT INTO tenant_memberships(tenant_id, user_id, role) VALUES (:tenant_id, :user_id, 'MEMBER')"),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                """
                INSERT INTO knowledge_base_grants(id, tenant_id, knowledge_base_id, subject_type, subject_id, role)
                VALUES (:id, :tenant_id, :kb_id, 'USER', :user_id, 'EDITOR')
                """
            ),
            {"id": str(uuid.uuid4()), "tenant_id": tenant_id, "kb_id": kb_id, "user_id": user_id},
        )
    return user_id, username, password


async def _restrict_document_without_projection(*, tenant_id: str, kb_id: str, document_id: str) -> None:
    payload = json.dumps({"document_access": {"policy": "restricted", "user_ids": [], "group_ids": []}})
    async with connect(_functional_settings()) as conn:
        for table in ("documents", "chunks"):
            await conn.execute(
                text(
                    f"UPDATE {table} SET metadata = metadata || CAST(:payload AS jsonb) "  # noqa: S608 - fixed table names.
                    "WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id AND "
                    f"{'id' if table == 'documents' else 'document_id'} = :document_id"  # noqa: S608
                ),
                {"payload": payload, "tenant_id": tenant_id, "kb_id": kb_id, "document_id": document_id},
            )


async def _delete_editor(*, user_id: str, kb_id: str) -> None:
    async with connect(_functional_settings()) as conn:
        await conn.execute(text("DELETE FROM audit_events WHERE actor_user_id = :user_id"), {"user_id": user_id})
        await conn.execute(text("DELETE FROM auth_sessions WHERE user_id = :user_id"), {"user_id": user_id})
        await conn.execute(
            text(
                "DELETE FROM retrieval_events WHERE query_run_id IN "
                "(SELECT id FROM query_runs WHERE user_id = :user_id)"
            ),
            {"user_id": user_id},
        )
        await conn.execute(
            text("DELETE FROM agent_runs WHERE query_run_id IN (SELECT id FROM query_runs WHERE user_id = :user_id)"),
            {"user_id": user_id},
        )
        await conn.execute(text("DELETE FROM query_runs WHERE user_id = :user_id"), {"user_id": user_id})
        await conn.execute(
            text(
                "DELETE FROM knowledge_base_grants WHERE knowledge_base_id = :kb_id "
                "AND subject_type = 'USER' AND subject_id = :user_id"
            ),
            {"kb_id": kb_id, "user_id": user_id},
        )
        await conn.execute(text("DELETE FROM tenant_memberships WHERE user_id = :user_id"), {"user_id": user_id})
        await conn.execute(text("DELETE FROM auth_identities WHERE user_id = :user_id"), {"user_id": user_id})
        await conn.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})


def _upload(client: httpx.Client, kb_id: str, marker: str) -> dict[str, Any]:
    content = f"Deterministic retrieval fixture. Marker: {marker}.".encode()
    session = client.post(
        f"{API}/api/v1/uploads/sessions",
        json={
            "filename": f"functional-{marker}.txt",
            "content_type": "text/plain",
            "size_bytes": len(content),
            "checksum_sha256": hashlib.sha256(content).hexdigest(),
            "knowledge_base_id": kb_id,
            "parser_profile": "standard",
            "metadata": {"functional_retrieval": marker},
        },
    )
    session.raise_for_status()
    created = dict(session.json())
    put = client.put(created["upload_url"], content=content, headers=created.get("required_headers") or {})
    put.raise_for_status()
    complete = client.post(
        f"{API}/api/v1/uploads/sessions/{created['upload_session_id']}:complete", json={"metadata": {}}
    )
    complete.raise_for_status()
    return dict(complete.json())


def _wait_job(client: httpx.Client, job_id: str, timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"{API}/api/v1/ingestion-jobs/{job_id}")
        response.raise_for_status()
        last = dict(response.json())
        if last.get("status") == "completed":
            return
        if last.get("status") in {"failed", "cancelled"}:
            raise AssertionError(f"ingestion terminal failure: {last}")
        # This is a bounded event poll, not an arbitrary test delay.
        time.sleep(0.5)
    raise AssertionError(f"ingestion did not reach a terminal event: {last}")


async def _wait_publication_projections(kb_ids: list[str], timeout_seconds: int = 60) -> None:
    """Wait for only this test's durable projections before deleting its KBs."""
    deadline = time.monotonic() + timeout_seconds
    last: list[dict[str, Any]] = []
    async with connect(_functional_settings()) as conn:
        while time.monotonic() < deadline:
            result = await conn.execute(
                text(
                    "SELECT status, error_code FROM search_projection_events "
                    "WHERE knowledge_base_id = ANY(CAST(:kb_ids AS uuid[]))"
                ),
                {"kb_ids": kb_ids},
            )
            last = [dict(row) for row in result.mappings()]
            if last and all(row["status"] == "completed" for row in last):
                return
            if any(row["status"] == "failed" for row in last):
                raise AssertionError(f"search projection terminal failure: {last}")
            await asyncio.sleep(0.5)
    raise AssertionError(f"search projections did not complete: {last}")


async def _delete_test_projection_events(kb_ids: list[str]) -> None:
    async with connect(_functional_settings()) as conn:
        await conn.execute(
            text("DELETE FROM search_projection_events WHERE knowledge_base_id = ANY(CAST(:kb_ids AS uuid[]))"),
            {"kb_ids": kb_ids},
        )


async def _delete_test_query_history(*, tenant_id: str, kb_id: str) -> None:
    """Keep the test-owned KB eligible for the public bounded delete endpoint."""
    async with connect(_functional_settings()) as conn:
        await conn.execute(
            text(
                "DELETE FROM retrieval_events WHERE query_run_id IN "
                "(SELECT id FROM query_runs WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id)"
            ),
            {"tenant_id": tenant_id, "kb_id": kb_id},
        )
        await conn.execute(
            text(
                "DELETE FROM agent_runs WHERE query_run_id IN "
                "(SELECT id FROM query_runs WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id)"
            ),
            {"tenant_id": tenant_id, "kb_id": kb_id},
        )
        await conn.execute(
            text("DELETE FROM query_runs WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id"),
            {"tenant_id": tenant_id, "kb_id": kb_id},
        )


async def _inject_corrupted_staged_projection(
    *, tenant_id: str, kb_id: str, document_id: str
) -> tuple[str, list[float], str]:
    """Test-only failure window: DB staged while OpenSearch claims published."""
    async with connect(_functional_settings()) as conn:
        version_id, chunks = await load_current_document_projection(
            conn, tenant_id=tenant_id, knowledge_base_id=kb_id, document_id=document_id
        )
        assert version_id and len(chunks) == 1
        kb = await conn.execute(text("SELECT active_index FROM knowledge_bases WHERE id = :id"), {"id": kb_id})
        active_index = str(kb.scalar_one())
        index_version = await load_index_version_by_read_alias(
            conn, tenant_id=tenant_id, knowledge_base_id=kb_id, read_alias=active_index
        )
        assert index_version is not None
        await conn.execute(
            text(
                "UPDATE chunks SET publication_status = 'staged' WHERE id = :chunk_id "
                "AND tenant_id = :tenant_id AND knowledge_base_id = :kb_id"
            ),
            {"chunk_id": chunks[0].id, "tenant_id": tenant_id, "kb_id": kb_id},
        )
    # The captured canonical chunk deliberately keeps its formerly-published
    # metadata.  This is the only test-only bypass of normal publication.
    bulk_index_chunks(
        chunks,
        knowledge_base_id=kb_id,
        settings=_functional_settings(),
        write_alias=str(index_version["write_alias"]),
        physical_index=str(index_version["physical_index"]),
        read_alias=str(index_version["read_alias"]),
        dimensions=int(index_version.get("embedding_dimensions") or 0) or None,
        refresh="wait_for",
    )
    return chunks[0].id, list(chunks[0].embedding), str(index_version["read_alias"])


@pytest.mark.asyncio
async def test_published_upload_is_retrievable_with_correct_kb_provenance() -> None:
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            _wait_ready(client)
            models = client.get("http://localhost:8081/v1/models")
            if models.status_code != 200 or "mock" not in models.text.casefold():
                _blocked("Model Gateway is not configured with the local mock provider")
            admin_session = _login(client)
            tenant_id = str(admin_session["active_tenant_id"])
            suffix = uuid.uuid4().hex[:12]
            kb_ids: list[str] = []
            editor: tuple[str, str, str] | None = None
            try:
                for label in ("first", "second"):
                    response = client.post(
                        f"{API}/api/v1/knowledge-bases", json={"name": f"functional retrieval {label} {suffix}"}
                    )
                    response.raise_for_status()
                    kb_ids.append(str(response.json()["id"]))
                markers = [f"needle-{suffix}-one", f"needle-{suffix}-two"]
                uploads = [_upload(client, kb_id, marker) for kb_id, marker in zip(kb_ids, markers, strict=True)]
                for upload in uploads:
                    _wait_job(client, str(upload["job_id"]))
                await _wait_publication_projections(kb_ids)
                first = client.post(
                    f"{API}/api/v1/search", json={"query": markers[0], "knowledge_base_ids": [kb_ids[0]], "limit": 10}
                )
                first.raise_for_status()
                first_results = list(first.json().get("results") or [])
                assert any(
                    item.get("knowledge_base_id") == kb_ids[0] and markers[0] in str(item) for item in first_results
                )
                assert not any(markers[1] in str(item) for item in first_results)
                required = {
                    "knowledge_base_id",
                    "document_id",
                    "document_version_id",
                    "chunk_id",
                    "locator",
                    "source_url",
                }
                assert required <= set(first_results[0])
                multi = client.post(
                    f"{API}/api/v1/search",
                    json={"query": f"{markers[0]} {markers[1]}", "knowledge_base_ids": kb_ids, "limit": 20},
                )
                multi.raise_for_status()
                multi_results = list(multi.json().get("results") or [])
                assert {item.get("knowledge_base_id") for item in multi_results if "needle-" in str(item)} >= set(
                    kb_ids
                )
                editor = await _create_editor(tenant_id=tenant_id, kb_id=kb_ids[0], suffix=suffix)
                with httpx.Client(timeout=60, follow_redirects=True) as editor_client:
                    _login(editor_client, username=editor[1], password=editor[2])
                    allowed = editor_client.post(
                        f"{API}/api/v1/search",
                        json={"query": markers[0], "knowledge_base_ids": [kb_ids[0]], "limit": 10},
                    )
                    allowed.raise_for_status()
                    allowed_payload = allowed.json()
                    assert markers[0] in json.dumps(allowed_payload)
                    denied = editor_client.post(
                        f"{API}/api/v1/search",
                        json={"query": markers[1], "knowledge_base_ids": [kb_ids[1]], "limit": 10},
                    )
                    assert markers[1] not in json.dumps(denied.json())

                    document_id = str(first_results[0]["document_id"])
                    # The preceding allowed request populated any normal cache entry.  Do not enqueue an
                    # OpenSearch projection here: this is the deliberate stale-projection failure window.
                    await _restrict_document_without_projection(
                        tenant_id=tenant_id,
                        kb_id=kb_ids[0],
                        document_id=document_id,
                    )
                    stale = editor_client.post(
                        f"{API}/api/v1/search",
                        json={"query": markers[0], "knowledge_base_ids": [kb_ids[0]], "limit": 10},
                    )
                    stale.raise_for_status()
                    assert markers[0] not in json.dumps(stale.json())
                    debug = editor_client.post(
                        f"{API}/api/v1/search:debug",
                        json={"message": markers[0], "knowledge_base_ids": [kb_ids[0]], "top_k": 10},
                    )
                    debug.raise_for_status()
                    assert debug.json().get("evidence") == []
                await _assert_corrupted_staged_projection_is_never_exposed(
                    client=client, tenant_id=tenant_id, kb_ids=kb_ids, suffix=suffix
                )
            finally:
                if editor is not None:
                    await _delete_editor(user_id=editor[0], kb_id=kb_ids[0])
                if kb_ids:
                    await _delete_test_projection_events(kb_ids)
                    for kb_id in kb_ids:
                        await _delete_test_query_history(tenant_id=tenant_id, kb_id=kb_id)
                for kb_id in reversed(kb_ids):
                    client.delete(f"{API}/api/v1/knowledge-bases/{kb_id}").raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        _blocked(str(exc))


async def _assert_corrupted_staged_projection_is_never_exposed(
    *, client: httpx.Client, tenant_id: str, kb_ids: list[str], suffix: str
) -> None:
    """AUTH-003/ING-003: every read path confirms current PostgreSQL state."""
    marker = f"corrupt-staged-{suffix}"
    created = client.post(f"{API}/api/v1/knowledge-bases", json={"name": f"corrupt staged {suffix}"})
    created.raise_for_status()
    kb_id = str(created.json()["id"])
    kb_ids.append(kb_id)
    upload = _upload(client, kb_id, marker)
    _wait_job(client, str(upload["job_id"]))
    await _wait_publication_projections([kb_id])
    good = client.post(f"{API}/api/v1/search", json={"query": marker, "knowledge_base_ids": [kb_id], "limit": 10})
    good.raise_for_status()
    document_id = str(good.json()["results"][0]["document_id"])
    chunk_id, vector, read_alias = await _inject_corrupted_staged_projection(
        tenant_id=tenant_id, kb_id=kb_id, document_id=document_id
    )
    # The index is deliberately corrupt and both real candidate paths rank it.
    bm25 = bm25_search(
        marker,
        knowledge_base_id=kb_id,
        top_k=10,
        settings=_functional_settings(),
        read_alias=read_alias,
    )
    dense = dense_search(
        vector,
        knowledge_base_id=kb_id,
        top_k=10,
        settings=_functional_settings(),
        read_alias=read_alias,
    )
    assert chunk_id in {str(item["chunk_id"]) for item in bm25}
    assert chunk_id in {str(item["chunk_id"]) for item in dense}
    async with connect(_functional_settings()) as conn:
        confirmed = await fetch_current_retrieval_chunks(
            conn, knowledge_base_id=kb_id, chunk_ids=[chunk_id]
        )
    assert confirmed == {}
    public = client.post(f"{API}/api/v1/search", json={"query": marker, "knowledge_base_ids": [kb_id], "limit": 10})
    public.raise_for_status()
    assert not any(marker in json.dumps(item) for item in public.json().get("results") or [])
    debug = client.post(
        f"{API}/api/v1/search:debug", json={"message": marker, "knowledge_base_ids": [kb_id], "top_k": 10}
    )
    debug.raise_for_status()
    assert debug.json().get("evidence") == []
    await _delete_test_query_history(tenant_id=tenant_id, kb_id=kb_id)
