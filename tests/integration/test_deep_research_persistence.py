from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from wikipediarag.config import Settings
from wikipediarag.db import ensure_schema, get_engine
from wikipediarag.deep_research import _finish_partial_run
from wikipediarag.repository import (
    create_query_run,
    create_research_episode,
    create_research_run,
    create_research_tool_call,
    get_research_run,
    insert_research_claim_record,
    load_research_detail_records,
    upsert_research_coverage_record,
    upsert_research_evidence_record,
)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("WIKIPEDIARAG_INTEGRATION_DATABASE_URL"),
    reason="set WIKIPEDIARAG_INTEGRATION_DATABASE_URL to run PostgreSQL integration tests",
)
async def test_deep_research_persistence_stages_are_idempotent() -> None:
    database_url = os.environ["WIKIPEDIARAG_INTEGRATION_DATABASE_URL"]
    settings = Settings(database_url=database_url)
    await ensure_schema(settings)
    engine = get_engine(settings)

    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            run_id, job_id = await create_research_run(
                conn,
                tenant_id=settings.default_tenant_id,
                knowledge_base_id=settings.default_kb_id,
                user_id=settings.default_user_id,
                topic="persistence integration",
                retrieval_profile="test_mock",
                tool_mode="extended_search_only",
                retrieval_overrides={},
                context_policy={},
                questions=["Question?"],
            )
            question_row = (
                (
                    await conn.execute(
                        text("SELECT id FROM research_questions WHERE research_run_id = :run_id"),
                        {"run_id": str(run_id)},
                    )
                )
                .mappings()
                .first()
            )
            assert question_row is not None
            question_id = str(question_row["id"])

            request_id = str(uuid.uuid4())
            query_run_a = await create_query_run(
                conn,
                tenant_id=settings.default_tenant_id,
                knowledge_base_id=settings.default_kb_id,
                user_id=settings.default_user_id,
                request_id=request_id,
                client_request_id=None,
                mode="deep_research",
                input_text="Question?",
                trace_id="integration",
            )
            query_run_b = await create_query_run(
                conn,
                tenant_id=settings.default_tenant_id,
                knowledge_base_id=settings.default_kb_id,
                user_id=settings.default_user_id,
                request_id=request_id,
                client_request_id=None,
                mode="deep_research",
                input_text="Question?",
                trace_id="integration",
            )
            assert query_run_a == query_run_b

            episode_a = await create_research_episode(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                episode_index=1,
                question_id=question_id,
                query_run_id=str(query_run_a),
                context_summary={},
            )
            episode_b = await create_research_episode(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                episode_index=1,
                question_id=question_id,
                query_run_id=str(query_run_a),
                context_summary={},
            )
            assert episode_a == episode_b

            tool_a = await create_research_tool_call(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                episode_id=str(episode_a),
                question_id=question_id,
                query_run_id=str(query_run_a),
                tool_name="extended_search",
                tool_query_hash="same-query",
                safe_metadata={"tool_name": "extended_search"},
            )
            tool_b = await create_research_tool_call(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                episode_id=str(episode_a),
                question_id=question_id,
                query_run_id=str(query_run_a),
                tool_name="extended_search",
                tool_query_hash="same-query",
                safe_metadata={"tool_name": "extended_search"},
            )
            assert tool_a == tool_b

            document_id = f"integration-{uuid.uuid4().hex}"
            chunk_id = f"{document_id}-chunk"
            await conn.execute(
                text(
                    "INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri) "
                    "VALUES (:id, :tenant_id, :kb_id, 'integration', 'integration', 'integration://document')"
                ),
                {"id": document_id, "tenant_id": settings.default_tenant_id, "kb_id": settings.default_kb_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO chunks(id, tenant_id, knowledge_base_id, document_id, title, content, source_uri, "
                    "source_url, content_hash) VALUES (:id, :tenant_id, :kb_id, :document_id, 'integration', "
                    "'evidence', 'integration://document', 'integration://document', 'hash')"
                ),
                {
                    "id": chunk_id,
                    "tenant_id": settings.default_tenant_id,
                    "kb_id": settings.default_kb_id,
                    "document_id": document_id,
                },
            )
            evidence_kwargs: dict[str, Any] = {
                "tenant_id": settings.default_tenant_id,
                "research_run_id": str(run_id),
                "question_id": question_id,
                "chunk_id": chunk_id,
                "document_id": document_id,
                "document_version_id": None,
                "knowledge_base_id": settings.default_kb_id,
                "evidence_ref": "E1",
                "title": "integration",
                "source_url": "integration://document",
                "section_path": [],
                "content_abstract": "evidence",
                "support_status": "supports",
                "score": 1.0,
                "metadata": {},
            }
            evidence_a = await upsert_research_evidence_record(conn, **evidence_kwargs)
            evidence_b = await upsert_research_evidence_record(conn, **evidence_kwargs)
            assert evidence_a == evidence_b

            coverage_a = await upsert_research_coverage_record(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                question_id=question_id,
                status="covered",
                required_evidence_count=1,
                linked_evidence_ids=[str(evidence_a)],
                reason="integration",
                metrics={},
            )
            coverage_b = await upsert_research_coverage_record(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                question_id=question_id,
                status="covered",
                required_evidence_count=1,
                linked_evidence_ids=[str(evidence_a)],
                reason="integration",
                metrics={},
            )
            claim_a = await insert_research_claim_record(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                question_id=question_id,
                claim_text="integration claim",
                support_status="supported",
                evidence_ids=[str(evidence_a)],
                metadata={},
            )
            claim_b = await insert_research_claim_record(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
                question_id=question_id,
                claim_text="integration claim",
                support_status="supported",
                evidence_ids=[str(evidence_a)],
                metadata={},
            )
            assert coverage_a == coverage_b
            assert claim_a == claim_b
            question_attempt = (
                (
                    await conn.execute(
                        text("SELECT attempt_count FROM research_questions WHERE id = :question_id"),
                        {"question_id": question_id},
                    )
                )
                .mappings()
                .first()
            )
            assert question_attempt is not None
            assert question_attempt["attempt_count"] == 1

            run = await get_research_run(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
            )
            assert run is not None
            records = await load_research_detail_records(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
            )
            assert all("validated_args" not in row for row in records["tool_calls"])
            assert "tool_routing_history" in records
            await _finish_partial_run(
                conn,
                job_id=str(job_id),
                run=run,
                records=records,
                reason="run_deadline_exhausted",
                error_code="run_deadline_exhausted",
            )

            persisted_run = await get_research_run(
                conn,
                tenant_id=settings.default_tenant_id,
                research_run_id=str(run_id),
            )
            assert persisted_run is not None
            assert persisted_run["status"] == "completed"
            assert persisted_run["progress"]["stage"] == "completed_partial"
            assert persisted_run["stop_reason"] == "run_deadline_exhausted"
            assert persisted_run["final_report"]["synthesis"]
            report_text = str(persisted_run["final_report"])
            assert "Confirmed findings" in report_text
            assert "Incomplete findings" in report_text
            assert "Unresolved questions" in report_text
            assert "Used evidence" in report_text
            assert "Limitations" in report_text

            question_state = (
                (
                    await conn.execute(
                        text("SELECT execution_state, outcome FROM research_questions WHERE id = :question_id"),
                        {"question_id": question_id},
                    )
                )
                .mappings()
                .first()
            )
            assert dict(question_state or {}) == {"execution_state": "done", "outcome": "exhausted"}
            job_state = (
                (
                    await conn.execute(
                        text("SELECT status, error_code FROM ingestion_jobs WHERE id = :job_id"),
                        {"job_id": str(job_id)},
                    )
                )
                .mappings()
                .first()
            )
            assert dict(job_state or {}) == {"status": "completed", "error_code": "run_deadline_exhausted"}
        finally:
            await transaction.rollback()
