from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole
from wikipediarag.config import Settings
from wikipediarag.repository import DocumentVersionLifecycleError, create_document_upload_records
from wikipediarag.schemas import UploadBatchCreate, UploadBatchItemCreate


class _FakeConnectionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _FakeResult:
    def __init__(self, first: dict[str, Any] | None = None) -> None:
        self._first = first

    def mappings(self) -> _FakeResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._first


class _CaptureConnection:
    def __init__(self, existing_version: dict[str, Any] | None = None) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any] | None] = []
        self.existing_version = existing_version

    async def execute(self, statement: object, params: dict[str, Any] | None = None) -> object:
        self.statements.append(str(statement))
        self.params.append(params)
        if "FROM document_versions" in str(statement):
            return _FakeResult(self.existing_version)
        return _FakeResult()


async def _awaitable(value: str | None) -> str:
    return str(value)


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
    monkeypatch.setattr(
        api_app, "_require_workspace_kb_write", lambda *_args: _awaitable("11111111-1111-4111-8111-111111111111")
    )
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

    _job_id, status = await create_document_upload_records(
        cast(AsyncConnection, conn),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        upload_session={
            "id": "55555555-5555-4555-8555-555555555555",
            "batch_id": batch_id,
            "owner_user_id": "66666666-6666-4666-8666-666666666666",
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
    document_inserts = [
        params
        for statement, params in zip(conn.statements, conn.params, strict=True)
        if "INSERT INTO documents" in statement
    ]
    assert document_inserts[0] is not None
    assert document_inserts[0]["owner_user_id"] == "66666666-6666-4666-8666-666666666666"
    assert status == "received"


async def test_published_document_upload_is_completed_without_version_reset() -> None:
    conn = _CaptureConnection(
        existing_version={
            "status": "published",
            "content_hash": "a" * 64,
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "knowledge_base_id": "33333333-3333-4333-8333-333333333333",
        }
    )

    _job_id, status = await create_document_upload_records(
        cast(AsyncConnection, conn),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        upload_session={
            "id": "55555555-5555-4555-8555-555555555555",
            "batch_id": "44444444-4444-4444-8444-444444444444",
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

    assert status == "completed"
    assert not any("SET status = 'received'" in statement for statement in conn.statements)
    assert any("'deduplicated'" in statement for statement in conn.statements)


@pytest.mark.parametrize(
    ("version_status", "expected_code"),
    [("parsing", "DOCUMENT_VERSION_IN_PROGRESS"), ("failed", "DOCUMENT_VERSION_REPROCESS_REQUIRED")],
)
async def test_existing_non_published_document_upload_requires_explicit_reprocess(
    version_status: str,
    expected_code: str,
) -> None:
    conn = _CaptureConnection(
        existing_version={
            "status": version_status,
            "content_hash": "a" * 64,
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "knowledge_base_id": "33333333-3333-4333-8333-333333333333",
        }
    )

    with pytest.raises(DocumentVersionLifecycleError) as error:
        await create_document_upload_records(
            cast(AsyncConnection, conn),
            tenant_id="11111111-1111-4111-8111-111111111111",
            knowledge_base_id="33333333-3333-4333-8333-333333333333",
            upload_session={
                "id": "55555555-5555-4555-8555-555555555555",
                "batch_id": "44444444-4444-4444-8444-444444444444",
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

    assert error.value.code == expected_code
    assert len(conn.statements) == 1
