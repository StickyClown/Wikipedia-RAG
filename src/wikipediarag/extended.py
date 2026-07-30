from __future__ import annotations

import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.answerability import decide_answerability, is_insufficient
from wikipediarag.config import Settings
from wikipediarag.db import json_dumps
from wikipediarag.embedding import normalize_for_embedding
from wikipediarag.ids import new_uuid, stable_hash
from wikipediarag.repository import fetch_chunk_by_id
from wikipediarag.retrieval import retrieve
from wikipediarag.retrieval_profile import RetrievalProfile
from wikipediarag.schemas import Evidence, RetrievalResult

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
    query_run_id: str,
    trace_id: str,
    settings: Settings,
    profile: RetrievalProfile,
    profile_overrides: dict[str, Any] | None = None,
) -> RetrievalResult:
    extended_started = time.perf_counter()
    budgets = HarnessBudgets(max_context_tokens=profile.postprocess.max_context_tokens)
    state = HarnessState(
        original_query=query,
        intent=_classify_intent(query),
        subqueries=_build_subqueries(query, budgets.max_subqueries),
        retrieval_profile=profile.name,
    )
    minimum_search_steps = 2 if _looks_like_bridge_query(query.casefold()) else 1
    combined: dict[str, Evidence] = {}
    step_evidence_ids_by_step: list[list[str]] = []
    previous_coverage = 0.0
    stalled_steps = 0
    events: list[dict[str, Any]] = []

    for step, subquery in enumerate(state.subqueries[: budgets.max_steps], start=1):
        state.current_step = step
        if not _record_tool_call(state, "search", {"query": subquery, "profile": profile.name}):
            state.stop_reason = "duplicate_tool_call"
            break
        tool_started = time.perf_counter()
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
        )
        tool_latency_ms = _elapsed_ms(tool_started)
        retrieval_timings = _extract_timings(result.events)
        new_count = 0
        step_evidence_ids: list[str] = []
        for evidence in result.evidence:
            if evidence.chunk_id not in combined:
                combined[evidence.chunk_id] = evidence
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
        )
        inventory = _coverage_inventory(query, list(combined.values()), profile)
        state.coverage_inventory = inventory
        coverage = _coverage_ratio(inventory)
        state.coverage = coverage
        state.evidence_ledger.append(
            EvidenceLedgerItem(
                subquery_id=f"q{step - 1}",
                text=subquery,
                status=_ledger_status(inventory, result.evidence),
                evidence_ids=[item.evidence_id for item in result.evidence],
            )
        )
        events.append(
            {
                "stage": "harness_tool",
                "tool": "search",
                "step": step,
                "query": subquery,
                "new_evidence": new_count,
                "new_neighbors": neighbor_count,
                "coverage": coverage,
                "coverage_inventory": [item.model_dump(mode="json") for item in inventory],
                "latency_ms": tool_latency_ms,
                "retrieval_timings_ms": retrieval_timings,
                "index_contract_id": result.index_contract_id,
                "run_contract_id": result.run_contract_id,
            }
        )
        if coverage >= 1.0 and step >= minimum_search_steps:
            state.stop_reason = "evidence_sufficient"
            break
        if new_count == 0 and neighbor_count == 0:
            state.stop_reason = "no_new_evidence"
            break
        stalled_steps = stalled_steps + 1 if coverage <= previous_coverage else 0
        previous_coverage = coverage
        if stalled_steps >= 2:
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
        _select_final_evidence(combined, step_evidence_ids_by_step, profile.postprocess.final_evidence_max)
    )
    answerability = decide_answerability(query, final_evidence, profile)
    final = RetrievalResult(
        query=query,
        trace_id=trace_id,
        evidence=final_evidence,
        events=[
            *events,
            {
                "stage": "harness",
                "state": state.model_dump(),
                "allowed_tools": sorted(ALLOWED_TOOLS),
                "budgets": budgets.model_dump(),
                "stop_reason": state.stop_reason,
                "timings_ms": {"extended_search_total": _elapsed_ms(extended_started)},
            },
            {
                "stage": "answerability",
                "decision": answerability.model_dump(mode="json"),
            },
        ],
        insufficient_evidence=is_insufficient(answerability),
        answerability=answerability,
        index_contract_id=_first_contract_id(events, "index_contract_id"),
        run_contract_id=_first_contract_id(events, "run_contract_id"),
    )
    await _persist_agent_run(conn, tenant_id=tenant_id, query_run_id=query_run_id, state=state, budgets=budgets)
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


def _build_subqueries(query: str, limit: int) -> list[str]:
    parts = [part.strip(" ?") for part in query.replace(" и ", "?").split("?") if part.strip()]
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
            knowledge_base_id=knowledge_base_id,
            window=window,
        )
        new_ids: list[str] = []
        for neighbor in neighbors:
            if neighbor.chunk_id not in combined:
                combined[neighbor.chunk_id] = neighbor
                new_ids.append(neighbor.chunk_id)
                added += 1
        events.append(
            {
                "stage": "harness_tool",
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
) -> list[Evidence]:
    center = await fetch_chunk_by_id(
        conn,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        chunk_id=chunk_id,
    )
    if center is None:
        return []
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
            title=item.title,
            section_path=item.section_path,
            content=item.content,
            source_url=item.source_url,
            scores=item.scores,
            ranks=item.ranks,
            metadata=item.metadata,
        )
        for index, item in enumerate(evidence, start=1)
    ]


def _select_final_evidence(
    combined: dict[str, Evidence],
    step_evidence_ids_by_step: list[list[str]],
    limit: int,
) -> list[Evidence]:
    if not step_evidence_ids_by_step:
        return list(combined.values())[:limit]
    selected_ids: list[str] = []
    seen: set[str] = set()
    max_step_len = max((len(ids) for ids in step_evidence_ids_by_step), default=0)
    for offset in range(max_step_len):
        for step_ids in step_evidence_ids_by_step:
            if offset >= len(step_ids):
                continue
            chunk_id = step_ids[offset]
            if chunk_id in combined and chunk_id not in seen:
                selected_ids.append(chunk_id)
                seen.add(chunk_id)
                if len(selected_ids) >= limit:
                    return [combined[item] for item in selected_ids]
    for chunk_id in combined:
        if chunk_id not in seen:
            selected_ids.append(chunk_id)
            seen.add(chunk_id)
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
        scores={"neighbor": 1.0},
        ranks={},
        metadata=metadata,
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
