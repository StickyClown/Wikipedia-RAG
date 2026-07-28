from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.answerability import decide_answerability, is_insufficient
from wikipediarag.config import Settings
from wikipediarag.db import json_dumps
from wikipediarag.ids import new_uuid, stable_hash
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
    status: Literal["covered", "partial", "missing"]
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
    coverage: float = 0.0
    stop_reason: StopReason | None = None


ALLOWED_TOOLS = {
    "search",
    "fetch_chunk",
    "fetch_section",
    "fetch_document",
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
    ]
    return any(marker in normalized for marker in markers) or normalized.count("?") > 1


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
    combined: dict[str, Evidence] = {}
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
        for evidence in result.evidence:
            if evidence.chunk_id not in combined:
                combined[evidence.chunk_id] = evidence
                new_count += 1
            page = str(evidence.metadata.get("zim_entry_path") or evidence.title)
            if page and page not in state.visited_pages:
                state.visited_pages.append(page)
        coverage = min(1.0, len(combined) / max(profile.postprocess.final_evidence_min, 1))
        state.coverage = coverage
        state.evidence_ledger.append(
            EvidenceLedgerItem(
                subquery_id=f"q{step - 1}",
                text=subquery,
                status="covered" if coverage >= 1.0 else "partial" if result.evidence else "missing",
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
                "coverage": coverage,
                "latency_ms": tool_latency_ms,
                "retrieval_timings_ms": retrieval_timings,
                "index_contract_id": result.index_contract_id,
                "run_contract_id": result.run_contract_id,
            }
        )
        if coverage >= 1.0:
            state.stop_reason = "evidence_sufficient"
            break
        if new_count == 0:
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
    final_evidence = _renumber_evidence(list(combined.values())[: profile.postprocess.final_evidence_max])
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
    return parts[:limit]


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
