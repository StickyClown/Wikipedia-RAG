from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import HTTPException

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import KnowledgeBaseRole
from wikipediarag.document_access import DocumentAccessScope
from wikipediarag.schemas import FilterExpression, SearchFilters, SearchRequest


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _Connection:
    def __init__(self, source_rows: dict[str, dict[str, Any]]) -> None:
        self.source_rows = source_rows
        self.calls: list[dict[str, Any]] = []

    async def execute(self, _statement: object, parameters: dict[str, Any]) -> _Result:
        self.calls.append(parameters)
        return _Result(self.source_rows.get(str(parameters["id"])))


def _scope() -> dict[str, DocumentAccessScope]:
    return {"kb-a": DocumentAccessScope(user_id="user-a", kb_role=KnowledgeBaseRole.viewer)}


@pytest.mark.asyncio
async def test_search_filter_rejects_unauthorized_authority_fields_before_retrieval() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await api_app._authorize_search_identity_filters(
            cast(Any, object()),
            tenant_id="tenant-a",
            kb_ids=["kb-a"],
            access_scopes=_scope(),
            payload=SearchRequest(
                query="marker",
                filter_expressions=[FilterExpression(field="object_prefix", operator="eq", value="tenant-b/private")],
            ),
        )

    assert exc_info.value.status_code == 422
    assert cast(dict[str, Any], exc_info.value.detail)["error"]["code"] == "AUTHORITY_FILTER_FORBIDDEN"


@pytest.mark.asyncio
async def test_search_filter_source_id_must_belong_to_the_authorized_scope() -> None:
    source_a = "44444444-4444-4444-8444-444444444444"
    source_b = "55555555-5555-4555-8555-555555555555"
    connection = _Connection({source_a: {"knowledge_base_id": "kb-a"}})
    await api_app._authorize_search_identity_filters(
        connection,
        tenant_id="tenant-a",
        kb_ids=["kb-a"],
        access_scopes=_scope(),
        payload=SearchRequest(query="marker", filters=SearchFilters(source_id=source_a)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await api_app._authorize_search_identity_filters(
            connection,
            tenant_id="tenant-a",
            kb_ids=["kb-a"],
            access_scopes=_scope(),
            payload=SearchRequest(
                query="marker",
                filter_expressions=[FilterExpression(field="source_id", operator="eq", value=source_b)],
            ),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_search_filter_knowledge_base_id_cannot_expand_requested_scope() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await api_app._authorize_search_identity_filters(
            cast(Any, object()),
            tenant_id="tenant-a",
            kb_ids=["kb-a"],
            access_scopes=_scope(),
            payload=SearchRequest(
                query="marker",
                filter_expressions=[FilterExpression(field="knowledge_base_id", operator="eq", value="kb-b")],
            ),
        )

    assert exc_info.value.status_code == 404
