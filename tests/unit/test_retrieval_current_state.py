from __future__ import annotations

from typing import Any, cast

import pytest

from wikipediarag.auth import KnowledgeBaseRole
from wikipediarag.document_access import DocumentAccessScope
from wikipediarag.retrieval import _confirm_current_candidates
from wikipediarag.search_index import bm25_search, dense_search


class _SearchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"hits": {"hits": []}}


def test_bm25_and_dense_queries_require_published_index_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _SearchClient()
    monkeypatch.setattr("wikipediarag.search_index.get_client", lambda _settings: client)
    bm25_search("needle", tenant_id="t", knowledge_base_id="kb", top_k=5)
    dense_search([0.1], tenant_id="t", knowledge_base_id="kb", top_k=5)
    for call in client.calls:
        filters = call["body"]["query"]["bool"]["filter"]
        assert {"term": {"metadata.publication_status.keyword": "published"}} in filters


@pytest.mark.asyncio
async def test_candidates_are_confirmed_in_postgresql_before_fusion(monkeypatch: pytest.MonkeyPatch) -> None:
    async def current(*_args: Any, **_kwargs: Any) -> dict[str, dict[str, Any]]:
        return {
            "allowed": {
                "chunk_id": "allowed",
                "document_id": "document",
                "document_version_id": "version",
                "metadata": {},
            },
            "restricted": {
                "chunk_id": "restricted",
                "document_id": "document",
                "document_version_id": "version",
                "metadata": {"document_access": {"policy": "restricted", "user_ids": ["other"], "group_ids": []}},
            },
        }

    monkeypatch.setattr("wikipediarag.retrieval.fetch_current_retrieval_chunks", current)
    candidates = [
        {"chunk_id": value, "metadata": {}, "scores": {"bm25": 1.0}} for value in ("allowed", "staged", "restricted")
    ]
    scope = DocumentAccessScope(user_id="viewer", kb_role=KnowledgeBaseRole.viewer)
    confirmed = await _confirm_current_candidates(
        cast(Any, object()),
        {"bm25:v0": candidates},
        tenant_id="tenant",
        knowledge_base_by_label={"bm25:v0": "kb"},
        search_filters={"document_access_scopes": {"kb": scope}},
    )
    assert [item["chunk_id"] for item in confirmed["bm25:v0"]] == ["allowed"]
    assert confirmed["bm25:v0"][0]["scores"] == {"bm25": 1.0}
