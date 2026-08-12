from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import UploadFile
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, KnowledgeBaseRole, PlatformRole, TenantRole
from wikipediarag.config import Settings
from wikipediarag.db import SCHEMA_SQL
from wikipediarag.ingestion import _document_access_for_ingestion, _source_document_access
from wikipediarag.schemas import SourceAccessPatch, SourceCreate
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


def test_document_access_trusts_source_sync_but_not_direct_upload_metadata() -> None:
    restricted = {"policy": "restricted", "user_ids": ["user:1"], "group_ids": []}

    assert _document_access_for_ingestion(
        document_id="doc:client-upload",
        source_metadata={"document_access": restricted},
    ) == {"policy": "kb", "user_ids": [], "group_ids": []}
    assert (
        _document_access_for_ingestion(
            document_id="src:connector-document",
            source_metadata={"document_access": restricted},
        )
        == restricted
    )


def test_source_document_access_prefers_manual_then_source_default_then_connector() -> None:
    manual_access, manual_origin = _source_document_access(
        document_metadata={"document_access": {"policy": "tenant"}},
        source_default={"policy": "kb"},
        existing_metadata={
            "document_access_origin": "manual",
            "document_access": {"policy": "restricted", "user_ids": ["u1"], "group_ids": []},
        },
    )
    source_access, source_origin = _source_document_access(
        document_metadata={"document_access": {"policy": "restricted", "user_ids": ["u2"], "group_ids": []}},
        source_default={"policy": "tenant"},
    )
    connector_access, connector_origin = _source_document_access(
        document_metadata={"document_access": {"policy": "restricted", "user_ids": ["u3"], "group_ids": []}},
        source_default=None,
    )

    assert manual_access == {"policy": "restricted", "user_ids": ["u1"], "group_ids": []}
    assert manual_origin == "manual"
    assert source_access == {"policy": "tenant", "user_ids": [], "group_ids": []}
    assert source_origin == "source_default"
    assert connector_access == {"policy": "restricted", "user_ids": ["u3"], "group_ids": []}
    assert connector_origin == "connector"


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


async def test_patch_source_access_applies_default_to_existing_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def require_role(_conn: object, **kwargs: Any) -> KnowledgeBaseRole:
        calls.append(("role", kwargs))
        return KnowledgeBaseRole.manager

    async def get_source(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"id": "source:1", "metadata": {}}

    async def update_default(*_args: object, **kwargs: Any) -> None:
        calls.append(("source_default", kwargs))

    async def list_refs(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [
            {"document_id": "doc:1", "document_version_id": "docv:1"},
            {"document_id": "doc:2", "document_version_id": "docv:2"},
        ]

    async def update_db(*_args: object, **kwargs: Any) -> None:
        calls.append(("document_access", kwargs))

    async def get_kb(*_args: object) -> dict[str, str]:
        return {"active_index": "read-kb"}

    async def audit(*_args: object, **kwargs: Any) -> None:
        calls.append(("audit", kwargs))

    def update_os(**kwargs: Any) -> int:
        calls.append(("opensearch", kwargs))
        return 1

    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_role)
    monkeypatch.setattr(api_app, "get_knowledge_source", get_source)
    monkeypatch.setattr(api_app, "update_knowledge_source_document_access_default", update_default)
    monkeypatch.setattr(api_app, "list_source_active_document_refs", list_refs)
    monkeypatch.setattr(api_app, "update_document_access_metadata", update_db)
    monkeypatch.setattr(api_app, "get_knowledge_base", get_kb)
    monkeypatch.setattr(api_app, "_audit", audit)
    monkeypatch.setattr(api_app, "update_document_access", update_os)

    response = await api_app.patch_source_access(
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
        SourceAccessPatch(policy="tenant"),
        _request(method="PATCH"),
    )

    assert response.updated_documents == 2
    assert response.document_access_default == {"policy": "tenant", "user_ids": [], "group_ids": []}
    assert [name for name, _payload in calls].count("document_access") == 2
    assert [name for name, _payload in calls].count("opensearch") == 2
    assert all(payload.get("origin") == "source_default" for name, payload in calls if name == "document_access")


async def test_access_groups_for_kb_manager_omit_members(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Rows:
        def mappings(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": "group:1",
                    "name": "Engineering",
                    "group_type": "LOCAL",
                    "external_id": None,
                    "member_user_ids": ["hidden"],
                }
            ]

    class _Conn:
        async def execute(self, *_args: object, **_kwargs: object) -> _Rows:
            return _Rows()

    class _Context:
        async def __aenter__(self) -> _Conn:
            return _Conn()

        async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

    async def require_actor(_request: Request) -> ActorContext:
        return _actor()

    async def require_role(*_args: object, **_kwargs: object) -> KnowledgeBaseRole:
        return KnowledgeBaseRole.manager

    monkeypatch.setattr(api_app, "connect", lambda: _Context())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "_require_kb_role", require_role)

    response = await api_app.list_access_groups("33333333-3333-4333-8333-333333333333", _request(method="GET"))

    payload = response[0].model_dump(mode="json")
    assert payload == {"id": "group:1", "name": "Engineering", "group_type": "LOCAL", "external_id": None}
    assert "member_user_ids" not in payload


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

    async def create_records(_conn: object, **kwargs: Any) -> tuple[uuid.UUID, str]:
        events.append(("records", kwargs))
        return uuid.UUID("66666666-6666-4666-8666-666666666666"), "received"

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
