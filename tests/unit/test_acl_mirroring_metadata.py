from __future__ import annotations

from typing import Any

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, GrantSubjectType, KnowledgeBaseRole, PlatformRole
from wikipediarag.db import SCHEMA_SQL


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _GroupConnection:
    async def execute(self, *_args: object, **_kwargs: object) -> _Result:
        return _Result(
            {
                "id": "group-id",
                "tenant_id": "tenant",
                "group_type": "OIDC",
                "name": "/engineering/rag",
                "external_id": "/engineering/rag",
            }
        )


def test_kb_grants_have_acl_metadata_column() -> None:
    assert (
        "ALTER TABLE knowledge_base_grants ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'" in SCHEMA_SQL
    )


def test_kb_grant_list_includes_acl_metadata() -> None:
    list_sql = str(api_app.list_kb_grants.__code__.co_consts)

    assert "metadata" in list_sql


async def test_oidc_group_grant_metadata_records_external_group_path() -> None:
    actor = ActorContext(
        user_id="user-id",
        platform_role=PlatformRole.platform_admin,
        active_tenant_id="tenant",
        tenant_role=None,
        session_id="session-id",
        authentication_method=AuthenticationMethod.local,
        request_id="request-id",
        trace_id="trace-id",
    )

    metadata = await api_app._kb_grant_acl_metadata(
        _GroupConnection(),
        tenant_id="tenant",
        subject_type=GrantSubjectType.group,
        subject_id="group-id",
        role=KnowledgeBaseRole.viewer,
        actor=actor,
    )

    assert metadata["schema_version"] == "kb_grant_acl_metadata_v1"
    assert metadata["acl_snapshot"] == {
        "scope": "knowledge_base",
        "subject_type": "GROUP",
        "subject_id": "group-id",
        "role": "VIEWER",
        "source": "knowledge_base_grants",
    }
    assert metadata["acl_sync"]["source"] == "oidc_group_grant"
    assert metadata["acl_sync"]["status"] == "in_sync"
    assert metadata["acl_sync"]["external_group_path"] == "/engineering/rag"
    assert metadata["updated_by_user_id"] == "user-id"
