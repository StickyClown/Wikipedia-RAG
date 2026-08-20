from __future__ import annotations

from typing import Any, cast

import pytest

from wikipediarag.retrieval import _confirm_current_candidates
from wikipediarag.search_index import bm25_search, dense_search, ensure_index


class _SearchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"hits": {"hits": []}}


def test_bm25_and_dense_queries_require_published_index_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _SearchClient()
    monkeypatch.setattr("wikipediarag.search_index.get_client", lambda _settings: client)
    bm25_search("needle", knowledge_base_id="kb", top_k=5)
    dense_search([0.1], knowledge_base_id="kb", top_k=5)
    for call in client.calls:
        filters = call["body"]["query"]["bool"]["filter"]
        assert {"term": {"metadata.publication_status.keyword": "published"}} in filters
        assert not any("tenant_id" in str(item) for item in filters)


def test_workspace_index_mapping_does_not_store_tenant_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Indices:
        def exists(self, **_kwargs: Any) -> bool:
            return False

        def create(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def exists_alias(self, **_kwargs: Any) -> bool:
            return True

    class Client:
        indices = Indices()

    monkeypatch.setattr("wikipediarag.search_index.get_client", lambda _settings: Client())
    ensure_index()

    properties = captured["body"]["mappings"]["properties"]
    assert "tenant_id" not in properties


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
                "metadata": {},
            },
        }

    monkeypatch.setattr("wikipediarag.retrieval.fetch_current_retrieval_chunks", current)
    candidates = [
        {"chunk_id": value, "metadata": {}, "scores": {"bm25": 1.0}} for value in ("allowed", "staged", "restricted")
    ]
    confirmed = await _confirm_current_candidates(
        cast(Any, object()),
        {"bm25:v0": candidates},
        knowledge_base_by_label={"bm25:v0": "kb"},
        search_filters=None,
    )
    assert [item["chunk_id"] for item in confirmed["bm25:v0"]] == ["allowed", "restricted"]
    assert confirmed["bm25:v0"][0]["scores"] == {"bm25": 1.0}
