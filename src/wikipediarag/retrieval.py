from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.config import Settings, get_settings
from wikipediarag.embedding import cosine, embed_text, normalize_for_embedding
from wikipediarag.repository import fetch_chunks_for_dense_scan, insert_retrieval_event
from wikipediarag.schemas import Evidence, RetrievalResult
from wikipediarag.search_index import bm25_search


async def retrieve(
    conn: AsyncConnection,
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    query_run_id: str | None,
    trace_id: str,
    settings: Settings | None = None,
    top_k: int = 10,
) -> RetrievalResult:
    resolved = settings or get_settings()
    bm25_task = asyncio.to_thread(
        _safe_bm25,
        query,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        top_k=60,
        settings=resolved,
    )
    dense_task = dense_search_db(conn, query, tenant_id, knowledge_base_id, resolved, top_k=60)
    bm25, dense = await asyncio.gather(bm25_task, dense_task)
    fused = rrf_fuse({"bm25": bm25, "dense": dense}, top_k=40)
    reranked = await rerank(query, fused, resolved, top_k=20)
    selected = apply_page_quota(reranked, max_per_page=3)[:top_k]
    events = [
        {"stage": "bm25", "count": len(bm25), "top": [item["chunk_id"] for item in bm25[:5]]},
        {"stage": "dense", "count": len(dense), "top": [item["chunk_id"] for item in dense[:5]]},
        {"stage": "rrf", "count": len(fused), "top": [item["chunk_id"] for item in fused[:5]]},
        {
            "stage": "rerank",
            "count": len(reranked),
            "top": [item["chunk_id"] for item in reranked[:5]],
        },
        {
            "stage": "context",
            "count": len(selected),
            "top": [item["chunk_id"] for item in selected],
        },
    ]
    for event in events:
        await insert_retrieval_event(
            conn,
            tenant_id=tenant_id,
            query_run_id=query_run_id,
            trace_id=trace_id,
            event_type="retrieval_stage",
            stage=str(event["stage"]),
            payload=event,
        )
    evidence = [
        Evidence(
            evidence_id=f"S{index}",
            chunk_id=item["chunk_id"],
            title=item["title"],
            section_path=list(item["section_path"]),
            content=item["content"],
            source_url=item["source_url"],
            scores=dict(item.get("scores", {})),
            ranks=dict(item.get("ranks", {})),
        )
        for index, item in enumerate(selected, start=1)
    ]
    return RetrievalResult(
        query=query,
        trace_id=trace_id,
        evidence=evidence,
        events=events,
        insufficient_evidence=len(evidence) < 2,
    )


def _safe_bm25(
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings,
) -> list[dict[str, Any]]:
    try:
        candidates = bm25_search(
            query,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            top_k=top_k,
            settings=settings,
        )
    except Exception:
        return []
    for rank, candidate in enumerate(candidates, start=1):
        candidate["ranks"]["bm25"] = rank
    return candidates


async def dense_search_db(
    conn: AsyncConnection,
    query: str,
    tenant_id: str,
    knowledge_base_id: str,
    settings: Settings,
    top_k: int,
) -> list[dict[str, Any]]:
    query_vector = embed_text(query, settings.embedding_dimensions)
    rows = await fetch_chunks_for_dense_scan(conn, tenant_id, knowledge_base_id)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        embedding = row["embedding"]
        if not isinstance(embedding, list):
            continue
        score = cosine(query_vector, [float(value) for value in embedding])
        if score <= 0:
            continue
        candidates.append(
            {
                "chunk_id": row["id"],
                "document_id": row["document_id"],
                "page_id": row["page_id"],
                "title": row["title"],
                "section_path": list(row["section_path"] or []),
                "content": row["content"],
                "source_url": row["source_url"],
                "scores": {"dense": score},
                "ranks": {},
            }
        )
    candidates.sort(key=lambda item: item["scores"]["dense"], reverse=True)
    for rank, candidate in enumerate(candidates[:top_k], start=1):
        candidate["ranks"]["dense"] = rank
    return candidates[:top_k]


def rrf_fuse(result_sets: dict[str, list[dict[str, Any]]], top_k: int, k: int = 60) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for stage, candidates in result_sets.items():
        for rank, candidate in enumerate(candidates, start=1):
            stored = by_id.setdefault(candidate["chunk_id"], {**candidate, "scores": {}, "ranks": {}})
            stored["scores"].update(candidate.get("scores", {}))
            stored["ranks"].update(candidate.get("ranks", {}))
            stored["scores"][f"rrf_{stage}"] = 1.0 / (k + rank)
            stored["scores"]["rrf_total"] = stored["scores"].get("rrf_total", 0.0) + 1.0 / (k + rank)
    fused = list(by_id.values())
    fused.sort(key=lambda item: item["scores"].get("rrf_total", 0.0), reverse=True)
    return fused[:top_k]


async def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    settings: Settings,
    top_k: int,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    documents = [f"{item['title']}\n{' / '.join(item['section_path'])}\n{item['content']}" for item in candidates]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{settings.model_gateway_url.rstrip('/')}/v1/rerank",
                json={
                    "model": "rerank_default",
                    "query": query,
                    "documents": documents,
                    "top_n": len(documents),
                },
            )
            response.raise_for_status()
            scores = response.json()["results"]
        for result in scores:
            index = int(result["index"])
            candidates[index]["scores"]["rerank"] = float(result["relevance_score"])
    except Exception:
        query_terms = set(normalize_for_embedding(query).split())
        for item in candidates:
            doc_terms = set(normalize_for_embedding(item["content"]).split())
            item["scores"]["rerank"] = len(query_terms & doc_terms) / max(len(query_terms), 1)
    candidates.sort(
        key=lambda item: (item["scores"].get("rerank", 0.0), item["scores"].get("rrf_total", 0.0)),
        reverse=True,
    )
    for rank, candidate in enumerate(candidates[:top_k], start=1):
        candidate["ranks"]["rerank"] = rank
    return candidates[:top_k]


def apply_page_quota(candidates: list[dict[str, Any]], max_per_page: int) -> list[dict[str, Any]]:
    counts: dict[int, int] = {}
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        page_id = int(candidate.get("page_id") or 0)
        counts[page_id] = counts.get(page_id, 0) + 1
        if counts[page_id] <= max_per_page:
            selected.append(candidate)
    return selected
