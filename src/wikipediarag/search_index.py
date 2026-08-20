from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from typing import Any, cast

from opensearchpy import NotFoundError, OpenSearch
from opensearchpy.helpers import bulk

from wikipediarag.config import Settings, get_settings
from wikipediarag.ids import stable_hash
from wikipediarag.wiki_dump import Chunk

PHYSICAL_INDEX = "wiki-chunks-v1"
READ_ALIAS = "wiki-chunks-read"
WRITE_ALIAS = "wiki-chunks-write"


def build_index_names(
    *,
    source_type: str,
    snapshot_id: str,
    retrieval_profile: str,
    embedding_alias: str,
    embedding_dimensions: int,
    knowledge_base_id: str | None = None,
) -> dict[str, str]:
    # Kept as an ignored keyword while callers are being cut over.  Tenant
    # identity is never stored in, queried from, or used to name an index.
    scope_prefix = stable_hash([knowledge_base_id or "workspace"], 10)
    suffix = stable_hash(
        [
            knowledge_base_id or "legacy",
            source_type,
            snapshot_id,
            retrieval_profile,
            embedding_alias,
            embedding_dimensions,
        ],
        16,
    )
    return {
        "version_id": (
            f"{scope_prefix}:{source_type}:{snapshot_id}:{retrieval_profile}:{embedding_alias}:{embedding_dimensions}"
        ),
        "physical": f"wiki-chunks-{scope_prefix}-{suffix}",
        "read_alias": f"wiki-chunks-read-{scope_prefix}-{suffix}",
        "write_alias": f"wiki-chunks-write-{scope_prefix}-{suffix}",
    }


@lru_cache(maxsize=16)
def _cached_client(opensearch_url: str) -> OpenSearch:
    return OpenSearch(
        hosts=[opensearch_url],
        verify_certs=False,
        ssl_show_warn=False,
        pool_maxsize=20,
    )


def get_client(settings: Settings | None = None) -> OpenSearch:
    resolved = settings or get_settings()
    return _cached_client(resolved.opensearch_url)


def ensure_index(
    settings: Settings | None = None,
    *,
    physical_index: str = PHYSICAL_INDEX,
    read_alias: str = READ_ALIAS,
    write_alias: str = WRITE_ALIAS,
    dimensions: int | None = None,
) -> None:
    resolved = settings or get_settings()
    vector_dimensions = dimensions or resolved.embedding_dimensions
    client = get_client(resolved)
    if not client.indices.exists(index=physical_index):
        client.indices.create(
            index=physical_index,
            body={
                "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
                "mappings": {
                    "properties": {
                        "knowledge_base_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "document_version_id": {"type": "keyword"},
                        "chunk_id": {"type": "keyword"},
                        "page_id": {"type": "long"},
                        "revision_id": {"type": "long"},
                        "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                        "alias_text": {"type": "text"},
                        "section_path_text": {"type": "text"},
                        "content": {"type": "text"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": vector_dimensions,
                            "space_type": "cosinesimil",
                        },
                        "source_uri": {"type": "keyword", "index": False},
                        "source_url": {"type": "keyword", "index": False},
                        "prev_chunk_id": {"type": "keyword"},
                        "next_chunk_id": {"type": "keyword"},
                        "content_hash": {"type": "keyword"},
                        "metadata": {"type": "object", "enabled": True},
                    }
                },
            },
        )
    for alias in (read_alias, write_alias):
        if not client.indices.exists_alias(name=alias):
            client.indices.put_alias(index=physical_index, name=alias)


def bulk_index_chunks(
    chunks: Iterable[Chunk],
    *,
    knowledge_base_id: str,
    settings: Settings | None = None,
    write_alias: str = WRITE_ALIAS,
    dimensions: int | None = None,
    physical_index: str = PHYSICAL_INDEX,
    read_alias: str = READ_ALIAS,
    refresh: bool | str = False,
) -> int:
    resolved = settings or get_settings()
    ensure_index(
        resolved,
        physical_index=physical_index,
        read_alias=read_alias,
        write_alias=write_alias,
        dimensions=dimensions,
    )
    client = get_client(resolved)
    actions = [
        {
            "_op_type": "index",
            "_index": write_alias,
            "_id": f"{knowledge_base_id}:{chunk.id}",
            "_source": {
                "knowledge_base_id": knowledge_base_id,
                "document_id": chunk.document_id,
                "document_version_id": str(chunk.metadata.get("document_version_id") or ""),
                "chunk_id": chunk.id,
                "page_id": chunk.page_id,
                "revision_id": chunk.revision_id,
                "title": chunk.title,
                "alias_text": " ".join(
                    str(value) for value in cast(list[object], chunk.metadata.get("aliases", [])) if value
                )
                if isinstance(chunk.metadata.get("aliases"), list)
                else "",
                "section_path_text": " / ".join(chunk.section_path),
                "content": chunk.content,
                "embedding": chunk.embedding,
                "source_uri": chunk.source_uri,
                "source_url": chunk.source_url,
                "prev_chunk_id": chunk.prev_chunk_id,
                "next_chunk_id": chunk.next_chunk_id,
                "content_hash": chunk.content_hash,
                "metadata": {
                    **chunk.metadata,
                    "parent_chunk_id": chunk.parent_chunk_id,
                    "content_hash": chunk.content_hash,
                    "source_chunk_id": chunk.metadata.get("source_chunk_id") or chunk.id,
                },
            },
        }
        for chunk in chunks
    ]
    if not actions:
        return 0
    return _apply_projection_bulk(client, actions, refresh=refresh)


def bm25_search(
    query: str,
    *,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved = settings or get_settings()
    client = get_client(resolved)
    filter_clauses = [
        {"term": {"knowledge_base_id": knowledge_base_id}},
        # OpenSearch is a derived candidate index.  Publication in PostgreSQL is
        # authoritative, but this cheap predicate keeps staged rows out of the
        # normal first-stage result set as well.
        {"term": {"metadata.publication_status.keyword": "published"}},
        *_public_filter_clauses(filters or {}),
    ]
    response = client.search(
        index=read_alias,
        body={
            "size": top_k,
            "query": {
                "bool": {
                    "filter": filter_clauses,
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["title^3", "alias_text^4", "section_path_text^2", "content"],
                            }
                        }
                    ],
                }
            },
        },
    )
    candidates = [_hit_to_candidate(hit, "bm25") for hit in response["hits"]["hits"]]
    return sorted(candidates, key=lambda item: (-float(item["scores"].get("bm25", 0.0)), str(item.get("chunk_id"))))


def dense_search(
    query_vector: list[float],
    *,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved = settings or get_settings()
    client = get_client(resolved)
    filter_clauses = [
        {"term": {"knowledge_base_id": knowledge_base_id}},
        {"term": {"metadata.publication_status.keyword": "published"}},
        *_public_filter_clauses(filters or {}),
    ]
    response = client.search(
        index=read_alias,
        body={
            "size": top_k,
            "query": {
                "bool": {
                    "filter": filter_clauses,
                    "must": [{"knn": {"embedding": {"vector": query_vector, "k": top_k}}}],
                }
            },
        },
    )
    candidates = [_hit_to_candidate(hit, "dense") for hit in response["hits"]["hits"]]
    return sorted(candidates, key=lambda item: (-float(item["scores"].get("dense", 0.0)), str(item.get("chunk_id"))))


def delete_document_chunks(
    *,
    knowledge_base_id: str,
    document_id: str,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
) -> int:
    resolved = settings or get_settings()
    client = get_client(resolved)
    try:
        response = client.delete_by_query(
            index=read_alias,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"knowledge_base_id": knowledge_base_id}},
                            {"term": {"document_id": document_id}},
                        ]
                    }
                }
            },
            conflicts="proceed",
            refresh=True,
        )
    except NotFoundError:
        return 0
    return int(response.get("deleted") or 0)


def delete_document_version_chunks(
    *,
    knowledge_base_id: str,
    document_version_id: str,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
) -> int:
    resolved = settings or get_settings()
    client = get_client(resolved)
    try:
        response = client.delete_by_query(
            index=read_alias,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"knowledge_base_id": knowledge_base_id}},
                            {"term": {"document_version_id": document_version_id}},
                        ]
                    }
                }
            },
            conflicts="proceed",
            refresh=True,
        )
    except NotFoundError:
        # A first projection has no previous per-version document to remove.
        # Treat the missing derived index as an idempotent empty projection.
        return 0
    return int(response.get("deleted") or 0)


def read_document_projection(
    *,
    knowledge_base_id: str,
    document_id: str,
    limit: int,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
) -> list[dict[str, Any]]:
    """Read a bounded, exact derived projection for one document.

    The caller supplies the document identity from PostgreSQL.  This helper is
    intentionally not a general discovery API and returns no more than the
    configured safety bound.
    """
    try:
        response = get_client(settings or get_settings()).search(
            index=read_alias,
            body={
                "size": max(1, int(limit)),
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"knowledge_base_id": knowledge_base_id}},
                            {"term": {"document_id": document_id}},
                        ]
                    }
                },
                "_source": [
                    "chunk_id",
                    "document_version_id",
                    "content_hash",
                    "metadata.publication_status",
                ],
            },
        )
    except NotFoundError:
        return []
    return [dict(hit) for hit in response.get("hits", {}).get("hits", [])]


def delete_exact_projection_documents(
    *,
    document_ids: Iterable[str],
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
    refresh: bool | str = False,
) -> int:
    """Delete only explicitly observed OpenSearch document identifiers."""
    identifiers = sorted({str(value) for value in document_ids if str(value)})
    if not identifiers:
        return 0
    client = get_client(settings or get_settings())
    actions = [{"_op_type": "delete", "_index": read_alias, "_id": identifier} for identifier in identifiers]
    return _apply_projection_bulk(client, actions, refresh=refresh)


def _apply_projection_bulk(client: OpenSearch, actions: list[dict[str, Any]], *, refresh: bool | str) -> int:
    """Apply a bounded projection mutation and inspect every bulk outcome.

    ``helpers.bulk`` returns HTTP-successful item failures separately.  Treating
    those as success would make a reconciliation complete while the derived
    projection remains divergent, so failures are surfaced to the durable,
    bounded event retry policy.
    """
    succeeded, errors = bulk(
        client,
        actions,
        refresh=refresh,
        raise_on_error=False,
        raise_on_exception=False,
        stats_only=False,
    )
    if errors:
        statuses: list[str] = []
        for error in errors:
            operation: dict[str, Any] = next(iter(error.values()), {}) if isinstance(error, dict) else {}
            statuses.append(str(operation.get("status") or "unknown"))
        raise RuntimeError(f"SEARCH_PROJECTION_BULK_ITEM_FAILED:{','.join(sorted(set(statuses))[:4])}")
    if int(succeeded) != len(actions):
        raise RuntimeError("SEARCH_PROJECTION_BULK_COUNT_MISMATCH")
    return int(succeeded)


def projection_fingerprint(records: Iterable[dict[str, Any]]) -> str:
    """Return a safe stable fingerprint without retaining document content."""
    normalized = []
    for record in records:
        source = record.get("_source", record)
        metadata = source.get("metadata") or {}
        normalized.append(
            [
                str(source.get("chunk_id") or ""),
                str(source.get("document_version_id") or ""),
                str(source.get("content_hash") or metadata.get("content_hash") or ""),
                str(metadata.get("publication_status") or ""),
            ]
        )
    return stable_hash(sorted(normalized), 64)


def _public_filter_clauses(filters: dict[str, Any]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    document_type = _optional_text(filters.get("document_type"))
    language = _optional_text(filters.get("language"))
    date_from = _optional_text(filters.get("date_from"))
    date_to = _optional_text(filters.get("date_to"))
    source_kind = _optional_text(filters.get("source_kind"))
    source_id = _optional_text(filters.get("source_id"))
    document_id = _optional_text(filters.get("document_id"))
    title = _optional_text(filters.get("title"))

    if document_type:
        clauses.append(
            {
                "bool": {
                    "should": [
                        {"term": {"metadata.content_type.keyword": document_type}},
                        {"term": {"metadata.detected_mime.keyword": document_type}},
                        {"term": {"metadata.source_type.keyword": document_type}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if language:
        clauses.append(
            {
                "bool": {
                    "should": [
                        {"term": {"metadata.language.keyword": language}},
                        {"term": {"metadata.detected_language.keyword": language}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if date_from or date_to:
        date_range: dict[str, str] = {}
        if date_from:
            date_range["gte"] = date_from
        if date_to:
            date_range["lte"] = date_to
        clauses.append({"range": {"metadata.document_date.keyword": date_range}})
    if source_kind:
        clauses.append(
            {
                "bool": {
                    "should": [
                        {"term": {"metadata.source_kind.keyword": source_kind}},
                        {"term": {"metadata.source_type.keyword": source_kind}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if source_id:
        clauses.append({"term": {"metadata.source_id.keyword": source_id}})
    if document_id:
        clauses.append({"term": {"document_id": document_id}})
    if title:
        clauses.append({"match_phrase": {"title": title}})
    return clauses


def _optional_text(value: Any) -> str:
    return str(value).strip().casefold() if value is not None and str(value).strip() else ""


def _hit_to_candidate(hit: dict[str, Any], stage: str) -> dict[str, Any]:
    source = hit["_source"]
    return {
        "chunk_id": source["chunk_id"],
        "knowledge_base_id": source.get("knowledge_base_id", ""),
        "document_id": source["document_id"],
        "document_version_id": source.get("document_version_id", ""),
        "page_id": source.get("page_id"),
        "title": source["title"],
        "section_path": source.get("section_path_text", "").split(" / ") if source.get("section_path_text") else [],
        "content": source["content"],
        "source_uri": source.get("source_uri", ""),
        "source_url": source.get("source_url", ""),
        "locator": source.get("metadata", {}).get("locator", {}),
        "scores": {stage: float(hit.get("_score") or 0.0)},
        "ranks": {},
        "embedding": source.get("embedding", []),
        "metadata": source.get("metadata", {}),
    }
