from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk

from wikipediarag.config import Settings, get_settings
from wikipediarag.document_access import document_access_filter
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
) -> dict[str, str]:
    suffix = stable_hash([source_type, snapshot_id, retrieval_profile, embedding_alias, embedding_dimensions], 16)
    return {
        "version_id": f"{source_type}:{snapshot_id}:{retrieval_profile}:{embedding_alias}:{embedding_dimensions}",
        "physical": f"wiki-chunks-{suffix}",
        "read_alias": f"wiki-chunks-read-{suffix}",
        "write_alias": f"wiki-chunks-write-{suffix}",
    }


def get_client(settings: Settings | None = None) -> OpenSearch:
    resolved = settings or get_settings()
    return OpenSearch(hosts=[resolved.opensearch_url], verify_certs=False, ssl_show_warn=False)


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
                        "tenant_id": {"type": "keyword"},
                        "knowledge_base_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "document_version_id": {"type": "keyword"},
                        "chunk_id": {"type": "keyword"},
                        "page_id": {"type": "long"},
                        "revision_id": {"type": "long"},
                        "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
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
    tenant_id: str,
    knowledge_base_id: str,
    settings: Settings | None = None,
    write_alias: str = WRITE_ALIAS,
    dimensions: int | None = None,
    physical_index: str = PHYSICAL_INDEX,
    read_alias: str = READ_ALIAS,
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
            "_id": chunk.id,
            "_source": {
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "document_id": chunk.document_id,
                "document_version_id": str(chunk.metadata.get("document_version_id") or ""),
                "chunk_id": chunk.id,
                "page_id": chunk.page_id,
                "revision_id": chunk.revision_id,
                "title": chunk.title,
                "section_path_text": " / ".join(chunk.section_path),
                "content": chunk.content,
                "embedding": chunk.embedding,
                "source_uri": chunk.source_uri,
                "source_url": chunk.source_url,
                "prev_chunk_id": chunk.prev_chunk_id,
                "next_chunk_id": chunk.next_chunk_id,
                "content_hash": chunk.content_hash,
                "metadata": chunk.metadata,
            },
        }
        for chunk in chunks
    ]
    if not actions:
        return 0
    indexed, _ = bulk(client, actions, refresh=True)
    return int(indexed)


def bm25_search(
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved = settings or get_settings()
    ensure_index(resolved)
    client = get_client(resolved)
    filter_clauses = [
        {"term": {"tenant_id": tenant_id}},
        {"term": {"knowledge_base_id": knowledge_base_id}},
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
                                "fields": ["title^3", "section_path_text^2", "content"],
                            }
                        }
                    ],
                }
            },
        },
    )
    return [_hit_to_candidate(hit, "bm25") for hit in response["hits"]["hits"]]


def dense_search(
    query_vector: list[float],
    *,
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved = settings or get_settings()
    client = get_client(resolved)
    filter_clauses = [
        {"term": {"tenant_id": tenant_id}},
        {"term": {"knowledge_base_id": knowledge_base_id}},
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
    return [_hit_to_candidate(hit, "dense") for hit in response["hits"]["hits"]]


def delete_document_chunks(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
) -> int:
    resolved = settings or get_settings()
    client = get_client(resolved)
    response = client.delete_by_query(
        index=read_alias,
        body={
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"knowledge_base_id": knowledge_base_id}},
                        {"term": {"document_id": document_id}},
                    ]
                }
            }
        },
        conflicts="proceed",
        refresh=True,
    )
    return int(response.get("deleted") or 0)


def delete_document_version_chunks(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_version_id: str,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
) -> int:
    resolved = settings or get_settings()
    client = get_client(resolved)
    response = client.delete_by_query(
        index=read_alias,
        body={
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"knowledge_base_id": knowledge_base_id}},
                        {"term": {"document_version_id": document_version_id}},
                    ]
                }
            }
        },
        conflicts="proceed",
        refresh=True,
    )
    return int(response.get("deleted") or 0)


def update_document_access(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_access: dict[str, Any],
    origin: str | None = None,
    settings: Settings | None = None,
    read_alias: str = READ_ALIAS,
) -> int:
    resolved = settings or get_settings()
    client = get_client(resolved)
    script_source = "if (ctx._source.metadata == null) { ctx._source.metadata = new HashMap(); } "
    script_source += "ctx._source.metadata.document_access = params.document_access;"
    params: dict[str, Any] = {"document_access": document_access}
    if origin is not None:
        script_source += "ctx._source.metadata.document_access_origin = params.origin;"
        params["origin"] = origin
    response = client.update_by_query(
        index=read_alias,
        body={
            "script": {
                "source": script_source,
                "lang": "painless",
                "params": params,
            },
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"knowledge_base_id": knowledge_base_id}},
                        {"term": {"document_id": document_id}},
                    ]
                }
            },
        },
        conflicts="proceed",
        refresh=True,
    )
    return int(response.get("updated") or 0)


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
    clauses.extend(document_access_filter(filters.get("document_access_scope")))
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
