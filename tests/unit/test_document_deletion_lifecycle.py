from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest
from botocore.exceptions import ClientError
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole
from wikipediarag.config import Settings
from wikipediarag.db import SCHEMA_SQL
from wikipediarag.repository import claim_next_job, get_document_public, mark_document_chunks_deleted
from wikipediarag.search_index import delete_document_chunks
from wikipediarag.storage import delete_objects
from wikipediarag.workspace_access import ResourceAccess, ResourceType


class _FakeConnectionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/api/v1/documents/doc:1",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


def test_document_lifecycle_schema_is_forward_only() -> None:
    assert "ALTER TABLE documents ADD COLUMN IF NOT EXISTS lifecycle_state" in SCHEMA_SQL
    assert "ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS lifecycle_state" in SCHEMA_SQL
    assert "deleted_by_user_id uuid NULL REFERENCES users(id)" in SCHEMA_SQL
    assert "CREATE INDEX IF NOT EXISTS ix_documents_purge" in SCHEMA_SQL


def test_public_document_reads_exclude_deleted_documents() -> None:
    public_sql = str(get_document_public.__code__.co_consts)

    assert "d.lifecycle_state = 'active'" in public_sql


def test_soft_delete_unpublishes_db_chunks_and_claims_only_due_purge_jobs() -> None:
    chunk_sql = str(mark_document_chunks_deleted.__code__.co_consts)
    claim_sql = str(claim_next_job.__code__.co_consts)

    assert "publication_status = 'deleted'" in chunk_sql
    assert "config ->> 'purge_after'" in claim_sql
    assert "(config ->> 'purge_after')::timestamptz <= now()" in claim_sql


def test_opensearch_document_delete_is_kb_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def delete_by_query(self, **kwargs: Any) -> dict[str, int]:
            calls.append(kwargs)
            return {"deleted": 3}

    monkeypatch.setattr("wikipediarag.search_index.get_client", lambda _settings: Client())

    deleted = delete_document_chunks(
        knowledge_base_id="kb",
        document_id="doc",
        read_alias="alias",
    )

    assert deleted == 3
    assert calls[0]["index"] == "alias"
    filters = calls[0]["body"]["query"]["bool"]["filter"]
    assert not any("tenant_id" in str(item) for item in filters)
    assert {"term": {"knowledge_base_id": "kb"}} in filters
    assert {"term": {"document_id": "doc"}} in filters


def test_storage_delete_objects_treats_missing_keys_as_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"Errors": [{"Key": "missing.json", "Code": "NoSuchKey"}]}

    monkeypatch.setattr("wikipediarag.storage._client", lambda _settings: Client())

    deleted = delete_objects(["existing.json", "missing.json"], Settings(minio_bucket="bucket"))

    assert deleted == 2
    assert calls[0]["Bucket"] == "bucket"


def test_storage_delete_objects_raises_safe_error_for_blocking_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def delete_objects(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Errors": [{"Key": "private/path.json", "Code": "AccessDenied", "Message": "private/path.json"}]}

    monkeypatch.setattr("wikipediarag.storage._client", lambda _settings: Client())

    with pytest.raises(ClientError) as exc_info:
        delete_objects(["private/path.json"], Settings(minio_bucket="bucket"))

    error = exc_info.value.response["Error"]
    assert error["Code"] == "AccessDenied"
    assert error["Message"] == "object deletion failed"


def test_storage_delete_objects_falls_back_for_legacy_minio_md5_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_keys: list[str] = []

    class Client:
        def delete_objects(self, **_kwargs: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "MissingContentMD5", "Message": "required"}}, "DeleteObjects")

        def delete_object(self, **kwargs: Any) -> dict[str, Any]:
            deleted_keys.append(str(kwargs["Key"]))
            return {}

    monkeypatch.setattr("wikipediarag.storage._client", lambda _settings: Client())

    deleted = delete_objects(["first.json", "second.json"], Settings(minio_bucket="bucket"))

    assert deleted == 2
    assert deleted_keys == ["first.json", "second.json"]


async def test_delete_document_endpoint_returns_safe_lifecycle_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = ActorContext(
        user_id="22222222-2222-4222-8222-222222222222",
        platform_role=PlatformRole.platform_admin,
        session_id="session",
        authentication_method=AuthenticationMethod.local,
        request_id="33333333-3333-4333-8333-333333333333",
        trace_id="trace",
    )
    events: list[tuple[str, dict[str, Any]]] = []

    class Result:
        def __init__(self, row: dict[str, Any]) -> None:
            self.row = row

        def mappings(self) -> Result:
            return self

        def first(self) -> dict[str, Any]:
            return self.row

    class Conn:
        async def execute(self, statement: object, _params: object = None) -> Result:
            if "SELECT active_index" in str(statement):
                return Result({"active_index": "kb-read-alias"})
            return Result(
                {
                    "tenant_id": "11111111-1111-4111-8111-111111111111",
                    "knowledge_base_id": "33333333-3333-4333-8333-333333333333",
                    "lifecycle_state": "active",
                }
            )

    class Context:
        async def __aenter__(self) -> Conn:
            return Conn()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Repository:
        def __init__(self, _conn: object) -> None:
            pass

        async def load_document(self, _document_id: str) -> ResourceAccess:
            return ResourceAccess(ResourceType.document, "doc:1", actor.user_id)

        async def authorize(self, **_kwargs: object) -> tuple[bool, bool, bool, bool]:
            return True, True, True, True

    async def require_actor(_request: Request) -> ActorContext:
        return actor

    async def get_lifecycle(_conn: object, tenant_id: str, document_id: str) -> dict[str, Any]:
        return {
            "id": document_id,
            "knowledge_base_id": "33333333-3333-4333-8333-333333333333",
            "lifecycle_state": "active",
        }

    async def require_role(*_args: object, **_kwargs: object) -> None:
        return None

    async def get_kb(_conn: object, _tenant_id: str, _kb_id: str) -> dict[str, str]:
        return {"active_index": "kb-read-alias"}

    async def soft_delete(_conn: object, **kwargs: Any) -> None:
        events.append(("soft_delete", kwargs))

    async def create_job(_conn: object, **kwargs: Any) -> str:
        events.append(("job", kwargs))
        return "44444444-4444-4444-8444-444444444444"

    async def audit(_conn: object, **kwargs: Any) -> None:
        events.append(("audit", kwargs))

    def delete_search(**kwargs: Any) -> int:
        events.append(("search_delete", kwargs))
        return 2

    monkeypatch.setattr(api_app, "get_settings", lambda: Settings(document_soft_delete_retention_days=30))
    monkeypatch.setattr(api_app, "connect", lambda: Context())
    monkeypatch.setattr(api_app, "_require_actor", require_actor)
    monkeypatch.setattr(api_app, "WorkspaceGrantRepository", Repository)
    monkeypatch.setattr(api_app, "soft_delete_document", soft_delete)
    monkeypatch.setattr(api_app, "create_document_deletion_job", create_job)
    monkeypatch.setattr(api_app, "_audit", audit)
    monkeypatch.setattr(api_app, "delete_document_chunks", delete_search)

    response = await api_app.delete_document("doc:1", _request())
    payload = response.model_dump(mode="json")

    assert payload["document_id"] == "doc:1"
    assert payload["job_id"] == "44444444-4444-4444-8444-444444444444"
    assert payload["lifecycle_state"] == "deleting"
    assert "object_key" not in str(payload)
    search_delete = [payload for event, payload in events if event == "search_delete"][0]
    assert search_delete["tenant_id"] == "11111111-1111-4111-8111-111111111111"
    assert search_delete["knowledge_base_id"] == "33333333-3333-4333-8333-333333333333"
    assert search_delete["document_id"] == "doc:1"
    assert search_delete["read_alias"] == "kb-read-alias"
