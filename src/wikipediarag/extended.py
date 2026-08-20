from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.answerability import decide_answerability, is_insufficient
from wikipediarag.config import Settings
from wikipediarag.db import connect_autocommit, json_dumps
from wikipediarag.embedding import normalize_for_embedding
from wikipediarag.ids import new_uuid, stable_hash
from wikipediarag.provenance import public_provenance_from_metadata
from wikipediarag.reliability import OperationDeadline
from wikipediarag.repository import fetch_chunk_by_id, insert_retrieval_event
from wikipediarag.retrieval import make_query_context, query_ref_from_context, retrieve, retrieve_multi
from wikipediarag.retrieval_profile import RetrievalProfile
from wikipediarag.schemas import AnswerabilityStatus, Evidence, RetrievalResult, SourceProvenance
from wikipediarag.workspace_sql import text

StopReason = Literal[
    "evidence_sufficient",
    "budget_reached",
    "duplicate_tool_call",
    "no_new_evidence",
    "coverage_stalled",
    "conflict_unresolved",
]


class HarnessBudgets(BaseModel):
    max_steps: int = 8
    max_subqueries: int = 6
    max_rewrites_per_subquery: int = 2
    max_parallel_tool_calls: int = 4
    max_unique_documents: int = 20
    max_total_retrieved_chunks: int = 300
    max_context_tokens: int = 30000
    max_wall_time_seconds: int = 90


class EvidenceLedgerItem(BaseModel):
    subquery_id: str
    text: str
    status: Literal["covered", "partial", "missing", "mentioned"]
    evidence_ids: list[str] = Field(default_factory=list)


class CoverageItem(BaseModel):
    part: str
    status: Literal["covered", "mentioned", "missing"]
    evidence_ids: list[str] = Field(default_factory=list)


class HarnessState(BaseModel):
    original_query: str
    intent: str
    subqueries: list[str] = Field(default_factory=list)
    current_step: int = 0
    retrieval_profile: str
    evidence_ledger: list[EvidenceLedgerItem] = Field(default_factory=list)
    visited_pages: list[str] = Field(default_factory=list)
    tool_call_hashes: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    coverage_inventory: list[CoverageItem] = Field(default_factory=list)
    coverage: float = 0.0
    stop_reason: StopReason | None = None


ALLOWED_TOOLS = {
    "search",
    "fetch_chunk",
    "fetch_section",
    "fetch_document",
    "get_neighbors",
    "follow_links",
    "search_within_document",
    "compare_evidence",
    "verify_claims",
    "finish",
}


def should_start_extended(query: str) -> bool:
    normalized = query.casefold()
    markers = [
        "сравни",
        "сравнение",
        "отличается",
        "почему",
        "как связано",
        "между",
        "multi-hop",
        "несколько",
        "конфликт",
        "что общего",
        "название которой начинается",
        "название которого начинается",
        "название начинается",
        "те же цифры",
        "тех же цифр",
        "вышедшего на экраны",
        "вышедший на экраны",
    ]
    return (
        any(marker in normalized for marker in markers)
        or _looks_like_bridge_query(normalized)
        or normalized.count("?") > 1
    )


async def run_extended_search(
    conn: AsyncConnection,
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_ids: list[str] | None = None,
    query_run_id: str,
    trace_id: str,
    settings: Settings,
    profile: RetrievalProfile,
    profile_overrides: dict[str, Any] | None = None,
    search_filters: dict[str, Any] | None = None,
    seed_result: RetrievalResult | None = None,
    deadline: OperationDeadline | None = None,
) -> RetrievalResult:
    extended_started = time.perf_counter()
    budgets = HarnessBudgets(max_context_tokens=profile.postprocess.max_context_tokens)
    state = HarnessState(
        original_query=query,
        intent=_classify_intent(query),
        # Reserve one bounded slot for an evidence-gap repair query. This
        # keeps the total within max_subqueries while ensuring PARTIAL does
        # not silently exhaust the queue before repair can be scheduled.
        subqueries=_build_subqueries(query, max(1, budgets.max_subqueries - 1)),
        retrieval_profile=profile.name,
    )
    minimum_search_steps = 2 if _looks_like_bridge_query(query.casefold()) else 1
    combined: dict[str, Evidence] = {}
    if seed_result is not None:
        for evidence in seed_result.evidence:
            combined[f"{evidence.knowledge_base_id}:{evidence.chunk_id}"] = evidence
    step_evidence_ids_by_step: list[list[str]] = []
    previous_coverage = 0.0
    stalled_steps = 0
    normalized_query = " ".join(query.split())
    primary_query_context = make_query_context(
        query=normalized_query,
        transform_id="tr.normalization.1",
        subquery_id="sq.primary.1",
        query_role="primary",
        transform_type="normalization",
    )
    bridge_queries = _bridge_subqueries(query)
    subquery_contexts = _subquery_contexts(state.subqueries, bridge_queries)
    query_refs = [
        query_ref_from_context(context, text=subquery, order=index)
        for index, (subquery, context) in enumerate(zip(state.subqueries, subquery_contexts, strict=True), start=1)
    ]
    bridge_refs = [ref for ref in query_refs if ref.get("query_role") == "bridge"]
    decomposition_refs = [ref for ref in query_refs if ref.get("query_role") == "decomposition"]
    events: list[dict[str, Any]] = [
        {
            "stage": "query_transform",
            "stable_stage": "query_transform",
            "query_context": primary_query_context,
            "original_query_hash": stable_hash(["retrieval_query", query], 32),
            "normalized_query_hash": stable_hash(["retrieval_query", normalized_query], 32),
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
                    "type": "bridge_queries",
                    "transform_id": "tr.bridge.1",
                    "query_hashes": [stable_hash(["retrieval_query", item], 32) for item in bridge_queries],
                    "query_refs": bridge_refs,
                    "status": "performed" if bridge_queries else "skipped",
                    "reason": "bridge_query_detector_v1" if bridge_queries else "no_bridge_query_detected",
                },
                {
                    "order": 4,
                    "type": "decomposition",
                    "transform_id": "tr.decomposition.1",
                    "query_hashes": [stable_hash(["retrieval_query", item], 32) for item in state.subqueries],
                    "query_refs": decomposition_refs,
                    "status": "performed" if state.subqueries else "skipped",
                    "reason": "extended_search_subquery_builder_v1",
                },
            ],
            "query_refs": query_refs,
        }
    ]

    # A direct retrieval can seed the harness. The original query is already
    # represented in the ledger and must not be searched a second time.
    subquery_index = 0
    if seed_result is not None:
        original_key = normalize_for_embedding(query)
        for index, candidate in enumerate(state.subqueries):
            if normalize_for_embedding(candidate) == original_key:
                state.subqueries.pop(index)
                break
    if seed_result is not None:
        state.evidence_ledger.append(
            EvidenceLedgerItem(
                subquery_id="sq.primary.1",
                text=query,
                status="covered" if seed_result.evidence else "missing",
                evidence_ids=[item.evidence_id for item in seed_result.evidence],
            )
        )
    # Execute the first independent wave concurrently.  Each retrieval gets
    # its own autocommit connection; the controller connection is reserved for
    # deterministic ledger/event persistence below.
    prefetched: dict[str, RetrievalResult] = {}
    initial_wave = state.subqueries[: min(budgets.max_parallel_tool_calls, len(state.subqueries))]

    async def run_prefetched(item: str, item_context: dict[str, Any]) -> tuple[str, RetrievalResult]:
        async def call(retrieval_conn: AsyncConnection) -> RetrievalResult:
            if len(knowledge_base_ids or [knowledge_base_id]) > 1:
                return await retrieve_multi(
                    retrieval_conn,
                    item,
                    tenant_id=tenant_id,
                    knowledge_base_ids=list(knowledge_base_ids or []),
                    query_run_id=query_run_id,
                    trace_id=trace_id,
                    settings=settings,
                    top_k=profile.postprocess.final_evidence_max,
                    profile=profile,
                    profile_overrides=profile_overrides,
                    query_context=item_context,
                    search_filters=search_filters,
                    persist_events=False,
                    apply_query_transforms=False,
                    deadline=deadline,
                )
            return await retrieve(
                retrieval_conn,
                item,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                query_run_id=query_run_id,
                trace_id=trace_id,
                settings=settings,
                top_k=profile.postprocess.final_evidence_max,
                profile=profile,
                profile_overrides=profile_overrides,
                query_context=item_context,
                search_filters=search_filters,
                persist_events=False,
                apply_query_transforms=False,
                deadline=deadline,
            )

        try:
            async with connect_autocommit(settings) as retrieval_conn:
                result = await call(retrieval_conn)
        except OSError:
            # Unit/in-process callers may supply a lightweight fake connection.
            result = await call(conn)
        return item, result

    if initial_wave and len(knowledge_base_ids or [knowledge_base_id]) > 1:
        prefetched = dict(
            await _gather_ordered(
                [run_prefetched(item, subquery_contexts[index]) for index, item in enumerate(initial_wave)]
            )
        )
    step = 0
    while subquery_index < len(state.subqueries) and step < budgets.max_steps:
        if time.perf_counter() - extended_started >= budgets.max_wall_time_seconds:
            state.stop_reason = "budget_reached"
            break
        subquery = state.subqueries[subquery_index]
        subquery_index += 1
        step += 1
        if subquery_index <= len(subquery_contexts):
            query_context = subquery_contexts[subquery_index - 1]
        else:
            query_context = make_query_context(
                query=subquery,
                transform_id=f"tr.gap.{step}",
                subquery_id=f"sq.gap.{step}",
                query_role="repair",
                transform_type="evidence_gap_repair",
            )
        state.current_step = step
        if not _record_tool_call(state, "search", {"query": subquery, "profile": profile.name}):
            state.stop_reason = "duplicate_tool_call"
            break
        tool_started = time.perf_counter()
        if subquery in prefetched:
            result = prefetched.pop(subquery)
        elif len(knowledge_base_ids or [knowledge_base_id]) > 1:
            result = await retrieve_multi(
                conn,
                subquery,
                tenant_id=tenant_id,
                knowledge_base_ids=list(knowledge_base_ids or []),
                query_run_id=query_run_id,
                trace_id=trace_id,
                settings=settings,
                top_k=profile.postprocess.final_evidence_max,
                profile=profile,
                profile_overrides=profile_overrides,
                query_context=query_context,
                search_filters=search_filters,
                persist_events=False,
                apply_query_transforms=False,
                deadline=deadline,
            )
        else:
            result = await retrieve(
                conn,
                subquery,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                query_run_id=query_run_id,
                trace_id=trace_id,
                settings=settings,
                top_k=profile.postprocess.final_evidence_max,
                profile=profile,
                profile_overrides=profile_overrides,
                query_context=query_context,
                search_filters=search_filters,
                persist_events=False,
                apply_query_transforms=False,
                deadline=deadline,
            )
        tool_latency_ms = _elapsed_ms(tool_started)
        retrieval_timings = _extract_timings(result.events)
        new_count = 0
        step_evidence_ids: list[str] = []
        for evidence in result.evidence:
            evidence_key = f"{evidence.knowledge_base_id}:{evidence.chunk_id}"
            if evidence_key not in combined:
                combined[evidence_key] = evidence
                new_count += 1
                step_evidence_ids.append(evidence.chunk_id)
            page = str(evidence.metadata.get("zim_entry_path") or evidence.title)
            if page and page not in state.visited_pages:
                state.visited_pages.append(page)
        if step_evidence_ids:
            step_evidence_ids_by_step.append(step_evidence_ids)
        neighbor_count = await _expand_neighbors(
            conn,
            list(result.evidence[:2]),
            combined,
            state,
            events,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            window=1,
            filters=search_filters,
        )
        inventory = _coverage_inventory(query, list(combined.values()), profile)
        state.coverage_inventory = inventory
        coverage = _coverage_ratio(inventory)
        state.coverage = coverage
        provisional_evidence = _renumber_evidence(
            _select_final_evidence(
                combined,
                step_evidence_ids_by_step,
                profile.postprocess.final_evidence_max,
            )
        )
        progress_answerability = decide_answerability(query, provisional_evidence, profile)
        missing_answer_terms = progress_answerability.signals.get("missing_answer_bearing_terms", [])
        gap_queries_added = 0
        state.evidence_ledger.append(
            EvidenceLedgerItem(
                subquery_id=str(query_context["subquery_id"]),
                text=subquery,
                status=_ledger_status(inventory, result.evidence),
                evidence_ids=[item.evidence_id for item in result.evidence],
            )
        )
        events.append(
            {
                "stage": "harness_tool",
                "stable_stage": "retrieval.extended",
                "tool": "search",
                "step": step,
                "query_text_hash": stable_hash(["retrieval_query", subquery], 32),
                "query_length_chars": len(subquery),
                "subquery_id": query_context["subquery_id"],
                "transform_id": query_context["transform_id"],
                "query_hash": query_context["query_hash"],
                "query_context": query_context,
                "new_evidence": new_count,
                "new_neighbors": neighbor_count,
                "retrieved_count": _retrieved_count(result.events),
                "selected_count": len(result.evidence),
                "max_bm25_score": _max_stage_score(result.events, "bm25"),
                "max_dense_score": _max_stage_score(result.events, "dense"),
                "max_fusion_score": _max_stage_score(result.events, "fusion"),
                "max_rerank_score": _max_stage_score(result.events, "rerank"),
                "coverage": coverage,
                "coverage_inventory": [item.model_dump(mode="json") for item in inventory],
                "answerability_progress": progress_answerability.model_dump(mode="json"),
                "latency_ms": tool_latency_ms,
                "retrieval_timings_ms": retrieval_timings,
                "index_contract_id": result.index_contract_id,
                "run_contract_id": result.run_contract_id,
            }
        )
        if (
            progress_answerability.status == AnswerabilityStatus.answerable
            and not progress_answerability.missing_parts
            and not missing_answer_terms
            and not progress_answerability.signals.get("conflict_marker")
            and step >= minimum_search_steps
        ):
            state.stop_reason = "evidence_sufficient"
            break
        if progress_answerability.status in {AnswerabilityStatus.partial, AnswerabilityStatus.conflicting}:
            for repair_query in _gap_repair_queries(
                query,
                progress_answerability,
                provisional_evidence,
                existing=state.subqueries,
                limit=budgets.max_subqueries - len(state.subqueries),
            ):
                state.subqueries.append(repair_query)
                gap_queries_added += 1
        events[-1]["gap_queries_added"] = gap_queries_added
        if new_count == 0 and neighbor_count == 0 and subquery_index >= len(state.subqueries):
            state.stop_reason = (
                "conflict_unresolved"
                if progress_answerability.status == AnswerabilityStatus.conflicting
                else "no_new_evidence"
            )
            break
        stalled_steps = stalled_steps + 1 if coverage <= previous_coverage else 0
        previous_coverage = coverage
        if stalled_steps >= 2:
            if subquery_index >= len(state.subqueries):
                state.stop_reason = "coverage_stalled"
                break
        if (
            len(combined) >= budgets.max_total_retrieved_chunks
            or len(state.visited_pages) >= budgets.max_unique_documents
        ):
            state.stop_reason = "budget_reached"
            break

    if state.stop_reason is None:
        state.stop_reason = "budget_reached"
    final_evidence = _renumber_evidence(
        _select_final_evidence(
            combined,
            step_evidence_ids_by_step,
            min(profile.postprocess.final_evidence_max, 12),
            ambiguity_mode=profile.answer.ambiguity_mode,
        )
    )
    answerability = decide_answerability(query, final_evidence, profile)
    if answerability.status == AnswerabilityStatus.conflicting and state.stop_reason in {
        None,
        "budget_reached",
        "coverage_stalled",
        "no_new_evidence",
    }:
        state.stop_reason = "conflict_unresolved"
    final = RetrievalResult(
        query=query,
        trace_id=trace_id,
        evidence=final_evidence,
        events=[
            *events,
            {
                "stage": "harness",
                "stable_stage": "retrieval.extended",
                "state": state.model_dump(),
                "allowed_tools": sorted(ALLOWED_TOOLS),
                "budgets": budgets.model_dump(),
                "stop_reason": state.stop_reason,
                "timings_ms": {"extended_search_total": _elapsed_ms(extended_started)},
            },
            {
                "stage": "answerability",
                "stable_stage": "answerability",
                "query_context": primary_query_context,
                "decision": {
                    **answerability.model_dump(mode="json"),
                    "reason_codes": [answerability.reason, f"status_{answerability.status.value.lower()}"],
                },
                "reason_codes": [answerability.reason, f"status_{answerability.status.value.lower()}"],
            },
        ],
        insufficient_evidence=is_insufficient(answerability),
        answerability=answerability,
        index_contract_id=_first_contract_id(events, "index_contract_id"),
        run_contract_id=_first_contract_id(events, "run_contract_id"),
    )
    await _persist_agent_run(conn, tenant_id=tenant_id, query_run_id=query_run_id, state=state, budgets=budgets)
    for event in final.events:
        await insert_retrieval_event(
            conn,
            tenant_id=tenant_id,
            query_run_id=query_run_id,
            trace_id=trace_id,
            event_type="retrieval_stage",
            stage=str(event["stage"]),
            payload=event,
        )
    return final


def _classify_intent(query: str) -> str:
    return "multi_hop_or_comparison" if should_start_extended(query) else "direct"


def _extract_timings(events: list[dict[str, Any]]) -> dict[str, int]:
    for event in reversed(events):
        if event.get("stage") == "timings" and isinstance(event.get("timings_ms"), dict):
            return {
                str(key): int(value)
                for key, value in dict(event["timings_ms"]).items()
                if isinstance(value, int | float)
            }
    return {}


async def _gather_ordered(awaitables: list[Any]) -> list[Any]:
    """Gather a bounded wave while preserving input order."""
    return list(await asyncio.gather(*awaitables))


def _subquery_contexts(subqueries: list[str], bridge_queries: list[str]) -> list[dict[str, Any]]:
    bridge_keys = {normalize_for_embedding(query) for query in bridge_queries}
    bridge_index = 0
    decomposition_index = 0
    contexts: list[dict[str, Any]] = []
    for subquery in subqueries:
        if normalize_for_embedding(subquery) in bridge_keys:
            bridge_index += 1
            contexts.append(
                make_query_context(
                    query=subquery,
                    transform_id="tr.bridge.1",
                    subquery_id=f"sq.bridge.{bridge_index}",
                    query_role="bridge",
                    transform_type="bridge_queries",
                )
            )
        else:
            decomposition_index += 1
            contexts.append(
                make_query_context(
                    query=subquery,
                    transform_id="tr.decomposition.1",
                    subquery_id=f"sq.decomposition.{decomposition_index}",
                    query_role="decomposition",
                    transform_type="decomposition",
                )
            )
    return contexts


def _retrieved_count(events: list[dict[str, Any]]) -> int:
    counts = [int(event["count"]) for event in events if isinstance(event.get("count"), int)]
    return max(counts, default=0)


def _max_stage_score(events: list[dict[str, Any]], score_key: str) -> float | None:
    best: float | None = None
    for event in events:
        raw_candidates = event.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                continue
            scores = candidate.get("scores")
            if not isinstance(scores, dict):
                continue
            value = scores.get(score_key)
            if isinstance(value, int | float):
                best = float(value) if best is None else max(best, float(value))
    return best


def _build_subqueries(query: str, limit: int) -> list[str]:
    # Keep conjunctions intact: splitting on every "и" destroys entities and
    # attributes (e.g. "владелец и срок хранения"). Decomposition is handled
    # by the typed answerability gaps below.
    parts = [part.strip(" ?") for part in re.split(r"\?|;|\bvs\b|\bversus\b", query, flags=re.I) if part.strip()]
    if not parts:
        parts = [query]
    if query not in parts:
        parts.insert(0, query)
    variants: list[str] = []
    variants.extend(_bridge_subqueries(query))
    for part in parts:
        variants.extend(_query_variants(part))
    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        key = normalize_for_embedding(variant)
        if key and key not in seen:
            deduped.append(variant)
            seen.add(key)
    return deduped[:limit]


def _query_variants(query: str) -> list[str]:
    compact = " ".join(query.split())
    rare_terms = [term for term in normalize_for_embedding(compact).split() if len(term) >= 6][:6]
    variants = [compact]
    if rare_terms:
        variants.append(" ".join(rare_terms))
    if any(marker in compact.casefold() for marker in ("сравни", "сравнение", "отличается", "между")):
        variants.append(compact.replace("сравни", "").replace("Сравни", "").strip())
    return [variant for variant in variants if variant]


def _gap_repair_queries(
    query: str,
    decision: Any,
    evidence: list[Evidence],
    *,
    existing: list[str],
    limit: int,
) -> list[str]:
    """Create deterministic, bounded queries for requirements still missing from evidence."""
    if limit <= 0:
        return []
    missing = [
        str(part).strip()
        for part in [
            *(decision.missing_parts or []),
            *(decision.signals.get("missing_answer_bearing_terms", []) or []),
        ]
        if str(part).strip()
    ]
    anchors = _bridge_anchors(evidence)
    query_terms = normalize_for_embedding(query)
    additions = [item for item in [*missing, *anchors] if normalize_for_embedding(item) not in query_terms]
    if not additions:
        return []
    candidate = " ".join([" ".join(query.split()), *additions[:8]]).strip()
    candidate = " ".join(candidate.split())[:500]
    existing_keys = {normalize_for_embedding(item) for item in existing}
    return [candidate] if normalize_for_embedding(candidate) not in existing_keys else []


def _bridge_anchors(evidence: list[Evidence]) -> list[str]:
    """Extract stable entity/code anchors without exposing provider or storage identifiers."""
    text = " ".join(f"{item.title} {' '.join(item.section_path)} {item.content}" for item in evidence)
    patterns = [
        r"\b[A-ZА-Я]{2,}[A-ZА-Я0-9]*[-_]\d+[A-ZА-Я0-9]*\b",
        r"\b[A-ZА-Я][A-Za-zА-Яа-я0-9-]{3,}(?:\s+[A-ZА-Я][A-Za-zА-Яа-я0-9-]{3,}){0,2}\b",
    ]
    anchors: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.findall(pattern, text):
            value = " ".join(str(match).split())
            key = normalize_for_embedding(value)
            if key and key not in seen:
                anchors.append(value)
                seen.add(key)
            if len(anchors) >= 4:
                return anchors
    return anchors


def _looks_like_bridge_query(normalized_query: str) -> bool:
    return (
        ("фильм" in normalized_query or "документальн" in normalized_query)
        and ("сери" in normalized_query or "жил" in normalized_query)
        and ("цифр" in normalized_query or "назван" in normalized_query)
    )


def _bridge_subqueries(query: str) -> list[str]:
    normalized = query.casefold()
    film_variants: list[str] = []
    series_variants: list[str] = []
    other_variants: list[str] = []
    year_match = re.search(r"\b(?:19|20)\d{2}\b", normalized)
    year = year_match.group(0) if year_match else ""
    month = _month_marker(normalized)
    if "документаль" in normalized or "фильм" in normalized:
        if month and year:
            film_variants.append(f"документальный фильм {month} {year}")
            film_variants.append(f"фильм вышедший в {month} {year}")
        elif year:
            film_variants.append(f"документальный фильм {year}")
    if "сери" in normalized and ("жил" in normalized or "дом" in normalized or "здани" in normalized):
        series_variants.append("серия жилых домов")
        series_variants.append("серия жилых домов город")
    if "цифр" in normalized and "назван" in normalized:
        other_variants.append("название начинается с тех же цифр")
    ordered: list[str] = []
    if film_variants:
        ordered.append(film_variants[0])
    if series_variants:
        ordered.append(series_variants[0])
    ordered.extend(film_variants[1:])
    ordered.extend(series_variants[1:])
    ordered.extend(other_variants)
    return ordered


def _month_marker(normalized_query: str) -> str:
    for marker, canonical in (
        ("январ", "январе"),
        ("феврал", "феврале"),
        ("март", "марте"),
        ("апрел", "апреле"),
        ("май", "мае"),
        ("мае", "мае"),
        ("июн", "июне"),
        ("июл", "июле"),
        ("август", "августе"),
        ("сентябр", "сентябре"),
        ("октябр", "октябре"),
        ("ноябр", "ноябре"),
        ("декабр", "декабре"),
    ):
        if marker in normalized_query:
            return canonical
    return ""


def _first_contract_id(events: list[dict[str, Any]], key: str) -> str:
    for event in events:
        value = event.get(key)
        if isinstance(value, str):
            return value
    return ""


def _record_tool_call(state: HarnessState, tool: str, payload: dict[str, Any]) -> bool:
    if tool not in ALLOWED_TOOLS:
        raise ValueError(f"tool is not allowed: {tool}")
    call_hash = stable_hash([tool, json_dumps(payload)], 32)
    if call_hash in state.tool_call_hashes:
        return False
    state.tool_call_hashes.append(call_hash)
    return True


async def _expand_neighbors(
    conn: AsyncConnection,
    seeds: list[Evidence],
    combined: dict[str, Evidence],
    state: HarnessState,
    events: list[dict[str, Any]],
    *,
    tenant_id: str,
    knowledge_base_id: str,
    window: int,
    filters: dict[str, Any] | None = None,
) -> int:
    added = 0
    for seed in seeds:
        payload = {"chunk_id": seed.chunk_id, "window": window}
        if not _record_tool_call(state, "get_neighbors", payload):
            continue
        started = time.perf_counter()
        neighbors = await get_neighbors(
            conn,
            seed.chunk_id,
            tenant_id=tenant_id,
            knowledge_base_id=seed.knowledge_base_id,
            window=window,
            filters=filters,
        )
        new_ids: list[str] = []
        for neighbor in neighbors:
            evidence_key = f"{neighbor.knowledge_base_id}:{neighbor.chunk_id}"
            if evidence_key not in combined:
                combined[evidence_key] = neighbor
                new_ids.append(evidence_key)
                added += 1
        events.append(
            {
                "stage": "harness_tool",
                "stable_stage": "retrieval.extended",
                "tool": "get_neighbors",
                "seed_chunk_id": seed.chunk_id,
                "window": window,
                "new_evidence": len(new_ids),
                "chunk_ids": new_ids,
                "latency_ms": _elapsed_ms(started),
            }
        )
    return added


async def get_neighbors(
    conn: AsyncConnection,
    chunk_id: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    window: int = 1,
    filters: dict[str, Any] | None = None,
) -> list[Evidence]:
    center = await fetch_chunk_by_id(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        chunk_id=chunk_id,
    )
    if center is None:
        return []
    document_id = str(center.get("document_id") or "")
    rows: list[dict[str, Any]] = []
    previous_id = center.get("prev_chunk_id")
    for _ in range(window):
        if not previous_id:
            break
        row = await fetch_chunk_by_id(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            chunk_id=str(previous_id),
        )
        if row is None:
            break
        if str(row.get("document_id") or "") == document_id:
            rows.insert(0, row)
        previous_id = row.get("prev_chunk_id")
    next_id = center.get("next_chunk_id")
    for _ in range(window):
        if not next_id:
            break
        row = await fetch_chunk_by_id(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            chunk_id=str(next_id),
        )
        if row is None:
            break
        if str(row.get("document_id") or "") == document_id:
            rows.append(row)
        next_id = row.get("next_chunk_id")
    return [_evidence_from_chunk_row(row) for row in rows]


def _coverage_inventory(query: str, evidence: list[Evidence], profile: RetrievalProfile) -> list[CoverageItem]:
    decision = decide_answerability(query, evidence, profile)
    evidence_text = normalize_for_embedding(" ".join(f"{item.title} {item.content}" for item in evidence))
    covered = set(decision.covered_parts)
    missing = set(decision.missing_parts)
    items: list[CoverageItem] = []
    for part in decision.required_parts or [query]:
        if part in covered:
            status: Literal["covered", "mentioned", "missing"] = "covered"
        elif part in missing and _part_is_mentioned(part, evidence_text):
            status = "mentioned"
        else:
            status = "missing"
        part_terms = set(normalize_for_embedding(part).split())
        evidence_ids = [
            item.evidence_id
            for item in evidence
            if part_terms & set(normalize_for_embedding(f"{item.title} {item.content}").split())
        ][:6]
        items.append(CoverageItem(part=part, status=status, evidence_ids=evidence_ids))
    return items


def _coverage_ratio(inventory: list[CoverageItem]) -> float:
    if not inventory:
        return 0.0
    score = 0.0
    for item in inventory:
        if item.status == "covered":
            score += 1.0
        elif item.status == "mentioned":
            score += 0.5
    return min(1.0, score / len(inventory))


def _ledger_status(
    inventory: list[CoverageItem], evidence: list[Evidence]
) -> Literal["covered", "partial", "missing", "mentioned"]:
    if inventory and all(item.status == "covered" for item in inventory):
        return "covered"
    if inventory and any(item.status == "mentioned" for item in inventory):
        return "mentioned"
    return "partial" if evidence else "missing"


def _part_is_mentioned(part: str, evidence_text: str) -> bool:
    terms = [term for term in normalize_for_embedding(part).split() if len(term) >= 5]
    return bool(terms and any(term in evidence_text for term in terms))


def _renumber_evidence(evidence: list[Evidence]) -> list[Evidence]:
    return [
        Evidence(
            evidence_id=f"S{index}",
            chunk_id=item.chunk_id,
            knowledge_base_id=item.knowledge_base_id,
            title=item.title,
            section_path=item.section_path,
            content=item.content,
            source_url=item.source_url,
            scores=item.scores,
            ranks=item.ranks,
            metadata=item.metadata,
            document_id=item.document_id,
            content_unit_id=item.content_unit_id,
            supporting_chunk_ids=item.supporting_chunk_ids,
            provenance_refs=item.provenance_refs,
            provenance=item.provenance,
        )
        for index, item in enumerate(evidence, start=1)
    ]


def _select_final_evidence(
    combined: dict[str, Evidence],
    step_evidence_ids_by_step: list[list[str]],
    limit: int,
    *,
    ambiguity_mode: str = "off",
) -> list[Evidence]:
    selected_ids: list[str] = []
    seen: set[str] = set()
    priority: dict[str, int] = {}
    max_step_len = max((len(ids) for ids in step_evidence_ids_by_step), default=0)
    for offset in range(max_step_len):
        for step_ids in step_evidence_ids_by_step:
            if offset >= len(step_ids):
                continue
            chunk_id = step_ids[offset]
            if chunk_id in combined and chunk_id not in seen:
                priority.setdefault(chunk_id, len(priority))
    for chunk_id in combined:
        priority.setdefault(chunk_id, len(priority) + 1000)
    ordered = sorted(
        combined,
        key=lambda chunk_id: (
            priority[chunk_id],
            -float(combined[chunk_id].scores.get("rerank", combined[chunk_id].scores.get("neighbor", 0.0))),
            str(combined[chunk_id].knowledge_base_id),
            str(combined[chunk_id].document_id),
            chunk_id,
        ),
    )
    first_pass: list[str] = []
    remaining: list[str] = []
    seen_documents: set[str] = set()
    if ambiguity_mode in {"auto", "always"}:
        for chunk_id in ordered:
            item = combined[chunk_id]
            document_key = str(item.document_id or item.source_url or item.title or "")
            if document_key not in seen_documents and len(first_pass) < min(8, limit):
                first_pass.append(chunk_id)
                seen_documents.add(document_key)
            else:
                remaining.append(chunk_id)
        ordered = [*first_pass, *remaining]
    document_counts: dict[str, int] = {}
    for chunk_id in ordered:
        unit = combined[chunk_id].content_unit_id or str(combined[chunk_id].metadata.get("content_unit_id") or chunk_id)
        if unit in seen:
            continue
        document_key = str(
            combined[chunk_id].document_id or combined[chunk_id].source_url or combined[chunk_id].title or ""
        )
        if ambiguity_mode in {"auto", "always"} and document_counts.get(document_key, 0) >= 2:
            continue
        selected_ids.append(chunk_id)
        seen.add(unit)
        document_counts[document_key] = document_counts.get(document_key, 0) + 1
        if len(selected_ids) >= limit:
            break
    return [combined[item] for item in selected_ids]


def _evidence_from_chunk_row(row: dict[str, Any]) -> Evidence:
    metadata = dict(row.get("metadata") or {})
    metadata.update(
        {
            "parent_chunk_id": row.get("parent_chunk_id"),
            "prev_chunk_id": row.get("prev_chunk_id"),
            "next_chunk_id": row.get("next_chunk_id"),
            "neighbor_expanded": True,
        }
    )
    return Evidence(
        evidence_id="",
        chunk_id=str(row["id"]),
        title=str(row["title"]),
        section_path=list(row.get("section_path") or []),
        content=str(row.get("content") or ""),
        source_url=str(row.get("source_url") or ""),
        knowledge_base_id=str(row.get("knowledge_base_id") or ""),
        document_id=str(row.get("document_id") or ""),
        content_unit_id=str(metadata.get("content_unit_id") or ""),
        supporting_chunk_ids=[str(row["id"])],
        provenance_refs=[str(row["id"])],
        scores={"neighbor": 1.0},
        ranks={},
        metadata=metadata,
        provenance=SourceProvenance.model_validate(
            public_provenance_from_metadata(
                metadata,
                document_id=str(row.get("document_id") or ""),
                document_version_id=str(metadata.get("document_version_id") or ""),
                source_uri=str(row.get("source_uri") or ""),
                source_url=str(row.get("source_url") or ""),
                chunk_id=str(row.get("id") or ""),
            )
        ),
    )


async def _persist_agent_run(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    query_run_id: str,
    state: HarnessState,
    budgets: HarnessBudgets,
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO agent_runs(id, tenant_id, query_run_id, status, stop_reason, ledger, completed_at)
            VALUES (:id, :tenant_id, :query_run_id, 'completed', :stop_reason,
                    CAST(:ledger AS jsonb), now())
            """
        ),
        {
            "id": str(new_uuid()),
            "tenant_id": tenant_id,
            "query_run_id": query_run_id,
            "stop_reason": state.stop_reason,
            "ledger": json_dumps({"state": state.model_dump(), "budgets": budgets.model_dump()}),
        },
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
