from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole, TenantRole
from wikipediarag.config import Settings
from wikipediarag.repository import create_document_upload_records
from wikipediarag.schemas import UploadBatchCreate, UploadBatchItemCreate


class _FakeConnectionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _CaptureConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any] | None] = []

    async def execute(self, statement: object, params: dict[str, Any] | None = None) -> object:
        self.statements.append(str(statement))
        self.params.append(params)
        return object()


def _request(*, method: str = "POST", path: str = "/api/v1/uploads/batches") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


def _actor() -> ActorContext:
    return ActorContext(
        user_id="22222222-2222-4222-8222-222222222222",
        platform_role=PlatformRole.platform_admin,
        active_tenant_id="11111111-1111-4111-8111-111111111111",
        tenant_role=TenantRole.tenant_admin,
        session_id="session",
        authentication_method=AuthenticationMethod.local,
        request_id="33333333-3333-4333-8333-333333333333",
        trace_id="trace",
    )


async def test_create_upload_batch_endpoint_creates_sessions_without_object_key_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _actor()
    batch_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    created_sessions: list[dict[str, Any]] = []

    async def require_actor(_request: Request) -> ActorContext:
        return actor

    async def require_role(*_args: object, **_kwargs: object) -> None:
        return None

    async def get_kb(_conn: object, _tenant_id: str, kb_id: str) -> dict[str, str]:
        return {"id": kb_id}

    async def create_batch(_conn: object, **kwargs: Any) -> uuid.UUID:
        assert kwargs["total_items"] == 2
        return batch_id

    async def create_session(_conn: object, **kwargs: Any) -> tuple[uuid.UUID, datetime]:
        created_sessions.append(kwargs)
        return uuid.UUID(f"55555555-5555-4555-8555-55555555555{len(created_sessions)}"), datetime.now(UTC)

    monkeypatch.setattr(api_app, "get_settings", lambda: Settings(upload_session_ttl_seconds=300))
    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_role)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "create_upload_batch", create_batch)
    monkeypatch.setattr(api_app, "create_upload_session", create_session)
    monkeypatch.setattr(
        api_app, "create_presigned_put_url", lambda object_key, **_kwargs: f"https://upload/{object_key}"
    )

    payload = UploadBatchCreate(
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        items=[
            UploadBatchItemCreate(
                filename="a.txt",
                content_type="text/plain",
                size_bytes=3,
                checksum_sha256="a" * 64,
            ),
            UploadBatchItemCreate(
                filename="b.txt",
                content_type="text/plain",
                size_bytes=4,
                checksum_sha256="b" * 64,
            ),
        ],
    )

    response = await api_app.create_upload_batch_endpoint(payload, _request())
    data = response.model_dump(mode="json")

    assert data["batch_id"] == str(batch_id)
    assert data["total_items"] == 2
    assert len(data["items"]) == 2
    assert all(session["batch_id"] == str(batch_id) for session in created_sessions)
    assert "object_key" not in str(data)
    assert all(
        "/batches/44444444-4444-4444-8444-444444444444/" in session["object_key"] for session in created_sessions
    )


async def test_document_upload_completion_reuses_existing_batch_id() -> None:
    conn = _CaptureConnection()
    batch_id = "44444444-4444-4444-8444-444444444444"

    await create_document_upload_records(
        cast(AsyncConnection, conn),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        upload_session={
            "id": "55555555-5555-4555-8555-555555555555",
            "batch_id": batch_id,
            "filename": "a.txt",
            "object_key": "uploads/server-owned/key",
            "parser_profile": "standard",
            "content_type": "text/plain",
            "size_bytes": 3,
        },
        document_id="doc:abc",
        document_version_id="docv:abc",
        content_hash="a" * 64,
        metadata={},
    )

    assert not any("INSERT INTO upload_batches" in statement for statement in conn.statements)
    upload_session_updates = [
        params
        for statement, params in zip(conn.statements, conn.params, strict=True)
        if "UPDATE upload_sessions" in statement
    ]
    assert upload_session_updates
    assert upload_session_updates[0] is not None
    assert upload_session_updates[0]["batch_id"] == batch_id
