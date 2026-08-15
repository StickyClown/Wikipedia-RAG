from __future__ import annotations

import base64
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Any

from redis import asyncio as redis_async
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.config import Settings, get_settings
from wikipediarag.document_access import DocumentAccessScope, is_document_visible
from wikipediarag.ids import stable_hash
from wikipediarag.repository import (
    fetch_current_retrieval_chunks,
    get_knowledge_base,
    load_index_version_by_read_alias,
    retrieval_document_scope_marker,
)
from wikipediarag.retrieval import retrieve, retrieve_multi
from wikipediarag.schemas import (
    Evidence,
    FilterExpression,
    SearchDocumentGroup,
    SearchFacet,
    SearchFacetBucket,
    SearchHighlight,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

SEARCH_MAX_WINDOW = 1000
SEARCH_INITIAL_WINDOW = 50
SEARCH_CACHE_TTL_SECONDS = 120
_REDIS_CLIENT: redis_async.Redis | None = None
FACET_FIELDS = ("source_type", "document_type", "language", "knowledge_base_id")
FILTER_FIELDS = {
    "document_type",
    "language",
    "date",
    "document_date",
    "source",
    "source_kind",
    "source_id",
    "document_id",
    "title",
}
METADATA_PREFIX = "metadata."


async def run_public_search(
    conn: AsyncConnection,
    payload: SearchRequest,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
    settings: Settings | None = None,
    document_access_scopes: dict[str, DocumentAccessScope] | None = None,
) -> SearchResponse:
    resolved = settings or get_settings()
    document_scope_marker = await retrieval_document_scope_marker(
        conn,
        tenant_id=tenant_id,
        knowledge_base_ids=knowledge_base_ids,
    )
    fingerprint = _search_fingerprint(
        payload,
        tenant_id=tenant_id,
        knowledge_base_ids=knowledge_base_ids,
        document_access_scopes=document_access_scopes,
        document_scope_marker=document_scope_marker,
    )
    offset, cursor_fingerprint = _decode_cursor(payload.cursor) if payload.cursor else (payload.offset, None)
    if cursor_fingerprint is not None and cursor_fingerprint != fingerprint:
        return SearchResponse(results=[], limit=payload.limit, offset=offset, has_more=False)
    if offset >= SEARCH_MAX_WINDOW:
        return SearchResponse(results=[], limit=payload.limit, offset=offset, has_more=False)
    window = min(SEARCH_MAX_WINDOW, _cache_window(offset + payload.limit + 1))
    profile_overrides = _search_profile_overrides(window)
    ranking_profile = payload.ranking_profile or await _infer_ranking_profile(
        conn,
        tenant_id=tenant_id,
        knowledge_base_ids=knowledge_base_ids,
    )
    search_filters = _opensearch_filter_payload(payload)
    if document_access_scopes:
        search_filters["document_access_scopes"] = document_access_scopes
    trace_id = stable_hash([tenant_id, *knowledge_base_ids, payload.query, offset, window], 32)
    cache_key = _redis_key(tenant_id, fingerprint)
    cached_payload = await _redis_get(cache_key, resolved)
    if cached_payload is not None and len(cached_payload.get("results", [])) >= window:
        cached_results = [SearchResult.model_validate(item) for item in cached_payload["results"]]
        cached_results = await _confirm_current_search_results(
            conn,
            cached_results,
            tenant_id=tenant_id,
            document_access_scopes=document_access_scopes,
        )
        page = cached_results[offset : offset + payload.limit + 1]
        has_more = len(page) > payload.limit
        return SearchResponse(
            results=page[: payload.limit],
            limit=payload.limit,
            offset=offset,
            has_more=has_more,
            next_cursor=_encode_cursor(offset + payload.limit, fingerprint) if has_more else None,
            facets=[SearchFacet.model_validate(item) for item in cached_payload.get("facets", [])]
            if payload.include_facets
            else [],
            groups=_document_groups(page[: payload.limit]) if payload.group_by_document else [],
            facet_scope="lexical_filtered_corpus",
        )
    if len(knowledge_base_ids) > 1:
        retrieval = await retrieve_multi(
            conn,
            payload.query,
            tenant_id=tenant_id,
            knowledge_base_ids=knowledge_base_ids,
            query_run_id=None,
            trace_id=trace_id,
            settings=resolved,
            top_k=window,
            profile_name=ranking_profile,
            profile_overrides=profile_overrides,
            search_filters=search_filters,
            persist_events=False,
        )
    else:
        retrieval = await retrieve(
            conn,
            payload.query,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_ids[0],
            query_run_id=None,
            trace_id=trace_id,
            settings=resolved,
            top_k=window,
            profile_name=ranking_profile,
            profile_overrides=profile_overrides,
            search_filters=search_filters,
            persist_events=False,
        )
    filtered = (
        [
            item
            for item in retrieval.evidence
            if _matches_document_access(item, document_access_scopes) and _matches_request(item, payload)
        ]
        if retrieval is not None
        else []
    )
    search_results = [
        _search_result(item, query=payload.query, include_highlights=payload.include_highlights) for item in filtered
    ]
    search_results = await _confirm_current_search_results(
        conn,
        search_results,
        tenant_id=tenant_id,
        document_access_scopes=document_access_scopes,
    )
    facets = _facets(filtered) if payload.include_facets else []
    await _redis_set(
        cache_key,
        {
            "results": [item.model_dump(mode="json") for item in search_results],
            "facets": [item.model_dump(mode="json") for item in facets],
        },
        resolved,
    )
    page = search_results[offset : offset + payload.limit + 1]
    has_more = len(page) > payload.limit
    results = page[: payload.limit]
    groups = _document_groups(results) if payload.group_by_document else []
    return SearchResponse(
        results=results,
        limit=payload.limit,
        offset=offset,
        has_more=has_more,
        next_cursor=_encode_cursor(offset + payload.limit, fingerprint) if has_more else None,
        facets=facets,
        groups=groups,
        facet_scope="lexical_filtered_corpus",
    )


def _cache_window(required: int) -> int:
    window = SEARCH_INITIAL_WINDOW
    while window < required and window < SEARCH_MAX_WINDOW:
        window = min(SEARCH_MAX_WINDOW, window * 2)
    return window


def _redis_key(tenant_id: str, fingerprint: str) -> str:
    return f"wikipediarag:search:{stable_hash([tenant_id, fingerprint], 32)}"


async def _redis_get(key: str, settings: Settings) -> dict[str, Any] | None:
    global _REDIS_CLIENT
    try:
        if _REDIS_CLIENT is None:
            _REDIS_CLIENT = redis_async.from_url(settings.redis_url, decode_responses=True, max_connections=20)
        raw = await _REDIS_CLIENT.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _redis_set(key: str, value: dict[str, Any], settings: Settings) -> None:
    global _REDIS_CLIENT
    try:
        if _REDIS_CLIENT is None:
            _REDIS_CLIENT = redis_async.from_url(settings.redis_url, decode_responses=True, max_connections=20)
        await _REDIS_CLIENT.set(key, json.dumps(value, ensure_ascii=False), ex=SEARCH_CACHE_TTL_SECONDS)
    except Exception:
        return


async def _infer_ranking_profile(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
) -> str | None:
    rows: list[dict[str, Any]] = []
    for kb_id in knowledge_base_ids:
        kb = await get_knowledge_base(conn, tenant_id, kb_id)
        read_alias = str(kb.get("active_index") or "") if kb else ""
        if not read_alias:
            return None
        row = await load_index_version_by_read_alias(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            read_alias=read_alias,
        )
        if row is None:
            return None
        rows.append(row)
    source_types = {str(row.get("source_type") or "") for row in rows}
    embedding_aliases = {str(row.get("embedding_alias") or "") for row in rows}
    if source_types == {"upload"} and embedding_aliases == {"embed_default"}:
        return "upload_sota_mvp"
    if source_types == {"upload"} and embedding_aliases == {"mock_embed_default"}:
        return "upload_mock"
    return None


def _search_profile_overrides(window: int) -> dict[str, Any]:
    first_stage_top_k = max(window * 2, 100)
    return {
        "retrieval": {
            "top_k": window,
            "bm25_top_k": first_stage_top_k,
            "dense_top_k": first_stage_top_k,
            "fusion_top_k": max(window, 60),
            "rerank_top_k": max(window, 50),
        },
        "postprocess": {
            "final_evidence_min": 1,
            "final_evidence_max": window,
            "parent_expansion": "off",
            "page_quota": max(5, window),
        },
    }


def _opensearch_filter_payload(payload: SearchRequest) -> dict[str, Any]:
    filters = payload.filters.model_dump(mode="json", exclude_none=True)
    for expression in payload.filter_expressions:
        field = _canonical_field(expression.field)
        if field in {"document_type", "language", "source_kind", "source_id", "document_id", "title"}:
            filters[field] = expression.value
        elif field == "document_date":
            if expression.operator == "gte":
                filters["date_from"] = expression.value
            elif expression.operator == "lte":
                filters["date_to"] = expression.value
    return filters


def _matches_request(evidence: Evidence, payload: SearchRequest) -> bool:
    simple = payload.filters
    if (
        simple.document_type
        and simple.document_type.casefold() not in _field_value(evidence, "document_type").casefold()
    ):
        return False
    if simple.language and simple.language.casefold() != _field_value(evidence, "language").casefold():
        return False
    document_date = _parse_date(_field_value(evidence, "document_date"))
    if simple.date_from and (document_date is None or document_date < simple.date_from):
        return False
    if simple.date_to and (document_date is None or document_date > simple.date_to):
        return False
    if simple.source and simple.source.casefold() not in _source_blob(evidence).casefold():
        return False
    if simple.source_kind and simple.source_kind.casefold() != _field_value(evidence, "source_kind").casefold():
        return False
    if simple.source_id and simple.source_id.casefold() != _field_value(evidence, "source_id").casefold():
        return False
    return all(_matches_expression(evidence, expression) for expression in payload.filter_expressions)


def _matches_document_access(evidence: Evidence, scopes: dict[str, DocumentAccessScope] | None) -> bool:
    if not scopes:
        return True
    return is_document_visible(dict(evidence.metadata or {}), scopes.get(evidence.knowledge_base_id))


async def _confirm_current_search_results(
    conn: AsyncConnection,
    results: list[SearchResult],
    *,
    tenant_id: str,
    document_access_scopes: dict[str, DocumentAccessScope] | None,
) -> list[SearchResult]:
    """Recheck cache and retrieval output against current publication and ACL state."""
    by_kb: dict[str, list[SearchResult]] = defaultdict(list)
    for item in results:
        by_kb[item.knowledge_base_id].append(item)
    allowed: list[SearchResult] = []
    for knowledge_base_id, scoped_results in by_kb.items():
        rows = await fetch_current_retrieval_chunks(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            chunk_ids=[item.chunk_id for item in scoped_results],
        )
        scope = (document_access_scopes or {}).get(knowledge_base_id)
        for item in scoped_results:
            row = rows.get(item.chunk_id)
            if row is not None and is_document_visible(dict(row.get("metadata") or {}), scope):
                allowed.append(item)
    return allowed


def _matches_expression(evidence: Evidence, expression: FilterExpression) -> bool:
    field = _canonical_field(expression.field)
    if field not in FILTER_FIELDS and not field.startswith(METADATA_PREFIX):
        return False
    actual = _field_value(evidence, field)
    values = expression.value if isinstance(expression.value, list) else [expression.value]
    expected = [str(value) for value in values]
    if expression.operator == "eq":
        return any(actual.casefold() == value.casefold() for value in expected)
    if expression.operator == "contains":
        return any(value.casefold() in actual.casefold() for value in expected)
    if expression.operator == "in":
        return any(actual.casefold() == value.casefold() for value in expected)
    actual_date = _parse_date(actual)
    expected_date = _parse_date(expected[0]) if expected else None
    if actual_date is None or expected_date is None:
        return False
    if expression.operator == "gte":
        return actual_date >= expected_date
    if expression.operator == "lte":
        return actual_date <= expected_date
    return False


def _search_result(evidence: Evidence, *, query: str, include_highlights: bool) -> SearchResult:
    metadata = dict(evidence.metadata or {})
    locator = metadata.get("locator")
    document_date = _parse_date(_field_value(evidence, "document_date"))
    score = _best_score(evidence.scores)
    return SearchResult(
        chunk_id=evidence.chunk_id,
        document_id=_field_value(evidence, "document_id") or evidence.chunk_id,
        document_version_id=_field_value(evidence, "document_version_id") or None,
        knowledge_base_id=evidence.knowledge_base_id,
        title=evidence.title,
        snippet=_snippet(evidence.content, query=query),
        section_path=list(evidence.section_path),
        source_url=evidence.source_url,
        source_type=_field_value(evidence, "source_type") or "unknown",
        document_type=_field_value(evidence, "document_type") or None,
        language=_field_value(evidence, "language") or None,
        document_date=document_date,
        locator=locator if isinstance(locator, dict) else {},
        score=score,
        ranks=dict(evidence.ranks),
        highlights=[SearchHighlight(field="content", fragments=[_snippet(evidence.content, query=query)])]
        if include_highlights
        else [],
        provenance=evidence.provenance,
    )


def _document_groups(results: list[SearchResult]) -> list[SearchDocumentGroup]:
    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for result in results:
        grouped[result.document_id].append(result)
    groups: list[SearchDocumentGroup] = []
    for document_id, hits in grouped.items():
        best = max(hits, key=lambda item: item.score)
        groups.append(
            SearchDocumentGroup(
                document_id=document_id,
                document_version_id=best.document_version_id,
                knowledge_base_id=best.knowledge_base_id,
                title=best.title,
                source_url=best.source_url,
                source_type=best.source_type,
                best_score=best.score,
                hit_count=len(hits),
                hits=hits[:3],
            )
        )
    groups.sort(key=lambda item: item.best_score, reverse=True)
    return groups


def _facets(evidence: Iterable[Evidence]) -> list[SearchFacet]:
    counters = {field: Counter[str]() for field in FACET_FIELDS}
    for item in evidence:
        for field in FACET_FIELDS:
            value = _field_value(item, field)
            if value:
                counters[field][value] += 1
    return [
        SearchFacet(
            field=field,
            buckets=[SearchFacetBucket(value=value, count=count) for value, count in counter.most_common(20)],
        )
        for field, counter in counters.items()
        if counter
    ]


def _field_value(evidence: Evidence, field: str) -> str:
    metadata = dict(evidence.metadata or {})
    if field.startswith(METADATA_PREFIX):
        value = metadata.get(field.removeprefix(METADATA_PREFIX))
    elif field == "document_type":
        value = metadata.get("content_type") or metadata.get("detected_mime") or metadata.get("source_type")
    elif field == "language":
        value = metadata.get("language") or metadata.get("detected_language")
    elif field == "document_date":
        value = metadata.get("document_date")
    elif field == "source":
        value = _source_blob(evidence)
    elif field == "source_kind":
        value = metadata.get("source_kind") or metadata.get("source_type")
    elif field == "title":
        value = evidence.title
    elif field == "knowledge_base_id":
        value = evidence.knowledge_base_id
    else:
        value = metadata.get(field)
    return str(value).strip() if value is not None else ""


def _source_blob(evidence: Evidence) -> str:
    metadata = dict(evidence.metadata or {})
    return " ".join(
        str(value)
        for value in (
            metadata.get("source_kind"),
            metadata.get("source_type"),
            metadata.get("source_id"),
            metadata.get("source_uri"),
            metadata.get("filename"),
            evidence.source_url,
            evidence.title,
        )
        if value
    )


def _canonical_field(field: str) -> str:
    normalized = field.strip().casefold()
    if normalized == "date":
        return "document_date"
    return normalized


def _best_score(scores: dict[str, float]) -> float:
    for key in ("rerank", "fusion", "rrf_total", "dense", "bm25"):
        if key in scores:
            return float(scores[key])
    return max((float(value) for value in scores.values()), default=0.0)


def _snippet(content: str, *, query: str, max_chars: int = 360) -> str:
    normalized_content = " ".join(content.split())
    if len(normalized_content) <= max_chars:
        return normalized_content
    terms = [re.escape(term) for term in query.split() if len(term) >= 3]
    match = re.search("|".join(terms), normalized_content, flags=re.IGNORECASE) if terms else None
    center = match.start() if match else 0
    start = max(0, center - max_chars // 2)
    end = min(len(normalized_content), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start else ""
    suffix = "..." if end < len(normalized_content) else ""
    return f"{prefix}{normalized_content[start:end]}{suffix}"


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _encode_cursor(offset: int, fingerprint: str | None = None) -> str:
    payload: dict[str, Any] = {"offset": offset}
    if fingerprint:
        payload.update({"version": 2, "fingerprint": fingerprint})
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return 0
    value = payload.get("offset") if isinstance(payload, dict) else 0
    try:
        return max(0, min(SEARCH_MAX_WINDOW, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
    if not cursor:
        return 0, None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return 0, None
    if not isinstance(payload, dict):
        return 0, None
    try:
        offset = max(0, min(SEARCH_MAX_WINDOW, int(payload.get("offset") or 0)))
    except (TypeError, ValueError):
        offset = 0
    return offset, str(payload.get("fingerprint")) if payload.get("version") == 2 else None


def _search_fingerprint(
    payload: SearchRequest,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
    document_access_scopes: dict[str, DocumentAccessScope] | None,
    document_scope_marker: str = "none",
) -> str:
    scope_payload = {
        str(kb_id): {
            "bypass": bool(scope.bypass),
            "tenant": stable_hash([scope.tenant_id], 16),
            "user": stable_hash([scope.user_id], 16),
            "role": str(scope.kb_role or ""),
            "groups": sorted(stable_hash([item], 16) for item in scope.group_ids),
        }
        for kb_id, scope in (document_access_scopes or {}).items()
    }
    scope_hash = stable_hash([json.dumps(scope_payload, sort_keys=True)], 32)
    return stable_hash(
        [
            "search_cursor_v3",
            tenant_id,
            *sorted(knowledge_base_ids),
            " ".join(payload.query.split()).casefold(),
            json.dumps(payload.filters.model_dump(mode="json"), sort_keys=True),
            json.dumps([item.model_dump(mode="json") for item in payload.filter_expressions], sort_keys=True),
            payload.ranking_profile or "",
            scope_hash,
            document_scope_marker,
        ],
        32,
    )
