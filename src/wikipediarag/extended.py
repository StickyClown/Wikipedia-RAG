from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.config import Settings
from wikipediarag.db import json_dumps
from wikipediarag.ids import new_uuid
from wikipediarag.retrieval import retrieve
from wikipediarag.schemas import RetrievalResult


async def run_extended_search(
    conn: AsyncConnection,
    query: str,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    query_run_id: str,
    trace_id: str,
    settings: Settings,
) -> RetrievalResult:
    max_steps = 3
    ledger: dict[str, Any] = {"questions": [], "tool_call_hashes": [], "stop_reason": None}
    result = await retrieve(
        conn,
        query,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        query_run_id=query_run_id,
        trace_id=trace_id,
        settings=settings,
        top_k=12,
    )
    ledger["questions"].append(
        {
            "subquery_id": "q0",
            "text": query,
            "status": "covered" if not result.insufficient_evidence else "partial",
            "evidence_ids": [item.evidence_id for item in result.evidence],
        }
    )
    ledger["stop_reason"] = "evidence_sufficient" if not result.insufficient_evidence else "budget_reached"
    ledger["steps_used"] = 1
    ledger["max_steps"] = max_steps
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
            "stop_reason": ledger["stop_reason"],
            "ledger": json_dumps(ledger),
        },
    )
    return result
