from __future__ import annotations

import asyncio
import re
import time
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.answerability import decide_answerability, is_insufficient
from wikipediarag.config import Settings, get_settings
from wikipediarag.document_access import DocumentAccessScope, is_document_visible
from wikipediarag.embedding import cosine, normalize_for_embedding
from wikipediarag.ids import stable_hash
from wikipediarag.model_client import embeddings
from wikipediarag.model_client import rerank as gateway_rerank
from wikipediarag.model_registry import get_model_registry
from wikipediarag.observability import retrieval_span, safe_error_code
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
    query_context: dict[str, Any] | None = None,
    search_filters: dict[str, Any] | None = None,
    persist_events: bool = True,
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
    active_query_context = query_context or make_query_context(
        query=normalized_query,
        transform_id="tr.normalization.1",
        subquery_id="sq.primary.1",
        query_role="primary",
        transform_type="normalization",
    )
    requested_top_k = top_k or profile.retrieval.top_k

    tasks: list[asyncio.Task[tuple[str, list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]]] = []
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
                    filters=_filters_for_kb(search_filters, knowledge_base_id),
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
                    filters=_filters_for_kb(search_filters, knowledge_base_id),
                )
            )
        )
    results = await asyncio.gather(*tasks)
    result_sets = {label: candidates for label, candidates, _timing, _model_events in results}
    _tag_candidate_sets(result_sets.values(), active_query_context)
    result_sets_snapshot = {label: _snapshot_candidates(candidates) for label, candidates in result_sets.items()}
    model_events = [event for _label, _candidates, _timing, events in results for event in events]
    for _label, _candidates, timing, _model_events in results:
        timings_ms.update(timing)

    fusion_started = time.perf_counter()
    with retrieval_span("rrf", {"query_run_id": query_run_id or "", "trace_id": trace_id}):
        if profile.retrieval.fusion == "rrf" and len(result_sets) > 1:
            fused = rrf_fuse(result_sets, top_k=profile.retrieval.fusion_top_k)
        else:
            fused = _merge_without_fusion(result_sets, top_k=profile.retrieval.fusion_top_k)
    timings_ms["fusion"] = _elapsed_ms(fusion_started)
    fused_snapshot = _snapshot_candidates(fused)

    rerank_started = time.perf_counter()
    if profile.retrieval.rerank:
        reranked, rerank_model_event = await rerank(
            normalized_query, fused, resolved, profile, top_k=profile.retrieval.rerank_top_k
        )
    else:
        reranked = fused[: profile.retrieval.rerank_top_k]
        rerank_model_event = None
    timings_ms["rerank"] = _elapsed_ms(rerank_started)
    reranked_snapshot = _snapshot_candidates(reranked)
    context_started = time.perf_counter()
    selected, policy_events = postprocess_candidates(reranked, profile, requested_top_k, query=normalized_query)
    timings_ms["context"] = _elapsed_ms(context_started)
    timings_ms["retrieval_total"] = _elapsed_ms(started)
    events = build_stage_events(
        query=query,
        normalized_query=normalized_query,
        profile=profile,
        read_alias=read_alias,
        result_sets=result_sets_snapshot,
        fused=fused_snapshot,
        reranked=reranked_snapshot,
        selected=selected,
        policy_events=policy_events,
        model_events=model_events,
        rerank_model_event=rerank_model_event,
        latency_ms=timings_ms["retrieval_total"],
        timings_ms=timings_ms,
        contract=active_contract.event_payload(),
        query_context=active_query_context,
    )
    evidence = [
        Evidence(
            evidence_id=f"S{index}",
            chunk_id=item["chunk_id"],
            knowledge_base_id=str(item.get("knowledge_base_id") or knowledge_base_id),
            title=item["title"],
            section_path=list(item["section_path"]),
            content=item["content"],
            source_url=item["source_url"],
            scores=dict(item.get("scores", {})),
            ranks=dict(item.get("ranks", {})),
            metadata=_evidence_metadata(item, document_version_id=""),
        )
        for index, item in enumerate(selected, start=1)
    ]
    answerability = decide_answerability(query, evidence, profile)
    events.append(_answerability_event(answerability, active_query_context))
    if persist_events:
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


async def retrieve_multi(
    conn: AsyncConnection,
    query: str,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
    query_run_id: str | None,
    trace_id: str,
    settings: Settings | None = None,
    top_k: int | None = None,
    profile_name: str | None = None,
    profile_overrides: dict[str, Any] | None = None,
    profile: RetrievalProfile | None = None,
    query_context: dict[str, Any] | None = None,
    search_filters: dict[str, Any] | None = None,
    persist_events: bool = True,
) -> RetrievalResult:
    if len(knowledge_base_ids) == 1:
        return await retrieve(
            conn,
            query,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_ids[0],
            query_run_id=query_run_id,
            trace_id=trace_id,
            settings=settings,
            top_k=top_k,
            profile_name=profile_name,
            profile_overrides=profile_overrides,
            profile=profile,
            query_context=query_context,
            search_filters=search_filters,
            persist_events=persist_events,
        )

    resolved = settings or get_settings()
    active_profile = profile or get_retrieval_profile(profile_name, resolved, profile_overrides)
    started = time.perf_counter()
    timings_ms: dict[str, int] = {}
    active_contracts = [
        await validate_active_retrieval_contract(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            profile=active_profile,
            retrieval_overrides=profile_overrides,
            settings=resolved,
        )
        for kb_id in knowledge_base_ids
    ]
    contract_by_kb = dict(zip(knowledge_base_ids, active_contracts, strict=True))
    normalized_query = " ".join(query.split())
    active_query_context = query_context or make_query_context(
        query=normalized_query,
        transform_id="tr.normalization.1",
        subquery_id="sq.primary.1",
        query_role="primary",
        transform_type="normalization",
    )
    requested_top_k = top_k or active_profile.retrieval.top_k

    tasks: list[asyncio.Task[tuple[str, list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]]] = []
    dense_stage_inputs: list[tuple[str, str]] = []
    for kb_id in knowledge_base_ids:
        read_alias = contract_by_kb[kb_id].read_alias
        if active_profile.retrieval.bm25:
            tasks.append(
                asyncio.create_task(
                    _run_scoped_bm25_stage(
                        normalized_query,
                        tenant_id=tenant_id,
                        knowledge_base_id=kb_id,
                        top_k=active_profile.retrieval.bm25_top_k,
                        settings=resolved,
                        read_alias=read_alias,
                        strict=active_profile.requires_real_provider,
                        filters=_filters_for_kb(search_filters, kb_id),
                    )
                )
            )
        if active_profile.retrieval.dense:
            dense_stage_inputs.append((kb_id, read_alias))
    raw_results = list(await asyncio.gather(*tasks))
    for kb_id, read_alias in dense_stage_inputs:
        raw_results.append(
            await _run_scoped_dense_stage(
                conn,
                normalized_query,
                tenant_id,
                kb_id,
                resolved,
                active_profile,
                top_k=active_profile.retrieval.dense_top_k,
                read_alias=read_alias,
                filters=_filters_for_kb(search_filters, kb_id),
            )
        )
    result_sets: dict[str, list[dict[str, Any]]] = {}
    model_events = [event for _label, _candidates, _timing, events in raw_results for event in events]
    for label, candidates, timing, _events in raw_results:
        _stage, _separator, kb_id = label.partition(":")
        result_sets[label] = candidates
        for timing_key, timing_value in timing.items():
            base_key = "dense" if timing_key == "dense_total" else timing_key
            timings_ms[base_key] = timings_ms.get(base_key, 0) + timing_value
            if kb_id:
                timings_ms[f"{timing_key}:{kb_id}"] = timing_value
    _tag_candidate_sets(result_sets.values(), active_query_context)
    result_sets_snapshot = {label: _snapshot_candidates(candidates) for label, candidates in result_sets.items()}

    fusion_started = time.perf_counter()
    with retrieval_span("rrf", {"query_run_id": query_run_id or "", "trace_id": trace_id}):
        if active_profile.retrieval.fusion == "rrf" and len(result_sets) > 1:
            fused = rrf_fuse(result_sets, top_k=active_profile.retrieval.fusion_top_k)
        else:
            fused = _merge_without_fusion(result_sets, top_k=active_profile.retrieval.fusion_top_k)
    fused_snapshot = _snapshot_candidates(fused)
    per_kb_cap = max(
        1, (active_profile.retrieval.rerank_top_k + len(knowledge_base_ids) - 1) // len(knowledge_base_ids)
    )
    before_kb_cap = fused
    fused = apply_knowledge_base_cap(fused, per_kb_cap)
    kb_cap_policy_events = _knowledge_base_cap_events(before_kb_cap, fused, per_kb_cap)
    timings_ms["fusion"] = _elapsed_ms(fusion_started)

    rerank_started = time.perf_counter()
    if active_profile.retrieval.rerank:
        reranked, rerank_model_event = await rerank(
            normalized_query, fused, resolved, active_profile, top_k=active_profile.retrieval.rerank_top_k
        )
    else:
        reranked = fused[: active_profile.retrieval.rerank_top_k]
        rerank_model_event = None
    timings_ms["rerank"] = _elapsed_ms(rerank_started)
    reranked_snapshot = _snapshot_candidates(reranked)
    context_started = time.perf_counter()
    selected, policy_events = postprocess_candidates(reranked, active_profile, requested_top_k, query=normalized_query)
    timings_ms["context"] = _elapsed_ms(context_started)
    timings_ms["retrieval_total"] = _elapsed_ms(started)
    index_contract_id = "multi:" + _stable_contract_scope(
        [contract.index_contract_id for contract in active_contracts],
    )
    run_contract_id = "multi:" + _stable_contract_scope([contract.run_contract_id for contract in active_contracts])
    events = build_stage_events(
        query=query,
        normalized_query=normalized_query,
        profile=active_profile,
        read_alias="multi",
        result_sets=result_sets_snapshot,
        fused=fused_snapshot,
        reranked=reranked_snapshot,
        selected=selected,
        policy_events=[*kb_cap_policy_events, *policy_events],
        model_events=model_events,
        rerank_model_event=rerank_model_event,
        latency_ms=timings_ms["retrieval_total"],
        timings_ms=timings_ms,
        contract={
            "knowledge_base_ids": knowledge_base_ids,
            "contracts": [
                {"knowledge_base_id": kb_id, **contract_by_kb[kb_id].event_payload()} for kb_id in knowledge_base_ids
            ],
            "index_contract_id": index_contract_id,
            "run_contract_id": run_contract_id,
        },
        query_context=active_query_context,
    )
    evidence = [
        Evidence(
            evidence_id=f"S{index}",
            chunk_id=item["chunk_id"],
            knowledge_base_id=str(item.get("knowledge_base_id") or ""),
            title=item["title"],
            section_path=list(item["section_path"]),
            content=item["content"],
            source_url=item["source_url"],
            scores=dict(item.get("scores", {})),
            ranks=dict(item.get("ranks", {})),
            metadata=_evidence_metadata(item, document_version_id=""),
        )
        for index, item in enumerate(selected, start=1)
    ]
    answerability = decide_answerability(query, evidence, active_profile)
    events.append(_answerability_event(answerability, active_query_context))
    if persist_events:
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
        index_contract_id=index_contract_id,
        run_contract_id=run_contract_id,
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
    filters: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    started = time.perf_counter()
    with retrieval_span("bm25", {"knowledge_base_id": knowledge_base_id, "top_k": top_k}):
        candidates = await asyncio.to_thread(
            _safe_bm25,
            query,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            top_k=top_k,
            settings=settings,
            read_alias=read_alias,
            strict=strict,
            filters=filters,
        )
    return "bm25", candidates, {"bm25": _elapsed_ms(started)}, []


async def _run_scoped_bm25_stage(
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings,
    read_alias: str,
    strict: bool,
    filters: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    _label, candidates, timings, events = await _run_bm25_stage(
        query,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        top_k=top_k,
        settings=settings,
        read_alias=read_alias,
        strict=strict,
        filters=filters,
    )
    return f"bm25:{knowledge_base_id}", candidates, timings, events


def _safe_bm25(
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    settings: Settings,
    read_alias: str,
    strict: bool,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        candidates = bm25_search(
            query,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            top_k=top_k,
            settings=settings,
            read_alias=read_alias,
            filters=filters,
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
    filters: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    candidates, timings, events = await dense_search_profile(
        conn,
        query,
        tenant_id,
        knowledge_base_id,
        settings,
        profile,
        top_k=top_k,
        read_alias=read_alias,
        filters=filters,
    )
    return "dense", candidates, timings, events


async def _run_scoped_dense_stage(
    conn: AsyncConnection,
    query: str,
    tenant_id: str,
    knowledge_base_id: str,
    settings: Settings,
    profile: RetrievalProfile,
    *,
    top_k: int,
    read_alias: str,
    filters: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    _label, candidates, timings, events = await _run_dense_stage(
        conn,
        query,
        tenant_id,
        knowledge_base_id,
        settings,
        profile,
        top_k=top_k,
        read_alias=read_alias,
        filters=filters,
    )
    return f"dense:{knowledge_base_id}", candidates, timings, events


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
    filters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    total_started = time.perf_counter()
    timings_ms: dict[str, int] = {}
    registry = get_model_registry(settings)
    alias = profile.model_aliases.embed
    model = registry.require(alias, "embedding")
    dimensions = int(model.dimensions or profile.embedding_dimensions(settings.embedding_dimensions))
    embedding_started = time.perf_counter()
    with retrieval_span("dense.embedding", {"knowledge_base_id": knowledge_base_id, "model_alias": alias}):
        vectors, usage = await embeddings(
            [query],
            settings,
            alias=alias,
            dimensions=dimensions,
            query_instruction=QUERY_EMBEDDING_INSTRUCTION,
        )
    timings_ms["dense_embedding"] = _elapsed_ms(embedding_started)
    model_events = [
        {
            "stage": "dense.embedding",
            "stable_stage": "dense.embedding",
            "operation": "embedding",
            "model_call": dict(usage.get("_gateway_metadata") or {}),
            "latency_ms": timings_ms["dense_embedding"],
        }
    ]
    query_vector = [float(value) for value in vectors[0]]
    if len(query_vector) != dimensions:
        raise ValueError(f"query embedding returned {len(query_vector)} dimensions, expected {dimensions}")
    search_started = time.perf_counter()
    try:
        with retrieval_span("dense.search", {"knowledge_base_id": knowledge_base_id, "top_k": top_k}):
            candidates = await asyncio.to_thread(
                dense_search,
                query_vector,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                top_k=top_k,
                settings=settings,
                read_alias=read_alias,
                filters=filters,
            )
    except Exception:
        if profile.requires_real_provider:
            raise
        candidates = await dense_search_db(
            conn,
            query_vector,
            tenant_id,
            knowledge_base_id,
            top_k=top_k,
            filters=filters,
        )
    timings_ms["dense_search"] = _elapsed_ms(search_started)
    for rank, candidate in enumerate(candidates, start=1):
        candidate["ranks"]["dense"] = rank
    timings_ms["dense_total"] = _elapsed_ms(total_started)
    return candidates, timings_ms, model_events


async def dense_search_db(
    conn: AsyncConnection,
    query_vector: list[float],
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = await fetch_chunks_for_dense_scan(conn, tenant_id, knowledge_base_id, limit=50000)
    access_scope = _document_access_scope_from_filters(filters)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not is_document_visible(dict(row.get("metadata") or {}), access_scope):
            continue
        embedding = row["embedding"]
        if not isinstance(embedding, list):
            continue
        score = cosine(query_vector, [float(value) for value in embedding])
        if score <= 0:
            continue
        candidates.append(
            {
                "chunk_id": row["id"],
                "knowledge_base_id": str(row.get("knowledge_base_id") or knowledge_base_id),
                "document_id": row["document_id"],
                "document_version_id": row.get("document_version_id"),
                "page_id": row["page_id"],
                "title": row["title"],
                "section_path": list(row["section_path"] or []),
                "content": row["content"],
                "source_uri": row["source_uri"],
                "source_url": row["source_url"],
                "locator": row.get("locator") or dict(row.get("metadata") or {}).get("locator", {}),
                "scores": {"dense": score},
                "ranks": {},
                "metadata": dict(row.get("metadata") or {}),
            }
        )
    candidates.sort(key=lambda item: item["scores"]["dense"], reverse=True)
    return candidates[:top_k]


def _filters_for_kb(filters: dict[str, Any] | None, knowledge_base_id: str) -> dict[str, Any] | None:
    if not filters:
        return None
    scoped = {key: value for key, value in filters.items() if key != "document_access_scopes"}
    scopes = filters.get("document_access_scopes")
    if isinstance(scopes, dict):
        scope = scopes.get(knowledge_base_id)
        if scope is not None:
            scoped["document_access_scope"] = scope
    return scoped


def _document_access_scope_from_filters(filters: dict[str, Any] | None) -> DocumentAccessScope | None:
    if not filters:
        return None
    scope = filters.get("document_access_scope")
    return scope if isinstance(scope, DocumentAccessScope) else None


def rrf_fuse(result_sets: dict[str, list[dict[str, Any]]], top_k: int, k: int = 60) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for stage, candidates in result_sets.items():
        for rank, candidate in enumerate(candidates, start=1):
            stored = by_id.setdefault(_candidate_scope_key(candidate), {**candidate, "scores": {}, "ranks": {}})
            stored["scores"].update(candidate.get("scores", {}))
            stored["ranks"].update(candidate.get("ranks", {}))
            stored["scores"][f"rrf_{stage}"] = 1.0 / (k + rank)
            stored["scores"]["rrf_total"] = stored["scores"].get("rrf_total", 0.0) + 1.0 / (k + rank)
    fused = list(by_id.values())
    fused.sort(key=lambda item: item["scores"].get("rrf_total", 0.0), reverse=True)
    for rank, candidate in enumerate(fused, start=1):
        candidate["ranks"]["fusion"] = rank
        candidate["scores"]["fusion"] = float(candidate["scores"].get("rrf_total", 0.0))
    return fused[:top_k]


def _merge_without_fusion(result_sets: dict[str, list[dict[str, Any]]], top_k: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for candidates in result_sets.values():
        for candidate in candidates:
            stored = merged.setdefault(_candidate_scope_key(candidate), {**candidate, "scores": {}, "ranks": {}})
            stored["scores"].update(candidate.get("scores", {}))
            stored["ranks"].update(candidate.get("ranks", {}))
    candidates = list(merged.values())[:top_k]
    for rank, candidate in enumerate(candidates, start=1):
        candidate.setdefault("ranks", {})["fusion"] = rank
    return candidates


async def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    settings: Settings,
    profile: RetrievalProfile,
    top_k: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not candidates:
        return [], None
    model_event: dict[str, Any] | None = None
    documents = [f"{item['title']}\n{' / '.join(item['section_path'])}\n{item['content']}" for item in candidates]
    try:
        with retrieval_span("rerank", {"model_alias": profile.model_aliases.rerank, "candidate_count": len(documents)}):
            payload = await gateway_rerank(
                query,
                documents,
                settings,
                alias=profile.model_aliases.rerank,
                top_n=min(top_k, len(documents)),
            )
        model_event = dict(payload.get("_gateway_metadata") or {})
        for result in payload.get("results", []):
            index = int(result["index"])
            candidates[index]["scores"]["rerank"] = float(result["relevance_score"])
            candidates[index]["metadata"] = {
                **dict(candidates[index].get("metadata") or {}),
                "rerank_provider": payload.get("provider"),
            }
    except Exception as exc:
        if profile.requires_real_provider:
            raise
        model_event = {
            "operation": "rerank",
            "model_alias": profile.model_aliases.rerank,
            "safe_error_code": safe_error_code(exc),
            "fallback": "deterministic_overlap",
        }
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
    return candidates[:top_k], model_event


def postprocess_candidates(
    candidates: list[dict[str, Any]],
    profile: RetrievalProfile,
    requested_top_k: int,
    *,
    query: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    page_counts: dict[tuple[str, int], int] = {}
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
                    **_decision_base(candidate),
                    "stage": "context_selection",
                    "decision": "dropped",
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
                    **_decision_base(candidate),
                    "stage": "context_selection",
                    "decision": "dropped",
                    "reason": "NEAR_DUPLICATE",
                    "content_hash": content_hash,
                }
            )
            continue
        page_key = (str(candidate.get("knowledge_base_id") or ""), int(candidate.get("page_id") or 0))
        page_counts[page_key] = page_counts.get(page_key, 0) + 1
        if page_counts[page_key] > profile.postprocess.page_quota:
            events.append(
                {
                    **_decision_base(candidate),
                    "stage": "context_selection",
                    "decision": "dropped",
                    "reason": "PAGE_QUOTA",
                    "page_quota": profile.postprocess.page_quota,
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
                events.append(
                    {
                        **_decision_base(candidate),
                        "stage": "context_selection",
                        "decision": "parent_expanded",
                        "reason": "PARENT_EXPANSION",
                        "parent_expansion": profile.postprocess.parent_expansion,
                    }
                )
        candidate_tokens = len(content.split())
        if profile.postprocess.context_packing == "token_budget" and used_tokens + candidate_tokens > token_budget:
            events.append(
                {
                    **_decision_base(candidate),
                    "stage": "context_selection",
                    "decision": "dropped",
                    "reason": "TOKEN_BUDGET",
                    "token_budget": token_budget,
                    "used_tokens": used_tokens,
                    "candidate_tokens": candidate_tokens,
                }
            )
            continue
        seen_hashes.add(content_hash)
        used_tokens += candidate_tokens
        final_rank = len(selected) + 1
        ranks = {**dict(candidate.get("ranks") or {}), "final": final_rank}
        query_context = dict(candidate.get("query_context") or {})
        if query_context:
            metadata["query_context"] = query_context
            metadata["subquery_id"] = query_context.get("subquery_id")
            metadata["transform_id"] = query_context.get("transform_id")
        selected_candidate = {**candidate, "content": content, "metadata": metadata, "ranks": ranks}
        selected.append(selected_candidate)
        events.append(
            {
                **_decision_base(selected_candidate),
                "stage": "context_selection",
                "decision": "selected",
                "reason": "SELECTED_FOR_CONTEXT",
                "tokens": candidate_tokens,
                "final_rank": final_rank,
            }
        )
        if len(selected) >= max_evidence:
            break
    return selected, events


def _decision_base(candidate: dict[str, Any]) -> dict[str, Any]:
    query_context = dict(candidate.get("query_context") or {})
    return {
        "chunk_id": candidate.get("chunk_id"),
        "document_id": candidate.get("document_id"),
        "knowledge_base_id": candidate.get("knowledge_base_id", ""),
        "page_id": candidate.get("page_id"),
        "subquery_id": candidate.get("subquery_id") or query_context.get("subquery_id"),
        "transform_id": candidate.get("transform_id") or query_context.get("transform_id"),
        "query_context": query_context,
        "scores": dict(candidate.get("scores") or {}),
        "ranks": dict(candidate.get("ranks") or {}),
    }


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
    model_events: list[dict[str, Any]] | None = None,
    rerank_model_event: dict[str, Any] | None = None,
    latency_ms: int,
    timings_ms: dict[str, int] | None = None,
    contract: dict[str, Any] | None = None,
    query_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    timings = dict(timings_ms or {})
    contract_payload = dict(contract or {})
    active_query_context = query_context or make_query_context(
        query=normalized_query,
        transform_id="tr.normalization.1",
        subquery_id="sq.primary.1",
        query_role="primary",
        transform_type="normalization",
    )
    query_refs = [query_ref_from_context(active_query_context, text=normalized_query, order=1)]
    events: list[dict[str, Any]] = [
        {
            "stage": "profile",
            "stable_stage": "query.run",
            "query_context": active_query_context,
            "active_profile": profile.name,
            "source": profile.source,
            "read_alias": read_alias,
            **contract_payload,
            "model_aliases": profile.model_aliases.model_dump(),
            "retrieval": profile.retrieval.model_dump(),
            "postprocess": profile.postprocess.model_dump(),
        },
        {
            "stage": "query_transform",
            "stable_stage": "query_transform",
            "query_context": active_query_context,
            "original_query": query,
            "normalized_query": normalized_query,
            "rewritten_query": None,
            "transforms": [
                {"order": 1, "type": "original", "transform_id": "tr.original.1", "text": query},
                {
                    "order": 2,
                    "type": "normalization",
                    "transform_id": "tr.normalization.1",
                    "text": normalized_query,
                    "changed": query != normalized_query,
                },
                {
                    "order": 3,
                    "type": "rewrite",
                    "transform_id": "tr.rewrite.1",
                    "status": "skipped",
                    "reason": (
                        "profile_query_rewrite_off"
                        if profile.retrieval.query_rewrite == "off"
                        else "no_rewrite_implementation_v1"
                    ),
                },
                {
                    "order": 4,
                    "type": "decomposition",
                    "transform_id": "tr.decomposition.1",
                    "status": "skipped",
                    "reason": (
                        "profile_query_decomposition_off"
                        if profile.retrieval.query_decomposition == "off"
                        else "direct_retrieval_no_decomposition"
                    ),
                },
            ],
            "query_refs": query_refs,
        },
    ]
    events.extend(_with_query_context(event, active_query_context) for event in model_events or [])
    for stage, candidates in result_sets.items():
        base_stage = stage.split(":", 1)[0]
        events.append(
            {
                "stage": stage,
                "stable_stage": "dense.search" if base_stage == "dense" else "bm25",
                "query_context": active_query_context,
                "count": len(candidates),
                "latency_ms": timings.get(
                    f"dense_total:{stage.split(':', 1)[1]}" if base_stage == "dense" and ":" in stage else stage,
                    timings.get("dense_total" if base_stage == "dense" else base_stage, 0),
                ),
                "candidates": [_candidate_debug(item, active_query_context) for item in candidates],
                "top": [item["chunk_id"] for item in candidates[:5]],
            }
        )
    events.append(
        {
            "stage": "rrf",
            "stable_stage": "rrf",
            "query_context": active_query_context,
            "count": len(fused),
            "latency_ms": timings.get("fusion", 0),
            "candidates": [_candidate_debug(item, active_query_context) for item in fused],
            "top": [item["chunk_id"] for item in fused[:5]],
        }
    )
    events.append(
        {
            "stage": "rerank",
            "stable_stage": "rerank",
            "query_context": active_query_context,
            "count": len(reranked),
            "latency_ms": timings.get("rerank", 0),
            "model_call": rerank_model_event or {},
            "candidates": [_candidate_debug(item, active_query_context) for item in reranked],
            "top": [item["chunk_id"] for item in reranked[:5]],
        }
    )
    events.extend(_with_query_context(event, active_query_context) for event in policy_events)
    events.append(
        {
            "stage": "context",
            "stable_stage": "context_selection",
            "query_context": active_query_context,
            "count": len(selected),
            "candidates": [_candidate_debug(item, active_query_context) for item in selected],
            "latency_ms": latency_ms,
            "stage_latency_ms": timings.get("context", 0),
        }
    )
    events.append({"stage": "timings", "query_context": active_query_context, "timings_ms": timings})
    return events


def _candidate_debug(item: dict[str, Any], query_context: dict[str, Any] | None = None) -> dict[str, Any]:
    active_query_context = dict(item.get("query_context") or query_context or {})
    return {
        "chunk_id": item["chunk_id"],
        "document_id": item.get("document_id"),
        "knowledge_base_id": item.get("knowledge_base_id", ""),
        "page_id": item.get("page_id"),
        "subquery_id": item.get("subquery_id") or active_query_context.get("subquery_id"),
        "transform_id": item.get("transform_id") or active_query_context.get("transform_id"),
        "query_context": active_query_context,
        "title": item["title"],
        "source_url": item.get("source_url"),
        "scores": item.get("scores", {}),
        "ranks": item.get("ranks", {}),
        "metadata": item.get("metadata", {}),
    }


def _evidence_metadata(item: dict[str, Any], *, document_version_id: str) -> dict[str, Any]:
    metadata = dict(item.get("metadata") or {})
    for source_key, target_key in (
        ("document_id", "document_id"),
        ("document_version_id", "document_version_id"),
        ("source_uri", "source_uri"),
        ("locator", "locator"),
        ("page_id", "page_id"),
    ):
        value = item.get(source_key)
        if value is not None and target_key not in metadata:
            metadata[target_key] = value
    if document_version_id and "document_version_id" not in metadata:
        metadata["document_version_id"] = document_version_id
    return metadata


def _candidate_scope_key(candidate: dict[str, Any]) -> str:
    return f"{candidate.get('knowledge_base_id', '')}:{candidate['chunk_id']}"


def make_query_context(
    *,
    query: str,
    transform_id: str,
    subquery_id: str,
    query_role: str,
    transform_type: str,
    parent_subquery_id: str | None = None,
) -> dict[str, Any]:
    return {
        "transform_id": transform_id,
        "subquery_id": subquery_id,
        "parent_subquery_id": parent_subquery_id,
        "query_role": query_role,
        "query_hash": stable_hash(["query_context", query], 32),
        "transform_type": transform_type,
    }


def query_ref_from_context(query_context: dict[str, Any], *, text: str, order: int) -> dict[str, Any]:
    return {
        "subquery_id": query_context.get("subquery_id"),
        "transform_id": query_context.get("transform_id"),
        "parent_subquery_id": query_context.get("parent_subquery_id"),
        "query_role": query_context.get("query_role"),
        "order": order,
        "text": text,
        "hash": query_context.get("query_hash") or stable_hash(["query_context", text], 32),
    }


def _tag_candidate_sets(candidate_sets: Any, query_context: dict[str, Any]) -> None:
    for candidates in candidate_sets:
        for candidate in candidates:
            candidate["query_context"] = dict(query_context)
            candidate["subquery_id"] = query_context.get("subquery_id")
            candidate["transform_id"] = query_context.get("transform_id")


def _with_query_context(event: dict[str, Any], query_context: dict[str, Any]) -> dict[str, Any]:
    if "query_context" in event:
        return event
    return {
        **event,
        "query_context": dict(query_context),
        "subquery_id": query_context.get("subquery_id"),
        "transform_id": query_context.get("transform_id"),
    }


def _snapshot_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [deepcopy(candidate) for candidate in candidates]


def apply_knowledge_base_cap(candidates: list[dict[str, Any]], max_per_kb: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        kb_id = str(candidate.get("knowledge_base_id") or "")
        counts[kb_id] = counts.get(kb_id, 0) + 1
        if counts[kb_id] <= max_per_kb:
            selected.append(candidate)
    return selected


def _knowledge_base_cap_events(
    before: list[dict[str, Any]], after: list[dict[str, Any]], max_per_kb: int
) -> list[dict[str, Any]]:
    kept = {_candidate_scope_key(candidate) for candidate in after}
    return [
        {
            **_decision_base(candidate),
            "stage": "context_selection",
            "decision": "dropped",
            "reason": "KNOWLEDGE_BASE_CAP",
            "max_per_kb": max_per_kb,
        }
        for candidate in before
        if _candidate_scope_key(candidate) not in kept
    ]


def _answerability_event(answerability: Any, query_context: dict[str, Any]) -> dict[str, Any]:
    decision = answerability.model_dump(mode="json")
    reason = str(decision.get("reason") or "")
    status = str(decision.get("status") or "")
    reason_codes = [code for code in [reason, f"status_{status.lower()}" if status else ""] if code]
    decision["reason_codes"] = reason_codes
    return {
        "stage": "answerability",
        "stable_stage": "answerability",
        "query_context": dict(query_context),
        "decision": decision,
        "reason_codes": reason_codes,
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


def _stable_contract_scope(contract_ids: list[str]) -> str:
    return stable_hash(["multi_kb_contract_scope", *contract_ids], 32)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
