from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import UploadFile
from starlette.requests import Request

import wikipediarag.api_app as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole, TenantRole
from wikipediarag.config import Settings
from wikipediarag.db import SCHEMA_SQL
from wikipediarag.schemas import SourceCreate
from wikipediarag.search_index import delete_document_version_chunks
from wikipediarag.source_connectors import (
    ConnectorError,
    DocSmartMockConnector,
    LocalFolderConnector,
    SundukMockConnector,
    connector_http_options,
)


class _FakeConnectionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


def _request(*, method: str = "POST", path: str = "/api/v1/knowledge-bases/kb/sources") -> Request:
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


def test_external_source_schema_is_forward_only() -> None:
    assert "ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS encrypted_credentials" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS source_sync_runs" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS source_document_states" in SCHEMA_SQL
    assert "tombstone_version text NULL" in SCHEMA_SQL


def test_connector_http_options_keep_tls_verification_enabled() -> None:
    options = connector_http_options(
        {
            "ca_bundle_path": "/opt/corporate-ca/root.crt",
            "mtls_cert_path": "/run/certs/client.crt",
            "mtls_key_path": "/run/certs/client.key",
        }
    )

    assert options["verify"] == "/opt/corporate-ca/root.crt"
    assert options["cert"] == ("/run/certs/client.crt", "/run/certs/client.key")
    assert options["verify"] is not False

    with pytest.raises(ConnectorError, match="mTLS requires both"):
        connector_http_options({"mtls_cert_path": "/run/certs/client.crt"})


def test_opensearch_delete_by_document_version_is_tenant_and_kb_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def delete_by_query(self, **kwargs: Any) -> dict[str, int]:
            calls.append(kwargs)
            return {"deleted": 2}

    monkeypatch.setattr("wikipediarag.search_index.get_client", lambda _settings: Client())

    deleted = delete_document_version_chunks(
        tenant_id="tenant",
        knowledge_base_id="kb",
        document_version_id="docv:old",
        read_alias="alias",
    )

    assert deleted == 2
    assert calls[0]["index"] == "alias"
    filters = calls[0]["body"]["query"]["bool"]["filter"]
    assert {"term": {"tenant_id": "tenant"}} in filters
    assert {"term": {"knowledge_base_id": "kb"}} in filters
    assert {"term": {"document_version_id": "docv:old"}} in filters


@pytest.mark.asyncio
async def test_local_folder_connector_reports_changes_and_full_sync_tombstones(tmp_path: Any) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nhello", encoding="utf-8")

    connector = LocalFolderConnector({"root_path": str(root)}, {})
    payload = await connector.sync(mode="full", cursor={}, known_external_ids={"a.md", "deleted.md"})

    assert [document.external_id for document in payload.documents] == ["a.md"]
    assert [tombstone.external_id for tombstone in payload.tombstones] == ["deleted.md"]


@pytest.mark.asyncio
async def test_corporate_mock_connectors_freeze_contracts() -> None:
    sunduk = await SundukMockConnector({}, {}).sync(mode="full", cursor={}, known_external_ids=set())
    docsmart = DocSmartMockConnector({}, {})
    docsmart_payload = await docsmart.sync(mode="full", cursor={}, known_external_ids=set())
    search_payload = await docsmart.search("резервное копирование", {"department": "IT"}, 20)

    assert sunduk.documents[0].external_id == "123"
    assert docsmart_payload.documents[0].external_id == "456"
    assert search_payload["results"][0]["metadata"] == {"department": "IT"}


async def test_source_create_encrypts_credentials_and_returns_safe_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _actor()
    source_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    created: dict[str, Any] = {}

    async def require_actor(_request: Request) -> ActorContext:
        return actor

    async def require_role(*_args: object, **_kwargs: object) -> None:
        return None

    async def get_kb(_conn: object, _tenant_id: str, _kb_id: str) -> dict[str, str]:
        return {"id": _kb_id}

    async def create_source_record(_conn: object, **kwargs: Any) -> uuid.UUID:
        created.update(kwargs)
        return source_id

    async def get_source_record(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {
            "id": str(source_id),
            "knowledge_base_id": "33333333-3333-4333-8333-333333333333",
            "kind": "gitlab_self_managed",
            "name": "GitLab",
            "status": "active",
            "config": {"base_url": "https://gitlab.local"},
            "metadata": {},
            "refresh_interval_seconds": 3600,
            "last_sync_run_id": None,
            "last_sync_status": None,
            "last_synced_at": None,
            "next_sync_at": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

    async def audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_role)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "create_knowledge_source", create_source_record)
    monkeypatch.setattr(api_app, "get_knowledge_source", get_source_record)
    monkeypatch.setattr(api_app, "encrypt_server_tokens", lambda _settings, payload: {"ciphertext": "encrypted"})
    monkeypatch.setattr(api_app, "_audit", audit)

    response = await api_app.create_source(
        "33333333-3333-4333-8333-333333333333",
        SourceCreate(
            kind="gitlab_self_managed",
            name="GitLab",
            config={"base_url": "https://gitlab.local"},
            credentials={"token": "raw-secret"},
            refresh_interval_seconds=3600,
        ),
        _request(),
    )

    assert created["encrypted_credentials"] == {"ciphertext": "encrypted"}
    assert "raw-secret" not in str(response.model_dump(mode="json"))
    assert "encrypted_credentials" not in str(response.model_dump(mode="json"))


async def test_multipart_upload_reuses_upload_records(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _actor()
    events: list[tuple[str, dict[str, Any]]] = []
    session_id = uuid.UUID("55555555-5555-4555-8555-555555555555")

    async def require_actor(_request: Request) -> ActorContext:
        return actor

    async def require_role(*_args: object, **_kwargs: object) -> None:
        return None

    async def get_kb(_conn: object, _tenant_id: str, _kb_id: str) -> dict[str, str]:
        return {"id": _kb_id}

    async def create_session(_conn: object, **kwargs: Any) -> tuple[uuid.UUID, datetime]:
        events.append(("session", kwargs))
        return session_id, datetime.now(UTC)

    async def get_session(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {
            "id": str(session_id),
            "filename": "api.md",
            "object_key": "uploads/server/key",
            "parser_profile": "standard",
            "content_type": "text/markdown",
            "size_bytes": 8,
            "checksum_sha256": "checksum",
        }

    async def create_records(_conn: object, **kwargs: Any) -> uuid.UUID:
        events.append(("records", kwargs))
        return uuid.UUID("66666666-6666-4666-8666-666666666666")

    async def audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(api_app, "get_settings", lambda: Settings(upload_max_bytes=1024))
    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_role)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "put_bytes", lambda *_args, **_kwargs: "s3://bucket/key")
    monkeypatch.setattr(api_app, "create_upload_session", create_session)
    monkeypatch.setattr(api_app, "get_upload_session", get_session)
    monkeypatch.setattr(api_app, "create_document_upload_records", create_records)
    monkeypatch.setattr(api_app, "_audit", audit)

    upload = UploadFile(filename="api.md", file=io.BytesIO(b"# API\nok"), headers=None)
    response = await api_app.upload_document_multipart(
        "33333333-3333-4333-8333-333333333333",
        _request(path="/api/v1/knowledge-bases/kb/documents"),
        upload,
    )

    assert response.status == "received"
    assert [event for event, _payload in events] == ["session", "records"]
    assert events[0][1]["metadata"] == {"api_multipart_upload": True}
