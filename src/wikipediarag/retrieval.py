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
from wikipediarag.observability import retrieval_span, safe_error_code, safe_telemetry_payload
from wikipediarag.query_transforms import bounded_decomposition, bounded_rewrite, normalize_query
from wikipediarag.reliability import OperationDeadline
from wikipediarag.repository import (
    fetch_chunks_for_dense_scan,
    fetch_current_retrieval_chunks,
    insert_retrieval_event,
)
from wikipediarag.retrieval_contract import validate_active_retrieval_contract
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile
from wikipediarag.schemas import Evidence, RetrievalResult
from wikipediarag.search_index import bm25_search, dense_search

QUERY_EMBEDDING_INSTRUCTION = (
    "Represent this query for retrieving factual answers from the local Russian Wikipedia corpus."
)


def _build_query_variants(query: str, profile: RetrievalProfile, enabled: bool) -> list[str]:
    """Build a small deterministic variant set used by the real first stage."""
    original = normalize_query(query)
    if not enabled:
        return [original]
    values = [original]
    rewrite = bounded_rewrite(original)
    if profile.retrieval.query_rewrite == "always" or (profile.retrieval.query_rewrite == "conditional" and rewrite):
        if rewrite:
            values.append(rewrite)
    decomposition = bounded_decomposition(original, max_subqueries=4)
    if profile.retrieval.query_decomposition == "always" or (
        profile.retrieval.query_decomposition == "conditional" and len(decomposition) > 1
    ):
        values.extend(decomposition)
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = normalize_for_embedding(value)
        if key and key not in seen:
            deduped.append(value)
            seen.add(key)
    return deduped[:4]


def query_embedding_instruction(profile: RetrievalProfile) -> str:
    if profile.source == "upload":
        return "Represent this query for retrieving factual answers from the uploaded private document corpus."
    if profile.source == "xml":
        return "Represent this query for retrieving factual answers from the Wikipedia XML corpus."
    return QUERY_EMBEDDING_INSTRUCTION


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
_CONTEXT_VALUE_RE = re.compile(r"\b\d{1,4}(?:[,.]\d+)?\b")


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
    apply_query_transforms: bool = True,
    deadline: OperationDeadline | None = None,
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
    variants = _build_query_variants(normalized_query, profile, apply_query_transforms)
    semaphore = asyncio.Semaphore(8)
    variant_vectors: list[list[float]] | None = None
    shared_embedding_event: dict[str, Any] | None = None
    if profile.retrieval.dense:
        embed_model = get_model_registry(resolved).require(profile.model_aliases.embed, "embedding")
        dimensions = int(embed_model.dimensions or profile.embedding_dimensions(resolved.embedding_dimensions))
        embedding_started = time.perf_counter()
        vectors, usage = await embeddings(
            variants,
            resolved,
            alias=profile.model_aliases.embed,
            dimensions=dimensions,
            query_instruction=query_embedding_instruction(profile),
            deadline=deadline,
            correlation_id=query_run_id or trace_id,
        )
        variant_vectors = [[float(value) for value in vector] for vector in vectors]
        timings_ms["dense_embedding"] = _elapsed_ms(embedding_started)
        shared_embedding_event = {
            "stage": "dense.embedding",
            "stable_stage": "dense.embedding",
            "operation": "embedding",
            "model_call": dict(usage.get("_gateway_metadata") or {}),
            "latency_ms": timings_ms["dense_embedding"],
            "batch_size": len(variants),
        }

    async def bounded_stage(
        variant_index: int,
        variant: str,
        kind: str,
    ) -> tuple[str, list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
        async with semaphore:
            if kind == "bm25":
                label, candidates, timings, events = await _run_bm25_stage(
                    variant,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    top_k=profile.retrieval.bm25_top_k,
                    settings=resolved,
                    read_alias=read_alias,
                    strict=profile.requires_real_provider,
                    filters=_filters_for_kb(search_filters, knowledge_base_id),
                )
            else:
                label, candidates, timings, events = await _run_dense_stage(
                    conn,
                    variant,
                    tenant_id,
                    knowledge_base_id,
                    resolved,
                    profile,
                    top_k=profile.retrieval.dense_top_k,
                    read_alias=read_alias,
                    filters=_filters_for_kb(search_filters, knowledge_base_id),
                    query_vector=variant_vectors[variant_index] if variant_vectors is not None else None,
                    deadline=deadline,
                )
            for candidate in candidates:
                candidate["variant_index"] = variant_index
                candidate["query_context"] = {
                    **active_query_context,
                    "text": variant,
                    "query_variant_index": variant_index,
                }
            return f"{label}:v{variant_index}", candidates, timings, events

    tasks = [
        asyncio.create_task(bounded_stage(index, variant, kind))
        for index, variant in enumerate(variants)
        for kind, enabled in (("bm25", profile.retrieval.bm25), ("dense", profile.retrieval.dense))
        if enabled
    ]
    results = await asyncio.gather(*tasks)
    result_sets = {label: candidates for label, candidates, _timing, _model_events in results}
    result_sets = await _confirm_current_candidates(
        conn,
        result_sets,
        tenant_id=tenant_id,
        knowledge_base_by_label={label: knowledge_base_id for label in result_sets},
        search_filters=search_filters,
    )
    _tag_candidate_sets(result_sets.values(), active_query_context)
    result_sets_snapshot = {label: _snapshot_candidates(candidates) for label, candidates in result_sets.items()}
    model_events = ([shared_embedding_event] if shared_embedding_event else []) + [
        event for _label, _candidates, _timing, events in results for event in events
    ]
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
    rerank_candidates = fused[: profile.retrieval.rerank_input_k]
    ambiguity_mode = profile.answer.ambiguity_mode
    if profile.retrieval.rerank:
        reranked, rerank_model_event = await rerank(
            normalized_query,
            rerank_candidates,
            resolved,
            profile,
            top_k=profile.retrieval.rerank_top_k,
            score_all=True,
            deadline=deadline,
            correlation_id=query_run_id or trace_id,
        )
    else:
        reranked = fused[: profile.retrieval.rerank_top_k]
        rerank_model_event = None
    timings_ms["rerank"] = _elapsed_ms(rerank_started)
    reranked_snapshot = _snapshot_candidates(reranked)
    context_started = time.perf_counter()
    selected, policy_events = postprocess_candidates(
        _order_ambiguity_candidates(reranked[: profile.retrieval.rerank_top_k], requested_top_k, ambiguity_mode),
        profile,
        requested_top_k,
        query=normalized_query,
        ambiguity_mode=ambiguity_mode,
    )
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
        apply_query_transforms=apply_query_transforms,
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
            document_id=str(item.get("document_id") or ""),
            content_unit_id=str(dict(item.get("metadata") or {}).get("content_unit_id") or ""),
            supporting_chunk_ids=list(
                dict(item.get("metadata") or {}).get("supporting_chunk_ids") or [item["chunk_id"]]
            ),
            provenance_refs=list(dict(item.get("metadata") or {}).get("provenance_refs") or []),
        )
        for index, item in enumerate(selected, start=1)
    ]
    answerability = decide_answerability(query, evidence, profile)
    if (
        answerability.status.value == "PARTIAL"
        and profile.retrieval.rerank
        and len(reranked) > profile.retrieval.rerank_top_k
    ):
        deepening_limit = min(len(fused), max(profile.retrieval.rerank_top_k, 2 * profile.retrieval.rerank_top_k))
        # All fused candidates were scored by the single reranker request.
        # Deepening only changes deterministic selection, never calls the gateway again.
        deepened = reranked[:deepening_limit]
        selected, deepening_policy_events = postprocess_candidates(
            _order_ambiguity_candidates(deepened, requested_top_k, ambiguity_mode),
            profile,
            requested_top_k,
            query=normalized_query,
            ambiguity_mode=ambiguity_mode,
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
                document_id=str(item.get("document_id") or ""),
                content_unit_id=str(dict(item.get("metadata") or {}).get("content_unit_id") or ""),
                supporting_chunk_ids=list(
                    dict(item.get("metadata") or {}).get("supporting_chunk_ids") or [item["chunk_id"]]
                ),
                provenance_refs=list(dict(item.get("metadata") or {}).get("provenance_refs") or []),
            )
            for index, item in enumerate(selected, start=1)
        ]
        reranked = deepened
        reranked_snapshot = _snapshot_candidates(reranked)
        policy_events.extend(deepening_policy_events)
        rerank_model_event = rerank_model_event
        timings_ms["retrieval_total"] = _elapsed_ms(started)
        answerability = decide_answerability(query, evidence, profile)
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
            apply_query_transforms=apply_query_transforms,
        )
    timings_ms["retrieval_total"] = _elapsed_ms(started)
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
    apply_query_transforms: bool = True,
    deadline: OperationDeadline | None = None,
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
            apply_query_transforms=apply_query_transforms,
            deadline=deadline,
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
    shared_query_vector: list[float] | None = None
    shared_embedding_started = time.perf_counter()
    if dense_stage_inputs:
        embed_alias = active_profile.model_aliases.embed
        embed_model = get_model_registry(resolved).require(embed_alias, "embedding")
        embed_dimensions = int(
            embed_model.dimensions or active_profile.embedding_dimensions(resolved.embedding_dimensions)
        )
        vectors, embedding_usage = await embeddings(
            [normalized_query],
            resolved,
            alias=embed_alias,
            dimensions=embed_dimensions,
            query_instruction=query_embedding_instruction(active_profile),
            deadline=deadline,
            correlation_id=query_run_id or trace_id,
        )
        shared_query_vector = [float(value) for value in vectors[0]]
        timings_ms["dense_embedding"] = _elapsed_ms(shared_embedding_started)
        shared_embedding_event = dict(embedding_usage.get("_gateway_metadata") or {})
    else:
        shared_embedding_event = None
    dense_tasks = [
        asyncio.create_task(
            _run_scoped_dense_stage(
                conn,
                normalized_query,
                tenant_id,
                kb_id,
                resolved,
                active_profile,
                top_k=active_profile.retrieval.dense_top_k,
                read_alias=read_alias,
                filters=_filters_for_kb(search_filters, kb_id),
                query_vector=shared_query_vector,
                deadline=deadline,
            )
        )
        for kb_id, read_alias in dense_stage_inputs
    ]
    raw_results = [*list(await asyncio.gather(*tasks)), *list(await asyncio.gather(*dense_tasks))]
    result_sets: dict[str, list[dict[str, Any]]] = {}
    model_events = [event for _label, _candidates, _timing, events in raw_results for event in events]
    if shared_embedding_event:
        model_events.insert(0, shared_embedding_event)
    for label, candidates, timing, _events in raw_results:
        _stage, _separator, kb_id = label.partition(":")
        result_sets[label] = candidates
        for timing_key, timing_value in timing.items():
            base_key = "dense" if timing_key == "dense_total" else timing_key
            timings_ms[f"{base_key}_task_sum"] = timings_ms.get(f"{base_key}_task_sum", 0) + timing_value
            timings_ms[base_key] = max(timings_ms.get(base_key, 0), timing_value)
            if kb_id:
                timings_ms[f"{timing_key}:{kb_id}"] = timing_value
    result_sets = await _confirm_current_candidates(
        conn,
        result_sets,
        tenant_id=tenant_id,
        knowledge_base_by_label={label: label.partition(":")[2] for label in result_sets},
        search_filters=search_filters,
    )
    _tag_candidate_sets(result_sets.values(), active_query_context)
    result_sets_snapshot = {label: _snapshot_candidates(candidates) for label, candidates in result_sets.items()}

    fusion_started = time.perf_counter()
    with retrieval_span("rrf", {"query_run_id": query_run_id or "", "trace_id": trace_id}):
        if active_profile.retrieval.fusion == "rrf" and len(result_sets) > 1:
            fused = rrf_fuse(result_sets, top_k=active_profile.retrieval.fusion_top_k)
        else:
            fused = _merge_without_fusion(result_sets, top_k=active_profile.retrieval.fusion_top_k)
    fused_snapshot = _snapshot_candidates(fused)
    # Do not discard the best candidates of a dominant KB before reranking.
    # The final context selector still enforces diversity, while global
    # reranking gets the complete first-stage union.
    kb_cap_policy_events: list[dict[str, Any]] = []
    timings_ms["fusion"] = _elapsed_ms(fusion_started)

    rerank_started = time.perf_counter()
    rerank_candidates = fused[: active_profile.retrieval.rerank_input_k]
    ambiguity_mode = active_profile.answer.ambiguity_mode
    if active_profile.retrieval.rerank:
        reranked, rerank_model_event = await rerank(
            normalized_query,
            rerank_candidates,
            resolved,
            active_profile,
            top_k=active_profile.retrieval.rerank_top_k,
            score_all=True,
            deadline=deadline,
            correlation_id=query_run_id or trace_id,
        )
    else:
        reranked = fused[: active_profile.retrieval.rerank_top_k]
        rerank_model_event = None
    timings_ms["rerank"] = _elapsed_ms(rerank_started)
    reranked_snapshot = _snapshot_candidates(reranked)
    context_started = time.perf_counter()
    selected, policy_events = postprocess_candidates(
        _order_ambiguity_candidates(reranked[: active_profile.retrieval.rerank_top_k], requested_top_k, ambiguity_mode),
        active_profile,
        requested_top_k,
        query=normalized_query,
        ambiguity_mode=ambiguity_mode,
    )
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
        apply_query_transforms=apply_query_transforms,
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
            document_id=str(item.get("document_id") or ""),
            content_unit_id=str(dict(item.get("metadata") or {}).get("content_unit_id") or ""),
            supporting_chunk_ids=list(
                dict(item.get("metadata") or {}).get("supporting_chunk_ids") or [item["chunk_id"]]
            ),
            provenance_refs=list(dict(item.get("metadata") or {}).get("provenance_refs") or []),
        )
        for index, item in enumerate(selected, start=1)
    ]
    answerability = decide_answerability(query, evidence, active_profile)
    if (
        answerability.status.value == "PARTIAL"
        and active_profile.retrieval.rerank
        and len(reranked) > active_profile.retrieval.rerank_top_k
    ):
        deepening_limit = min(
            len(reranked), max(active_profile.retrieval.rerank_top_k, 2 * active_profile.retrieval.rerank_top_k)
        )
        deepened = reranked[:deepening_limit]
        selected, deepening_policy_events = postprocess_candidates(
            _order_ambiguity_candidates(deepened, requested_top_k, ambiguity_mode),
            active_profile,
            requested_top_k,
            query=normalized_query,
            ambiguity_mode=ambiguity_mode,
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
                document_id=str(item.get("document_id") or ""),
                content_unit_id=str(dict(item.get("metadata") or {}).get("content_unit_id") or ""),
                supporting_chunk_ids=list(
                    dict(item.get("metadata") or {}).get("supporting_chunk_ids") or [item["chunk_id"]]
                ),
                provenance_refs=list(dict(item.get("metadata") or {}).get("provenance_refs") or []),
            )
            for index, item in enumerate(selected, start=1)
        ]
        policy_events.extend(deepening_policy_events)
        answerability = decide_answerability(query, evidence, active_profile)
        reranked_snapshot = _snapshot_candidates(deepened)
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
            latency_ms=timings_ms.get("retrieval_total", 0),
            timings_ms=timings_ms,
            contract={
                "knowledge_base_ids": knowledge_base_ids,
                "contracts": [
                    {"knowledge_base_id": kb_id, **contract_by_kb[kb_id].event_payload()}
                    for kb_id in knowledge_base_ids
                ],
                "index_contract_id": index_contract_id,
                "run_contract_id": run_contract_id,
            },
            query_context=active_query_context,
            apply_query_transforms=apply_query_transforms,
        )
    events.append(_answerability_event(answerability, active_query_context))
    timings_ms["retrieval_total"] = _elapsed_ms(started)
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
    query_vector: list[float] | None = None,
    deadline: OperationDeadline | None = None,
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
        query_vector=query_vector,
        deadline=deadline,
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
    query_vector: list[float] | None = None,
    deadline: OperationDeadline | None = None,
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
        query_vector=query_vector,
        deadline=deadline,
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
    query_vector: list[float] | None = None,
    deadline: OperationDeadline | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    total_started = time.perf_counter()
    timings_ms: dict[str, int] = {}
    registry = get_model_registry(settings)
    alias = profile.model_aliases.embed
    model = registry.require(alias, "embedding")
    dimensions = int(model.dimensions or profile.embedding_dimensions(settings.embedding_dimensions))
    model_events: list[dict[str, Any]] = []
    if query_vector is None:
        embedding_started = time.perf_counter()
        with retrieval_span("dense.embedding", {"knowledge_base_id": knowledge_base_id, "model_alias": alias}):
            vectors, usage = await embeddings(
                [query],
                settings,
                alias=alias,
                dimensions=dimensions,
                query_instruction=query_embedding_instruction(profile),
                deadline=deadline,
                correlation_id=knowledge_base_id,
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
                "metadata": {
                    **dict(row.get("metadata") or {}),
                    "parent_chunk_id": row.get("parent_chunk_id"),
                    "content_hash": row.get("content_hash"),
                },
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


async def _confirm_current_candidates(
    conn: AsyncConnection,
    result_sets: dict[str, list[dict[str, Any]]],
    *,
    tenant_id: str,
    knowledge_base_by_label: dict[str, str],
    search_filters: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Keep only candidates that PostgreSQL currently permits us to expose.

    The search index is deliberately useful for recall but cannot make a chunk
    public.  This confirmation happens before fusion/reranking, so a stale
    candidate never affects score, provenance, or cache output.
    """
    confirmed: dict[str, list[dict[str, Any]]] = {}
    for label, candidates in result_sets.items():
        knowledge_base_id = knowledge_base_by_label.get(label, "")
        rows = await fetch_current_retrieval_chunks(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            chunk_ids=[str(item.get("chunk_id") or "") for item in candidates],
        )
        access_scope = _document_access_scope_from_filters(_filters_for_kb(search_filters, knowledge_base_id))
        safe_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            current = rows.get(str(candidate.get("chunk_id") or ""))
            if current is None:
                continue
            metadata = {**dict(candidate.get("metadata") or {}), **dict(current.get("metadata") or {})}
            if not is_document_visible(metadata, access_scope):
                continue
            safe_candidates.append(
                {
                    **candidate,
                    "knowledge_base_id": knowledge_base_id,
                    "document_id": str(current.get("document_id") or candidate.get("document_id") or ""),
                    "document_version_id": current.get("document_version_id") or candidate.get("document_version_id"),
                    "metadata": metadata,
                }
            )
        confirmed[label] = safe_candidates
    return confirmed


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
    fused.sort(
        key=lambda item: (
            -float(item["scores"].get("rrf_total", 0.0)),
            str(item.get("knowledge_base_id") or ""),
            str(item.get("document_id") or ""),
            str(item.get("chunk_id") or ""),
        )
    )
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
    candidates = list(merged.values())
    candidates.sort(
        key=lambda item: (
            -max(
                [
                    float(value)
                    for key, value in item.get("scores", {}).items()
                    if key in {"bm25", "dense"} and isinstance(value, (int, float))
                ]
                or [0.0]
            ),
            str(item.get("knowledge_base_id") or ""),
            str(item.get("document_id") or ""),
            str(item.get("chunk_id") or ""),
        )
    )
    candidates = candidates[:top_k]
    for rank, candidate in enumerate(candidates, start=1):
        candidate.setdefault("ranks", {})["fusion"] = rank
    return candidates


async def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    settings: Settings,
    profile: RetrievalProfile,
    top_k: int,
    score_all: bool = False,
    deadline: OperationDeadline | None = None,
    correlation_id: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not candidates:
        return [], None
    candidates = candidates[: profile.retrieval.rerank_input_k]
    model_event: dict[str, Any] | None = None
    documents = [f"{item['title']}\n{' / '.join(item['section_path'])}\n{item['content']}" for item in candidates]
    try:
        with retrieval_span("rerank", {"model_alias": profile.model_aliases.rerank, "candidate_count": len(documents)}):
            payload = await gateway_rerank(
                query,
                documents,
                settings,
                alias=profile.model_aliases.rerank,
                top_n=len(documents) if score_all else min(top_k, len(documents)),
                deadline=deadline,
                correlation_id=correlation_id,
            )
        model_event = dict(payload.get("_gateway_metadata") or {})
        seen_indexes: set[int] = set()
        for result in payload.get("results", []):
            index = int(result.get("index", -1))
            if index < 0 or index >= len(candidates) or index in seen_indexes:
                continue
            seen_indexes.add(index)
            candidates[index]["scores"]["rerank"] = float(result.get("relevance_score", 0.0))
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
    _apply_entity_title_boost(query, candidates)
    candidates.sort(
        key=lambda item: (
            -float(item["scores"].get("title_exact", 0.0)),
            -float(item["scores"].get("rerank", 0.0)),
            -float(item["scores"].get("rrf_total", 0.0)),
            str(item.get("knowledge_base_id") or ""),
            str(item.get("document_id") or ""),
            str(item.get("chunk_id") or ""),
        )
    )
    for rank, candidate in enumerate(candidates[:top_k], start=1):
        candidate["ranks"]["rerank"] = rank
    return (candidates if score_all else candidates[:top_k]), model_event


def _apply_entity_title_boost(query: str, candidates: list[dict[str, Any]]) -> None:
    """Promote one unambiguous exact title without treating parentheticals as exact."""

    normalized_query = normalize_for_embedding(query).strip(" ?!.,")
    if not normalized_query:
        return
    title_candidates = {normalized_query}
    for prefix in ("что такое ", "кто такой ", "кто такая ", "what is ", "who is "):
        if normalized_query.startswith(prefix):
            tail = normalized_query.removeprefix(prefix).strip(" ?!.,")
            if tail:
                title_candidates.add(tail)
    exact: list[dict[str, Any]] = []
    for candidate in candidates:
        aliases = [str(candidate.get("title") or ""), *[str(item) for item in candidate.get("section_path") or []]]
        is_exact = any(normalize_for_embedding(alias).strip(" ?!.,") in title_candidates for alias in aliases)
        if is_exact:
            exact.append(candidate)
    if len({str(item.get("document_id") or item.get("title")) for item in exact}) != 1:
        return
    for candidate in candidates:
        aliases = [str(candidate.get("title") or ""), *[str(item) for item in candidate.get("section_path") or []]]
        if any(normalize_for_embedding(alias).strip(" ?!.,") in title_candidates for alias in aliases):
            candidate.setdefault("scores", {})["title_exact"] = 1.0


def postprocess_candidates(
    candidates: list[dict[str, Any]],
    profile: RetrievalProfile,
    requested_top_k: int,
    *,
    query: str = "",
    ambiguity_mode: str = "off",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    page_counts: dict[tuple[str, str, str], int] = {}
    document_counts: dict[str, int] = {}
    seen_hashes: set[str] = set()
    seen_units: dict[str, dict[str, Any]] = {}
    deferred_by_quota: list[dict[str, Any]] = []
    negative_titles = extract_explicit_negative_titles(query)
    max_evidence = min(requested_top_k, profile.postprocess.final_evidence_max, 12)
    document_limit = 2 if ambiguity_mode in {"auto", "always"} else 3
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
        content_hash = str(metadata.get("content_hash") or stable_hash([content], 32))
        parent_id = str(metadata.get("parent_chunk_id") or candidate.get("parent_chunk_id") or "")
        content_unit_id = parent_id or f"content:{content_hash}"
        metadata["content_unit_id"] = content_unit_id
        supporting = list(metadata.get("supporting_chunk_ids") or [])
        chunk_id = str(candidate.get("chunk_id") or "")
        if chunk_id and chunk_id not in supporting:
            supporting.append(chunk_id)
        metadata["supporting_chunk_ids"] = supporting
        if profile.postprocess.dedup and (content_hash in seen_hashes or content_unit_id in seen_units):
            if content_unit_id in seen_units:
                existing = seen_units[content_unit_id]
                existing_meta = dict(existing.get("metadata") or {})
                existing_ids = list(existing_meta.get("supporting_chunk_ids") or [])
                if chunk_id and chunk_id not in existing_ids:
                    existing_meta["supporting_chunk_ids"] = [*existing_ids, chunk_id]
                    existing["metadata"] = existing_meta
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
        page_key = page_scope_key(candidate)
        document_key = str(candidate.get("document_id") or candidate.get("source_url") or candidate.get("title") or "")
        document_quota_reached = (
            document_counts.get(document_key, 0) >= document_limit
            and not metadata.get("parent_expanded")
            and not metadata.get("adjacent_expansion")
        )
        quota_reached = page_counts.get(page_key, 0) >= profile.postprocess.page_quota
        existing_terms = set(normalize_for_embedding(" ".join(item["content"] for item in selected)).split())
        candidate_terms = set(normalize_for_embedding(content).split())
        query_terms = set(normalize_for_embedding(query).split())
        novel_requirement = bool((candidate_terms & query_terms) - existing_terms)
        # A new number is not by itself a new requirement: tables and legal
        # documents legitimately contain many unrelated values.  Let only
        # query-term novelty bypass the per-page quota.
        if document_quota_reached:
            deferred_by_quota.append({**candidate, "content": content, "metadata": metadata})
            events.append(
                {
                    **_decision_base(candidate),
                    "stage": "context_selection",
                    "decision": "deferred",
                    "reason": "DOCUMENT_UNIT_QUOTA",
                    "document_unit_quota": document_limit,
                }
            )
            continue
        if quota_reached and not novel_requirement:
            deferred_by_quota.append({**candidate, "content": content, "metadata": metadata})
            events.append(
                {
                    **_decision_base(candidate),
                    "stage": "context_selection",
                    "decision": "deferred",
                    "reason": "PAGE_QUOTA",
                    "page_quota": profile.postprocess.page_quota,
                    "page_quota_policy_version": PAGE_QUOTA_POLICY_VERSION,
                }
            )
            continue
        candidate_tokens = _count_context_tokens(content)
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
        seen_units[content_unit_id] = selected_candidate = {**candidate, "content": content, "metadata": metadata}
        used_tokens += candidate_tokens
        page_counts[page_key] = page_counts.get(page_key, 0) + 1
        document_counts[document_key] = document_counts.get(document_key, 0) + 1
        final_rank = len(selected) + 1
        ranks = {**dict(candidate.get("ranks") or {}), "final": final_rank}
        query_context = dict(candidate.get("query_context") or {})
        if query_context:
            metadata["query_context"] = query_context
            metadata["subquery_id"] = query_context.get("subquery_id")
            metadata["transform_id"] = query_context.get("transform_id")
        selected_candidate = {**selected_candidate, "ranks": ranks}
        seen_units[content_unit_id] = selected_candidate
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
    # A second deterministic pass admits deferred candidates only when the
    # normal quota left the context under-filled. This keeps page diversity as
    # the default while allowing evidence that carries a new requirement.
    evidence_target = min(profile.postprocess.final_evidence_min, max_evidence)
    target_met = len(selected) >= evidence_target
    if len(selected) < evidence_target:
        for candidate in deferred_by_quota:
            if len(selected) >= max_evidence:
                break
            content = str(candidate["content"])
            content_hash = str(dict(candidate.get("metadata") or {}).get("content_hash") or stable_hash([content], 32))
            unit = str(dict(candidate.get("metadata") or {}).get("content_unit_id") or f"content:{content_hash}")
            if content_hash in seen_hashes or unit in seen_units:
                continue
            selected_text = " ".join(item["content"] for item in selected)
            novel = bool(
                (set(normalize_for_embedding(content).split()) & set(normalize_for_embedding(query).split()))
                - set(normalize_for_embedding(selected_text).split())
            )
            if not novel and target_met:
                continue
            tokens = _count_context_tokens(content)
            if profile.postprocess.context_packing == "token_budget" and used_tokens + tokens > token_budget:
                continue
            page_key = page_scope_key(candidate)
            document_key = str(
                candidate.get("document_id") or candidate.get("source_url") or candidate.get("title") or ""
            )
            if (
                document_counts.get(document_key, 0) >= document_limit
                and not dict(candidate.get("metadata") or {}).get("parent_expanded")
                and not dict(candidate.get("metadata") or {}).get("adjacent_expansion")
            ):
                continue
            used_tokens += tokens
            page_counts[page_key] = page_counts.get(page_key, 0) + 1
            document_counts[document_key] = document_counts.get(document_key, 0) + 1
            metadata = dict(candidate.get("metadata") or {})
            metadata["quota_overflow"] = True
            selected_candidate = {
                **candidate,
                "metadata": metadata,
                "ranks": {**dict(candidate.get("ranks") or {}), "final": len(selected) + 1},
            }
            selected.append(selected_candidate)
            seen_hashes.add(content_hash)
            seen_units[unit] = selected_candidate
            events.append(
                {
                    **_decision_base(selected_candidate),
                    "stage": "context_selection",
                    "decision": "selected",
                    "reason": "NOVEL_REQUIREMENT_QUOTA_OVERFLOW",
                    "final_rank": len(selected),
                }
            )
            target_met = len(selected) >= evidence_target
            if target_met:
                break
    events.append(
        {
            "stage": "context_selection",
            "decision": "evidence_target",
            "evidence_target": evidence_target,
            "evidence_target_met": len(selected) >= evidence_target,
        }
    )
    return selected, events


def _order_ambiguity_candidates(
    candidates: list[dict[str, Any]], requested_top_k: int, ambiguity_mode: str
) -> list[dict[str, Any]]:
    """Deterministically expose multiple document meanings in the first pass."""
    if ambiguity_mode not in {"auto", "always"} or len(candidates) <= 1:
        return candidates
    first_pass_limit = min(8, requested_top_k, 12)
    selected: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    remaining: list[dict[str, Any]] = []
    for candidate in candidates:
        document_key = str(candidate.get("document_id") or candidate.get("source_url") or candidate.get("title") or "")
        if document_key not in seen_documents and len(selected) < first_pass_limit:
            selected.append(candidate)
            seen_documents.add(document_key)
        else:
            remaining.append(candidate)
    return [*selected, *remaining]


def _count_context_tokens(content: str) -> int:
    """Stable tokenizer contract approximation used by context packing.

    The gateway exposes a tokenizer contract, but retrieval must remain
    deterministic when the gateway is unavailable. Unicode word/punctuation
    segmentation is closer to model token accounting than whitespace counts.
    """
    return max(1, len(re.findall(r"\w+|[^\w\s]", content, flags=re.UNICODE)))


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
    apply_query_transforms: bool = True,
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
    rewritten_query = (
        bounded_rewrite(normalized_query)
        if apply_query_transforms and profile.retrieval.query_rewrite != "off"
        else None
    )
    decomposition = (
        bounded_decomposition(normalized_query, max_subqueries=4)
        if apply_query_transforms and profile.retrieval.query_decomposition != "off"
        else []
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
            "original_query_hash": stable_hash(["retrieval_query", query], 32),
            "normalized_query_hash": stable_hash(["retrieval_query", normalized_query], 32),
            "query_length_chars": len(query),
            "rewritten_query": stable_hash(["retrieval_query", rewritten_query], 32) if rewritten_query else None,
            "transforms": [
                {
                    "order": 1,
                    "type": "original",
                    "transform_id": "tr.original.1",
                    "hash": stable_hash(["retrieval_query", query], 32),
                },
                {
                    "order": 2,
                    "type": "normalization",
                    "transform_id": "tr.normalization.1",
                    "hash": stable_hash(["retrieval_query", normalized_query], 32),
                    "changed": query != normalized_query,
                },
                {
                    "order": 3,
                    "type": "rewrite",
                    "transform_id": "tr.rewrite.1",
                    "status": "performed" if rewritten_query else "skipped",
                    "reason": "bounded_deterministic_rewrite_v1" if rewritten_query else "rewrite_not_applicable",
                },
                {
                    "order": 4,
                    "type": "decomposition",
                    "transform_id": "tr.decomposition.1",
                    "status": "performed" if decomposition else "skipped",
                    "reason": (
                        "bounded_deterministic_decomposition_v1" if decomposition else "decomposition_not_applicable"
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
    safe_metadata = {
        key: value
        for key, value in dict(item.get("metadata") or {}).items()
        if key
        in {
            "source_type",
            "source_document_id",
            "source_chunk_id",
            "document_version_id",
            "content_hash",
            "content_unit_id",
            "supporting_chunk_ids",
            "parent_expanded",
            "neighbor_expanded",
            "quota_overflow",
        }
    }
    projected = safe_telemetry_payload(
        {
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
            "metadata": safe_metadata,
        }
    )
    return dict(projected) if isinstance(projected, dict) else {}


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
        "query_hash": stable_hash(["retrieval_query", text], 32),
        "length_chars": len(text),
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
    counts: dict[tuple[str, str, str], int] = {}
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        page_key = page_scope_key(candidate)
        if counts.get(page_key, 0) >= max_per_page:
            continue
        counts[page_key] = counts.get(page_key, 0) + 1
        selected.append(candidate)
    return selected


PAGE_QUOTA_POLICY_VERSION = "document_scoped_page_quota_v2"


def page_scope_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    """Return a document-scoped page identity for context diversification.

    Uploaded documents commonly restart their local page/chunk ordinals at one.
    The document id is therefore part of the quota key; a missing locator falls
    back to the chunk id so malformed/legacy rows cannot suppress one another.
    """
    metadata = dict(candidate.get("metadata") or {})
    knowledge_base_id = str(candidate.get("knowledge_base_id") or metadata.get("knowledge_base_id") or "")
    document_id = str(
        candidate.get("document_id") or metadata.get("document_id") or candidate.get("chunk_id") or "unknown"
    )
    raw_locator = candidate.get("locator")
    locator: dict[str, Any]
    if isinstance(raw_locator, dict):
        locator = raw_locator
    else:
        metadata_locator = metadata.get("locator")
        locator = metadata_locator if isinstance(metadata_locator, dict) else {}
    raw_page = candidate.get("page_id")
    if raw_page is None:
        raw_page = locator.get("page")
    if raw_page is None:
        raw_page = locator.get("page_id")
    if raw_page is None:
        raw_page = candidate.get("chunk_id") or "unknown"
    return knowledge_base_id, document_id, str(raw_page)


def _stable_contract_scope(contract_ids: list[str]) -> str:
    return stable_hash(["multi_kb_contract_scope", *contract_ids], 32)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
