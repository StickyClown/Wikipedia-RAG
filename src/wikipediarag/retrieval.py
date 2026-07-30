from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.answerability import decide_answerability, is_insufficient
from wikipediarag.config import Settings, get_settings
from wikipediarag.embedding import cosine, normalize_for_embedding
from wikipediarag.model_client import embeddings
from wikipediarag.model_client import rerank as gateway_rerank
from wikipediarag.model_registry import get_model_registry
from wikipediarag.repository import fetch_chunks_for_dense_scan, insert_retrieval_event
from wikipediarag.retrieval_contract import validate_active_retrieval_contract
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import Evidence, RetrievalResult
from wikipediarag.search_index import bm25_search, dense_search

QUERY_EMBEDDING_INSTRUCTION = (
    "Represent this query for retrieving factual answers from the local Russian Wikipedia corpus."
)
NEGATIVE_EVIDENCE_POLICY_VERSION = "explicit_negative_title_v1"
_NEGATIVE_TITLE_MARKERS = (
    "не используй",
    "не использовать",
    "исключи",
    "исключить",
    "отвлекающий",
    "отвлекающего",
    "дистрактор",
    "distractor",
    "ignore",
    "do not use",
    "exclude",
)
_QUOTED_TITLE_RE = re.compile(r"[«\"“]([^»\"”]{2,160})[»\"”]")


async def retrieve(
    conn: AsyncConnection,
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    query_run_id: str | None,
    trace_id: str,
    settings: Settings | None = None,
    top_k: int | None = None,
    profile_name: str | None = None,
    profile_overrides: dict[str, Any] | None = None,
    profile: RetrievalProfile | None = None,
) -> RetrievalResult:
    resolved = settings or get_settings()
    profile = profile or get_retrieval_profile(profile_name, resolved, profile_overrides)
    started = time.perf_counter()
    timings_ms: dict[str, int] = {}
    active_contract = await validate_active_retrieval_contract(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        profile=profile,
        retrieval_overrides=profile_overrides,
        settings=resolved,
    )
    read_alias = active_contract.read_alias
    normalized_query = " ".join(query.split())
    requested_top_k = top_k or profile.retrieval.top_k

    tasks: list[asyncio.Task[tuple[str, list[dict[str, Any]], dict[str, int]]]] = []
    if profile.retrieval.bm25:
        tasks.append(
            asyncio.create_task(
                _run_bm25_stage(
                    normalized_query,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    top_k=profile.retrieval.bm25_top_k,
                    settings=resolved,
                    read_alias=read_alias,
                    strict=profile.requires_real_provider,
                ),
            )
        )
    if profile.retrieval.dense:
        tasks.append(
            asyncio.create_task(
                _run_dense_stage(
                    conn,
                    normalized_query,
                    tenant_id,
                    knowledge_base_id,
                    resolved,
                    profile,
                    top_k=profile.retrieval.dense_top_k,
                    read_alias=read_alias,
                )
            )
        )
    results = await asyncio.gather(*tasks)
    result_sets = {label: candidates for label, candidates, _timing in results}
    for _label, _candidates, timing in results:
        timings_ms.update(timing)

    fusion_started = time.perf_counter()
    if profile.retrieval.fusion == "rrf" and len(result_sets) > 1:
        fused = rrf_fuse(result_sets, top_k=profile.retrieval.fusion_top_k)
    else:
        fused = _merge_without_fusion(result_sets, top_k=profile.retrieval.fusion_top_k)
    timings_ms["fusion"] = _elapsed_ms(fusion_started)

    rerank_started = time.perf_counter()
    reranked = (
        await rerank(normalized_query, fused, resolved, profile, top_k=profile.retrieval.rerank_top_k)
        if profile.retrieval.rerank
        else fused[: profile.retrieval.rerank_top_k]
    )
    timings_ms["rerank"] = _elapsed_ms(rerank_started)
    context_started = time.perf_counter()
    selected, policy_events = postprocess_candidates(reranked, profile, requested_top_k, query=normalized_query)
    timings_ms["context"] = _elapsed_ms(context_started)
    timings_ms["retrieval_total"] = _elapsed_ms(started)
    events = build_stage_events(
        query=query,
        normalized_query=normalized_query,
        profile=profile,
        read_alias=read_alias,
        result_sets=result_sets,
        fused=fused,
        reranked=reranked,
        selected=selected,
        policy_events=policy_events,
        latency_ms=timings_ms["retrieval_total"],
        timings_ms=timings_ms,
        contract=active_contract.event_payload(),
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
            metadata=dict(item.get("metadata", {})),
        )
        for index, item in enumerate(selected, start=1)
    ]
    answerability = decide_answerability(query, evidence, profile)
    events.append(
        {
            "stage": "answerability",
            "decision": answerability.model_dump(mode="json"),
        }
    )
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
    return RetrievalResult(
        query=query,
        trace_id=trace_id,
        evidence=evidence,
        events=events,
        insufficient_evidence=is_insufficient(answerability),
        answerability=answerability,
        index_contract_id=active_contract.index_contract_id,
        run_contract_id=active_contract.run_contract_id,
    )


async def _run_bm25_stage(
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings,
    read_alias: str,
    strict: bool,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    started = time.perf_counter()
    candidates = await asyncio.to_thread(
        _safe_bm25,
        query,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        top_k=top_k,
        settings=settings,
        read_alias=read_alias,
        strict=strict,
    )
    return "bm25", candidates, {"bm25": _elapsed_ms(started)}


def _safe_bm25(
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings,
    read_alias: str,
    strict: bool,
) -> list[dict[str, Any]]:
    try:
        candidates = bm25_search(
            query,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            top_k=top_k,
            settings=settings,
            read_alias=read_alias,
        )
    except Exception:
        if strict:
            raise
        return []
    for rank, candidate in enumerate(candidates, start=1):
        candidate["ranks"]["bm25"] = rank
    return candidates


async def _run_dense_stage(
    conn: AsyncConnection,
    query: str,
    tenant_id: str,
    knowledge_base_id: str,
    settings: Settings,
    profile: RetrievalProfile,
    *,
    top_k: int,
    read_alias: str,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    candidates, timings = await dense_search_profile(
        conn,
        query,
        tenant_id,
        knowledge_base_id,
        settings,
        profile,
        top_k=top_k,
        read_alias=read_alias,
    )
    return "dense", candidates, timings


async def dense_search_profile(
    conn: AsyncConnection,
    query: str,
    tenant_id: str,
    knowledge_base_id: str,
    settings: Settings,
    profile: RetrievalProfile,
    *,
    top_k: int,
    read_alias: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    total_started = time.perf_counter()
    timings_ms: dict[str, int] = {}
    registry = get_model_registry(settings)
    alias = profile.model_aliases.embed
    model = registry.require(alias, "embedding")
    dimensions = int(model.dimensions or profile.embedding_dimensions(settings.embedding_dimensions))
    embedding_started = time.perf_counter()
    vectors, _usage = await embeddings(
        [query],
        settings,
        alias=alias,
        dimensions=dimensions,
        query_instruction=QUERY_EMBEDDING_INSTRUCTION,
    )
    timings_ms["dense_embedding"] = _elapsed_ms(embedding_started)
    query_vector = [float(value) for value in vectors[0]]
    if len(query_vector) != dimensions:
        raise ValueError(f"query embedding returned {len(query_vector)} dimensions, expected {dimensions}")
    search_started = time.perf_counter()
    try:
        candidates = await asyncio.to_thread(
            dense_search,
            query_vector,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            top_k=top_k,
            settings=settings,
            read_alias=read_alias,
        )
    except Exception:
        if profile.requires_real_provider:
            raise
        candidates = await dense_search_db(conn, query_vector, tenant_id, knowledge_base_id, top_k=top_k)
    timings_ms["dense_search"] = _elapsed_ms(search_started)
    for rank, candidate in enumerate(candidates, start=1):
        candidate["ranks"]["dense"] = rank
    timings_ms["dense_total"] = _elapsed_ms(total_started)
    return candidates, timings_ms


async def dense_search_db(
    conn: AsyncConnection,
    query_vector: list[float],
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
) -> list[dict[str, Any]]:
    rows = await fetch_chunks_for_dense_scan(conn, tenant_id, knowledge_base_id, limit=50000)
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
                "metadata": dict(row.get("metadata") or {}),
            }
        )
    candidates.sort(key=lambda item: item["scores"]["dense"], reverse=True)
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


def _merge_without_fusion(result_sets: dict[str, list[dict[str, Any]]], top_k: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for candidates in result_sets.values():
        for candidate in candidates:
            stored = merged.setdefault(candidate["chunk_id"], {**candidate, "scores": {}, "ranks": {}})
            stored["scores"].update(candidate.get("scores", {}))
            stored["ranks"].update(candidate.get("ranks", {}))
    return list(merged.values())[:top_k]


async def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    settings: Settings,
    profile: RetrievalProfile,
    top_k: int,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    documents = [f"{item['title']}\n{' / '.join(item['section_path'])}\n{item['content']}" for item in candidates]
    try:
        payload = await gateway_rerank(
            query,
            documents,
            settings,
            alias=profile.model_aliases.rerank,
            top_n=min(top_k, len(documents)),
        )
        for result in payload.get("results", []):
            index = int(result["index"])
            candidates[index]["scores"]["rerank"] = float(result["relevance_score"])
            candidates[index]["metadata"] = {
                **dict(candidates[index].get("metadata") or {}),
                "rerank_provider": payload.get("provider"),
            }
    except Exception:
        if profile.requires_real_provider:
            raise
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


def postprocess_candidates(
    candidates: list[dict[str, Any]],
    profile: RetrievalProfile,
    requested_top_k: int,
    *,
    query: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    page_counts: dict[int, int] = {}
    seen_hashes: set[str] = set()
    negative_titles = extract_explicit_negative_titles(query)
    max_evidence = min(requested_top_k, profile.postprocess.final_evidence_max)
    token_budget = profile.postprocess.max_context_tokens
    used_tokens = 0
    for candidate in candidates:
        matched_negative_title = _matched_negative_title(candidate, negative_titles)
        if matched_negative_title:
            events.append(
                {
                    "stage": "policy",
                    "decision": "dropped",
                    "chunk_id": candidate["chunk_id"],
                    "reason": "EXPLICIT_NEGATIVE_TITLE",
                    "negative_evidence_policy_version": NEGATIVE_EVIDENCE_POLICY_VERSION,
                    "matched_negative_title": matched_negative_title,
                }
            )
            continue
        metadata = dict(candidate.get("metadata") or {})
        content_hash = str(metadata.get("content_hash") or candidate.get("chunk_id"))
        if profile.postprocess.dedup and content_hash in seen_hashes:
            events.append(
                {
                    "stage": "policy",
                    "decision": "dropped",
                    "chunk_id": candidate["chunk_id"],
                    "reason": "NEAR_DUPLICATE",
                }
            )
            continue
        page_id = int(candidate.get("page_id") or 0)
        page_counts[page_id] = page_counts.get(page_id, 0) + 1
        if page_counts[page_id] > profile.postprocess.page_quota:
            events.append(
                {
                    "stage": "policy",
                    "decision": "dropped",
                    "chunk_id": candidate["chunk_id"],
                    "reason": "PAGE_QUOTA",
                }
            )
            continue
        content = str(candidate["content"])
        if profile.postprocess.parent_expansion in {"selective", "always"}:
            parent_text = str(metadata.get("parent_text") or "")
            should_expand_parent = (
                profile.postprocess.parent_expansion == "always"
                or len(content.split()) < profile.chunking.child_tokens_min
            )
            if parent_text and should_expand_parent:
                content = parent_text
                metadata["parent_expanded"] = True
        candidate_tokens = len(content.split())
        if profile.postprocess.context_packing == "token_budget" and used_tokens + candidate_tokens > token_budget:
            events.append(
                {
                    "stage": "context",
                    "decision": "dropped",
                    "chunk_id": candidate["chunk_id"],
                    "reason": "TOKEN_BUDGET",
                }
            )
            continue
        seen_hashes.add(content_hash)
        used_tokens += candidate_tokens
        selected.append({**candidate, "content": content, "metadata": metadata})
        events.append(
            {
                "stage": "context",
                "decision": "selected",
                "chunk_id": candidate["chunk_id"],
                "tokens": candidate_tokens,
            }
        )
        if len(selected) >= max_evidence:
            break
    return selected, events


def extract_explicit_negative_titles(query: str) -> set[str]:
    normalized_query = query.casefold()
    if not any(marker in normalized_query for marker in _NEGATIVE_TITLE_MARKERS):
        return set()
    titles: set[str] = set()
    for match in _QUOTED_TITLE_RE.finditer(query):
        clause = _quote_clause(query, match.start(), match.end()).casefold()
        if any(marker in clause for marker in _NEGATIVE_TITLE_MARKERS):
            normalized_title = normalize_for_embedding(match.group(1))
            if normalized_title:
                titles.add(normalized_title)
    return titles


def _quote_clause(query: str, start: int, end: int) -> str:
    left = start
    while left > 0 and query[left - 1] not in ".!?;\n\r":
        left -= 1
    right = end
    while right < len(query) and query[right] not in ".!?;\n\r":
        right += 1
    return query[left:right]


def _matched_negative_title(candidate: dict[str, Any], negative_titles: set[str]) -> str | None:
    if not negative_titles:
        return None
    for alias in _candidate_title_aliases(candidate):
        normalized_alias = normalize_for_embedding(alias)
        if normalized_alias in negative_titles:
            return normalized_alias
    return None


def _candidate_title_aliases(candidate: dict[str, Any]) -> list[str]:
    metadata = dict(candidate.get("metadata") or {})
    aliases: list[str] = [str(candidate.get("title") or "")]
    aliases.extend(str(item) for item in candidate.get("section_path") or [] if isinstance(item, str))
    for key in ("redirect_title", "redirect_source_title", "alias_title", "matched_title"):
        value = metadata.get(key)
        if isinstance(value, str):
            aliases.append(value)
    raw_aliases = metadata.get("aliases")
    if isinstance(raw_aliases, list):
        aliases.extend(str(value) for value in raw_aliases if isinstance(value, str))
    return aliases


def build_stage_events(
    *,
    query: str,
    normalized_query: str,
    profile: RetrievalProfile,
    read_alias: str,
    result_sets: dict[str, list[dict[str, Any]]],
    fused: list[dict[str, Any]],
    reranked: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    policy_events: list[dict[str, Any]],
    latency_ms: int,
    timings_ms: dict[str, int] | None = None,
    contract: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    timings = dict(timings_ms or {})
    contract_payload = dict(contract or {})
    events: list[dict[str, Any]] = [
        {
            "stage": "profile",
            "active_profile": profile.name,
            "source": profile.source,
            "read_alias": read_alias,
            **contract_payload,
            "model_aliases": profile.model_aliases.model_dump(),
            "retrieval": profile.retrieval.model_dump(),
            "postprocess": profile.postprocess.model_dump(),
        },
        {
            "stage": "query",
            "original_query": query,
            "normalized_query": normalized_query,
            "rewritten_query": None,
        },
    ]
    for stage, candidates in result_sets.items():
        events.append(
            {
                "stage": stage,
                "count": len(candidates),
                "latency_ms": timings.get("dense_total" if stage == "dense" else stage, 0),
                "candidates": [_candidate_debug(item) for item in candidates[:20]],
                "top": [item["chunk_id"] for item in candidates[:5]],
            }
        )
    events.append(
        {
            "stage": "rrf",
            "count": len(fused),
            "latency_ms": timings.get("fusion", 0),
            "candidates": [_candidate_debug(item) for item in fused[:20]],
            "top": [item["chunk_id"] for item in fused[:5]],
        }
    )
    events.append(
        {
            "stage": "rerank",
            "count": len(reranked),
            "latency_ms": timings.get("rerank", 0),
            "candidates": [_candidate_debug(item) for item in reranked[:20]],
            "top": [item["chunk_id"] for item in reranked[:5]],
        }
    )
    events.extend(policy_events)
    events.append(
        {
            "stage": "context",
            "count": len(selected),
            "candidates": [_candidate_debug(item) for item in selected],
            "latency_ms": latency_ms,
            "stage_latency_ms": timings.get("context", 0),
        }
    )
    events.append({"stage": "timings", "timings_ms": timings})
    return events


def _candidate_debug(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": item["chunk_id"],
        "title": item["title"],
        "source_url": item.get("source_url"),
        "scores": item.get("scores", {}),
        "ranks": item.get("ranks", {}),
        "metadata": item.get("metadata", {}),
    }


def apply_page_quota(candidates: list[dict[str, Any]], max_per_page: int) -> list[dict[str, Any]]:
    counts: dict[int, int] = {}
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        page_id = int(candidate.get("page_id") or 0)
        counts[page_id] = counts.get(page_id, 0) + 1
        if counts[page_id] <= max_per_page:
            selected.append(candidate)
    return selected


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
