from __future__ import annotations

from typing import Any, cast

import pytest

from wikipediarag.config import Settings
from wikipediarag.schemas import Evidence, FilterExpression, SearchFilters, SearchRequest
from wikipediarag.search_service import (
    _confirm_workspace_search_results,
    _opensearch_filter_payload,
    _search_fingerprint,
    run_public_search,
)
from wikipediarag.workspace_access import PlatformRole


@pytest.fixture(autouse=True)
def _search_storage_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retrieval-shape tests independent of the new authoritative DB read."""

    async def marker(*_args: Any, **_kwargs: Any) -> str:
        return "test-marker"

    async def confirm(_conn: object, results: list[Any], **_kwargs: Any) -> list[Any]:
        return results

    monkeypatch.setattr("wikipediarag.search_service.retrieval_document_scope_marker", marker)
    monkeypatch.setattr("wikipediarag.search_service._confirm_current_search_results", confirm)


def _evidence(
    chunk_id: str,
    *,
    document_id: str,
    title: str = "Report",
    content: str = "A verification marker appears in this searchable report.",
    score: float = 0.9,
    language: str = "ru",
    document_type: str = "application/pdf",
    document_access: dict[str, Any] | None = None,
) -> Evidence:
    metadata = {
        "document_id": document_id,
        "document_version_id": f"{document_id}:v1",
        "source_type": "upload_document",
        "content_type": document_type,
        "language": language,
        "document_date": "2026-07-29",
        "locator": {"page": 1},
    }
    if document_access is not None:
        metadata["document_access"] = document_access
    return Evidence(
        evidence_id=chunk_id,
        chunk_id=chunk_id,
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        title=title,
        section_path=["Intro"],
        content=content,
        source_url="http://localhost/doc",
        scores={"rerank": score, "fusion": score / 2},
        ranks={"rerank": 1},
        metadata=metadata,
    )


async def test_workspace_candidate_confirmation_removes_revoked_document(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Repository:
        def __init__(self, _conn: object) -> None:
            pass

        async def authorized_document_ids(self, **kwargs: Any) -> frozenset[str]:
            calls.append(kwargs["document_ids"])
            return frozenset({"doc:visible"})

    monkeypatch.setattr("wikipediarag.search_service.WorkspaceGrantRepository", Repository)
    results = [
        _search_result_for_workspace("doc:visible"),
        _search_result_for_workspace("doc:revoked"),
    ]

    filtered = await _confirm_workspace_search_results(
        cast(Any, object()),
        results,
        actor_user_id="user",
        actor_platform_role=PlatformRole.user,
    )

    assert calls == [["doc:visible", "doc:revoked"]]
    assert [item.document_id for item in filtered] == ["doc:visible"]


def test_workspace_cache_marker_partitions_equivalent_searches() -> None:
    payload = SearchRequest(query="verification")
    shared = (["kb"], "documents")

    assert _search_fingerprint(
        payload,
        knowledge_base_ids=shared[0],
        document_scope_marker=shared[1],
        workspace_access_marker="user-a|group-a|8",
    ) != _search_fingerprint(
        payload,
        knowledge_base_ids=shared[0],
        document_scope_marker=shared[1],
        workspace_access_marker="user-b|group-a|8",
    )
    assert _search_fingerprint(
        payload,
        knowledge_base_ids=shared[0],
        document_scope_marker=shared[1],
        workspace_access_marker="user-a|group-a|8",
    ) != _search_fingerprint(
        payload,
        knowledge_base_ids=shared[0],
        document_scope_marker=shared[1],
        workspace_access_marker="user-a|group-a|9",
    )


def _search_result_for_workspace(document_id: str) -> Any:
    from wikipediarag.schemas import SearchResult

    return SearchResult(
        chunk_id=f"chunk:{document_id}",
        document_id=document_id,
        knowledge_base_id="kb",
        title="Report",
        snippet="safe",
        source_url="http://localhost/doc",
        source_type="upload_document",
        score=1.0,
    )


@pytest.mark.asyncio
async def test_public_search_uses_hybrid_retrieval_and_returns_facets_groups_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_retrieve(*_args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        from wikipediarag.schemas import RetrievalResult

        return RetrievalResult(
            query="verification",
            trace_id="trace",
            evidence=[
                _evidence("chunk:1", document_id="doc:1", score=0.99),
                _evidence("chunk:2", document_id="doc:1", score=0.80),
                _evidence("chunk:3", document_id="doc:2", score=0.70, language="en"),
            ],
            events=[],
        )

    monkeypatch.setattr("wikipediarag.search_service.retrieve", fake_retrieve)

    response = await run_public_search(
        cast(Any, object()),
        SearchRequest(
            query="verification",
            knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
            ranking_profile="upload_sota_mvp",
            limit=1,
            include_facets=True,
            group_by_document=True,
        ),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        settings=Settings(),
    )

    assert calls[0]["persist_events"] is False
    assert calls[0]["search_filters"] == {}
    assert calls[0]["profile_overrides"]["postprocess"]["final_evidence_max"] >= 20
    assert response.has_more is True
    assert response.next_cursor
    assert response.results[0].document_id == "doc:1"
    assert response.results[0].highlights[0].field == "content"
    assert response.groups[0].document_id == "doc:1"
    assert {facet.field for facet in response.facets} >= {"source_type", "document_type", "language"}


@pytest.mark.asyncio
async def test_public_search_infers_upload_profile_from_active_index(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_get_kb(_conn: object, _tenant_id: str, _kb_id: str) -> dict[str, str]:
        return {"active_index": "wiki-chunks-read-upload"}

    async def fake_load_index(_conn: object, **_kwargs: Any) -> dict[str, str]:
        return {"source_type": "upload", "embedding_alias": "embed_default"}

    async def fake_retrieve(*_args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        from wikipediarag.schemas import RetrievalResult

        return RetrievalResult(
            query="verification",
            trace_id="trace",
            evidence=[_evidence("chunk:1", document_id="doc:1")],
            events=[],
        )

    monkeypatch.setattr("wikipediarag.search_service.get_knowledge_base", fake_get_kb)
    monkeypatch.setattr("wikipediarag.search_service.load_index_version_by_read_alias", fake_load_index)
    monkeypatch.setattr("wikipediarag.search_service.retrieve", fake_retrieve)

    await run_public_search(
        cast(Any, object()),
        SearchRequest(
            query="verification",
            knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        ),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        settings=Settings(),
    )

    assert calls[0]["profile_name"] == "upload_sota_mvp"


@pytest.mark.asyncio
async def test_public_search_applies_simple_and_typed_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_retrieve(*_args: Any, **_kwargs: Any) -> Any:
        from wikipediarag.schemas import RetrievalResult

        return RetrievalResult(
            query="verification",
            trace_id="trace",
            evidence=[
                _evidence("chunk:ru", document_id="doc:ru", language="ru"),
                _evidence("chunk:en", document_id="doc:en", language="en", title="Draft"),
            ],
            events=[],
        )

    monkeypatch.setattr("wikipediarag.search_service.retrieve", fake_retrieve)

    response = await run_public_search(
        cast(Any, object()),
        SearchRequest(
            query="verification",
            knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
            ranking_profile="upload_sota_mvp",
            filters=SearchFilters(language="ru"),
            filter_expressions=[FilterExpression(field="title", operator="contains", value="Report")],
        ),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        settings=Settings(),
    )

    assert [item.chunk_id for item in response.results] == ["chunk:ru"]


@pytest.mark.asyncio
async def test_public_search_ignores_legacy_document_access_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retrieve(*_args: Any, **kwargs: Any) -> Any:
        from wikipediarag.schemas import RetrievalResult

        return RetrievalResult(
            query="verification",
            trace_id="trace",
            evidence=[
                _evidence("chunk:open", document_id="doc:open"),
                _evidence(
                    "chunk:restricted",
                    document_id="doc:restricted",
                    document_access={"policy": "restricted", "user_ids": ["other-user"], "group_ids": []},
                ),
            ],
            events=[],
        )

    monkeypatch.setattr("wikipediarag.search_service.retrieve", fake_retrieve)
    response = await run_public_search(
        cast(Any, object()),
        SearchRequest(
            query="verification",
            knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
            ranking_profile="upload_sota_mvp",
        ),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        settings=Settings(),
    )

    assert [item.chunk_id for item in response.results] == ["chunk:open", "chunk:restricted"]


@pytest.mark.asyncio
async def test_public_search_does_not_interpret_legacy_tenant_document_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retrieve(*_args: Any, **_kwargs: Any) -> Any:
        from wikipediarag.schemas import RetrievalResult

        return RetrievalResult(
            query="verification",
            trace_id="trace",
            evidence=[
                _evidence("chunk:kb", document_id="doc:kb"),
                _evidence(
                    "chunk:tenant",
                    document_id="doc:tenant",
                    document_access={"policy": "tenant", "user_ids": [], "group_ids": []},
                ),
            ],
            events=[],
        )

    monkeypatch.setattr("wikipediarag.search_service.retrieve", fake_retrieve)

    response = await run_public_search(
        cast(Any, object()),
        SearchRequest(
            query="verification",
            knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
            ranking_profile="upload_sota_mvp",
        ),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        settings=Settings(),
    )

    assert [item.chunk_id for item in response.results] == ["chunk:kb", "chunk:tenant"]


@pytest.mark.asyncio
async def test_public_search_does_not_interpret_legacy_group_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retrieve(*_args: Any, **_kwargs: Any) -> Any:
        from wikipediarag.schemas import RetrievalResult

        return RetrievalResult(
            query="verification",
            trace_id="trace",
            evidence=[
                _evidence(
                    "chunk:restricted",
                    document_id="doc:restricted",
                    document_access={"policy": "restricted", "user_ids": [], "group_ids": ["group:1"]},
                ),
            ],
            events=[],
        )

    monkeypatch.setattr("wikipediarag.search_service.retrieve", fake_retrieve)

    response = await run_public_search(
        cast(Any, object()),
        SearchRequest(
            query="verification",
            knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
            ranking_profile="upload_sota_mvp",
        ),
        tenant_id="11111111-1111-4111-8111-111111111111",
        knowledge_base_ids=["33333333-3333-4333-8333-333333333333"],
        settings=Settings(),
    )

    assert [item.chunk_id for item in response.results] == ["chunk:restricted"]


def test_filter_ast_compiles_supported_fields_to_opensearch_payload() -> None:
    payload = _opensearch_filter_payload(
        SearchRequest(
            query="q",
            filters=SearchFilters(source_kind="external_source"),
            filter_expressions=[
                FilterExpression(field="document_date", operator="gte", value="2026-01-01"),
                FilterExpression(field="document_id", operator="eq", value="doc:1"),
            ],
        )
    )

    assert payload["source_kind"] == "external_source"
    assert payload["date_from"] == "2026-01-01"
    assert payload["document_id"] == "doc:1"
