from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from wikipediarag.answerability import should_try_extended_search
from wikipediarag.answering import generate_answer
from wikipediarag.config import get_settings
from wikipediarag.db import connect, ensure_schema
from wikipediarag.document_ingestion import UploadValidationError, safe_upload_filename
from wikipediarag.extended import run_extended_search, should_start_extended
from wikipediarag.ids import stable_hash
from wikipediarag.repository import (
    complete_query_run,
    create_document_upload_records,
    create_ingestion_job,
    create_query_run,
    create_reprocess_job,
    create_upload_session,
    fail_query_run,
    get_document_public,
    get_knowledge_base,
    get_upload_session,
    list_document_versions_public,
    list_knowledge_bases,
    load_retrieval_events,
    request_cancel,
    request_resume,
)
from wikipediarag.retrieval import retrieve
from wikipediarag.retrieval_contract import KnowledgeBaseNotReady, validate_active_retrieval_contract
from wikipediarag.retrieval_profile import get_retrieval_profile
from wikipediarag.schemas import (
    ChatRequest,
    DebugSearchRequest,
    DocumentReprocessResponse,
    ImportRequest,
    KnowledgeBaseCreate,
    SseEvent,
    UploadCompleteResponse,
    UploadSessionAccepted,
    UploadSessionComplete,
    UploadSessionCreate,
    ZimImportRequest,
)
from wikipediarag.storage import create_presigned_put_url, head_object

app = FastAPI(title="WikipediaRag API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    await ensure_schema()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    settings = get_settings()
    components: dict[str, str] = {}
    try:
        async with connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["postgres"] = "ok"
    except Exception:
        components["postgres"] = "failed"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{settings.model_gateway_url.rstrip('/')}/ready")
            gateway_ready = response.status_code == 200 and response.json().get("status") == "ok"
            components["model_gateway"] = "ok" if gateway_ready else "failed"
    except Exception:
        components["model_gateway"] = "failed"
    status = "ok" if all(value == "ok" for value in components.values()) else "degraded"
    return {"status": status, "components": components}


@app.get("/api/v1/knowledge-bases")
async def get_knowledge_bases() -> list[dict[str, Any]]:
    settings = get_settings()
    async with connect() as conn:
        return await list_knowledge_bases(conn, settings.default_tenant_id)


@app.post("/api/v1/knowledge-bases")
async def create_knowledge_base(payload: KnowledgeBaseCreate) -> dict[str, str]:
    settings = get_settings()
    kb_id = str(uuid.uuid4())
    async with connect() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO knowledge_bases(id, tenant_id, name)
                VALUES (:id, :tenant_id, :name)
                """
            ),
            {"id": kb_id, "tenant_id": settings.default_tenant_id, "name": payload.name},
        )
    return {"id": kb_id, "name": payload.name}


@app.post("/api/v1/wikipedia/imports")
async def create_wikipedia_import(payload: ImportRequest) -> dict[str, str]:
    settings = get_settings()
    config = {
        "limit": payload.limit,
        "xml_path": payload.xml_path or str(settings.wiki_xml_path),
        "index_path": payload.index_path or str(settings.wiki_index_path),
        "snapshot_id": payload.snapshot_id or settings.wiki_snapshot_id,
        "retrieval_profile": settings.retrieval_profile,
    }
    async with connect() as conn:
        job_id = await create_ingestion_job(
            conn,
            settings.default_tenant_id,
            settings.default_kb_id,
            "wikipedia_xml",
            config,
        )
    return {"job_id": str(job_id)}


@app.post("/api/v1/wikipedia/zim-imports")
async def create_zim_import(payload: ZimImportRequest) -> dict[str, str]:
    settings = get_settings()
    config = {
        "limit": payload.limit or 10000,
        "zim_path": payload.zim_path,
        "zim_dir": str(settings.zim_dir),
        "zim_filename": payload.zim_filename or settings.zim_filename,
        "snapshot_id": payload.snapshot_id,
        "kiwix_public_base_url": settings.kiwix_public_base_url,
        "kiwix_book_name": settings.kiwix_book_name,
        "retrieval_profile": settings.retrieval_profile,
    }
    async with connect() as conn:
        job_id = await create_ingestion_job(
            conn,
            settings.default_tenant_id,
            settings.default_kb_id,
            "wikipedia_zim",
            config,
        )
    return {"job_id": str(job_id)}


@app.get("/api/v1/ingestion-jobs/{job_id}")
async def get_ingestion_job(job_id: str) -> dict[str, Any]:
    async with connect() as conn:
        result = await conn.execute(text("SELECT * FROM ingestion_jobs WHERE id = :id"), {"id": job_id})
        row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return cast(dict[str, Any], _jsonable(dict(row)))


@app.get("/api/v1/ingestion-jobs/{job_id}/events")
async def ingestion_job_events(job_id: str) -> StreamingResponse:
    async def event_stream() -> AsyncIterator[str]:
        sequence = 0
        while True:
            sequence += 1
            job = await get_ingestion_job(job_id)
            yield _sse("job.progress", {"sequence": sequence, "job": job})
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/v1/ingestion-jobs/{job_id}:cancel")
async def cancel_ingestion_job(job_id: str) -> dict[str, str]:
    async with connect() as conn:
        await request_cancel(conn, job_id)
    return {"status": "cancel_requested"}


@app.post("/api/v1/ingestion-jobs/{job_id}:resume")
async def resume_ingestion_job(job_id: str) -> dict[str, str]:
    async with connect() as conn:
        await request_resume(conn, job_id)
    return {"status": "resume_requested"}


@app.post("/api/v1/uploads/sessions")
async def create_upload_session_endpoint(payload: UploadSessionCreate) -> UploadSessionAccepted:
    settings = get_settings()
    try:
        filename = safe_upload_filename(payload.filename)
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail={"error": {"code": exc.code, "message": exc.safe_message}}) from exc
    kb_id = payload.knowledge_base_id or settings.default_kb_id
    async with connect() as conn:
        kb = await get_knowledge_base(conn, settings.default_tenant_id, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    object_key = (
        f"uploads/{settings.default_tenant_id}/{stable_hash([filename, payload.checksum_sha256], 16)}/"
        f"{payload.checksum_sha256}"
    )
    async with connect() as conn:
        session_id, expires_at = await create_upload_session(
            conn,
            tenant_id=settings.default_tenant_id,
            knowledge_base_id=kb_id,
            filename=filename,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            checksum_sha256=payload.checksum_sha256.lower(),
            object_key=object_key,
            parser_profile=payload.parser_profile,
            metadata=payload.metadata,
            ttl_seconds=settings.upload_session_ttl_seconds,
        )
    upload_url = await asyncio.to_thread(
        create_presigned_put_url,
        object_key,
        content_type=payload.content_type,
        expires_seconds=settings.upload_session_ttl_seconds,
        settings=settings,
    )
    return UploadSessionAccepted(
        upload_session_id=str(session_id),
        upload_url=upload_url,
        expires_at=expires_at,
        required_headers={"Content-Type": payload.content_type},
    )


@app.post("/api/v1/uploads/sessions/{upload_session_id}:complete")
async def complete_upload_session_endpoint(
    upload_session_id: str,
    payload: UploadSessionComplete | None = None,
) -> UploadCompleteResponse:
    settings = get_settings()
    async with connect() as conn:
        session = await get_upload_session(
            conn,
            tenant_id=settings.default_tenant_id,
            upload_session_id=upload_session_id,
        )
    if session is None:
        raise HTTPException(status_code=404, detail="upload session not found")
    if str(session["status"]) not in {"created", "uploaded"}:
        raise HTTPException(status_code=409, detail="upload session is not completable")
    expires_at = session["expires_at"]
    if isinstance(expires_at, datetime) and expires_at < datetime.now(UTC):
        raise HTTPException(status_code=409, detail="upload session expired")
    try:
        head = await asyncio.to_thread(head_object, str(session["object_key"]), settings)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="uploaded object is not available") from exc
    if int(head["content_length"]) != int(session["size_bytes"]):
        raise HTTPException(status_code=409, detail="uploaded object size mismatch")
    document_hash = stable_hash(
        [
            settings.default_tenant_id,
            str(session["knowledge_base_id"]),
            str(session["checksum_sha256"]),
            str(session["filename"]),
        ],
        24,
    )
    document_id = f"doc:{document_hash}"
    document_version_id = "docv:" + stable_hash(
        [
            document_id,
            str(session["checksum_sha256"]),
            str(session["parser_profile"]),
            "normalized_document_v1",
        ],
        32,
    )
    session_metadata = dict(session.get("metadata") or {})
    if payload is not None:
        session_metadata.update(payload.metadata)
    async with connect() as conn:
        job_id = await create_document_upload_records(
            conn,
            tenant_id=settings.default_tenant_id,
            knowledge_base_id=str(session["knowledge_base_id"]),
            upload_session={**session, "metadata": session_metadata},
            document_id=document_id,
            document_version_id=document_version_id,
            content_hash=str(session["checksum_sha256"]),
            metadata=session_metadata,
        )
    return UploadCompleteResponse(
        document_id=document_id,
        document_version_id=document_version_id,
        job_id=str(job_id),
        status="received",
    )


@app.get("/api/v1/documents/{document_id}")
async def get_document(document_id: str) -> dict[str, Any]:
    settings = get_settings()
    async with connect() as conn:
        document = await get_document_public(conn, settings.default_tenant_id, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return cast(dict[str, Any], _jsonable(document))


@app.get("/api/v1/documents/{document_id}/versions")
async def get_document_versions(document_id: str) -> dict[str, Any]:
    settings = get_settings()
    async with connect() as conn:
        versions = await list_document_versions_public(conn, settings.default_tenant_id, document_id)
    return {"document_id": document_id, "versions": _jsonable(versions)}


@app.post("/api/v1/documents/{document_id}:reprocess")
async def reprocess_document(document_id: str) -> DocumentReprocessResponse:
    settings = get_settings()
    async with connect() as conn:
        document = await get_document_public(conn, settings.default_tenant_id, document_id)
        if document is None or not document.get("current_version_id"):
            raise HTTPException(status_code=404, detail="document not found")
        job_id = await create_reprocess_job(
            conn,
            tenant_id=settings.default_tenant_id,
            knowledge_base_id=str(document["knowledge_base_id"]),
            document_id=document_id,
            document_version_id=str(document["current_version_id"]),
        )
    return DocumentReprocessResponse(
        document_id=document_id,
        document_version_id=str(document["current_version_id"]),
        job_id=str(job_id),
        status="received",
    )


@app.post("/api/v1/chat")
async def chat(payload: ChatRequest) -> StreamingResponse:
    settings = get_settings()
    request_id = str(uuid.uuid4())
    trace_id = stable_hash([request_id, payload.message], 32)
    active_profile = get_retrieval_profile(
        payload.retrieval_profile,
        settings,
        payload.retrieval_overrides,
    )
    kb_id = payload.knowledge_base_ids[0] if payload.knowledge_base_ids else settings.default_kb_id
    try:
        async with connect() as conn:
            await validate_active_retrieval_contract(
                conn,
                tenant_id=settings.default_tenant_id,
                knowledge_base_id=kb_id,
                profile=active_profile,
                retrieval_overrides=payload.retrieval_overrides,
                settings=settings,
            )
    except KnowledgeBaseNotReady as exc:
        raise _kb_not_ready_http(exc, request_id) from exc
    async with connect() as conn:
        query_run_id = await create_query_run(
            conn,
            tenant_id=settings.default_tenant_id,
            user_id=settings.default_user_id,
            request_id=request_id,
            client_request_id=payload.client_request_id,
            mode=payload.mode.value,
            input_text=payload.message,
            trace_id=trace_id,
        )

    async def event_stream() -> AsyncIterator[str]:
        sequence = 1
        current_stage = "question_received"
        last_successful_stage = "question_received"
        retrieval: Any | None = None
        yield _event(
            SseEvent(
                event="run.started",
                request_id=request_id,
                query_run_id=str(query_run_id),
                sequence=sequence,
                data={"trace_id": trace_id},
            )
        )
        try:
            async with connect() as conn:
                current_stage = "path_selected"
                use_harness_first = payload.mode.value == "extended" or (
                    active_profile.postprocess.extended_search in {"always", "conditional"}
                    and should_start_extended(payload.message)
                )
                last_successful_stage = "path_selected"
                if use_harness_first:
                    current_stage = "extended_search"
                    retrieval = await run_extended_search(
                        conn,
                        payload.message,
                        tenant_id=settings.default_tenant_id,
                        knowledge_base_id=kb_id,
                        query_run_id=str(query_run_id),
                        trace_id=trace_id,
                        settings=settings,
                        profile=active_profile,
                        profile_overrides=payload.retrieval_overrides,
                    )
                    last_successful_stage = "extended_search"
                else:
                    current_stage = "retrieval"
                    retrieval = await retrieve(
                        conn,
                        payload.message,
                        tenant_id=settings.default_tenant_id,
                        knowledge_base_id=kb_id,
                        query_run_id=str(query_run_id),
                        trace_id=trace_id,
                        settings=settings,
                        profile=active_profile,
                    )
                    last_successful_stage = "retrieval"
                    if (
                        retrieval.answerability
                        and should_try_extended_search(retrieval.answerability)
                        and active_profile.postprocess.extended_search
                        in {
                            "always",
                            "conditional",
                        }
                    ):
                        current_stage = "extended_search"
                        retrieval = await run_extended_search(
                            conn,
                            payload.message,
                            tenant_id=settings.default_tenant_id,
                            knowledge_base_id=kb_id,
                            query_run_id=str(query_run_id),
                            trace_id=trace_id,
                            settings=settings,
                            profile=active_profile,
                            profile_overrides=payload.retrieval_overrides,
                        )
                        last_successful_stage = "extended_search"
            current_stage = "answer_generation"
            answer, validation = await generate_answer(payload.message, retrieval, settings, active_profile)
            last_successful_stage = "answer_generation"
            timings_ms = _combined_timings(retrieval.model_dump(), validation)
            sequence += 1
            yield _event(
                SseEvent(
                    event="message.delta",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={
                        "text": answer,
                        "evidence": [item.model_dump() for item in retrieval.evidence],
                    },
                )
            )
            sequence += 1
            yield _event(
                SseEvent(
                    event="usage.updated",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={
                        "retrieval": retrieval.model_dump(),
                        "citation_validation": validation,
                        "timings_ms": timings_ms,
                    },
                )
            )
            async with connect() as conn:
                current_stage = "query_run_complete"
                await complete_query_run(
                    conn,
                    query_run_id=str(query_run_id),
                    answer=answer,
                    usage={
                        "citations": validation.get("citations", []),
                        "generation_usage": validation.get("usage", {}),
                        "provider": validation.get("provider"),
                        "provider_cost": validation.get("provider_cost"),
                        "model_alias": validation.get("model_alias"),
                        "timings_ms": timings_ms,
                        "index_contract_id": retrieval.index_contract_id,
                        "run_contract_id": retrieval.run_contract_id,
                    },
                    model_alias=str(validation.get("model_alias") or ""),
                    provider_request_id=str(validation.get("provider_request_id") or ""),
                )
                last_successful_stage = "query_run_complete"
            sequence += 1
            yield _event(
                SseEvent(
                    event="run.completed",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={"answer": answer},
                )
            )
        except asyncio.CancelledError:
            async with connect() as conn:
                await fail_query_run(conn, query_run_id=str(query_run_id), error_code="ClientDisconnected")
            raise
        except Exception as exc:
            async with connect() as conn:
                await fail_query_run(conn, query_run_id=str(query_run_id), error_code=type(exc).__name__)
            sequence += 1
            failure = _safe_failure_payload(
                exc,
                stage=current_stage,
                last_successful_stage=last_successful_stage,
                trace_id=trace_id,
                retrieval=retrieval,
            )
            yield _event(
                SseEvent(
                    event="run.failed",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data=failure,
                )
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/v1/query-runs/{query_run_id}/retrieval")
async def query_run_retrieval(query_run_id: str) -> dict[str, Any]:
    settings = get_settings()
    async with connect() as conn:
        events = await load_retrieval_events(conn, settings.default_tenant_id, query_run_id)
    return {"query_run_id": query_run_id, "events": _jsonable(events)}


@app.post("/api/v1/search:debug")
async def search_debug(payload: DebugSearchRequest) -> dict[str, Any]:
    settings = get_settings()
    trace_id = stable_hash(["debug", payload.message], 32)
    profile = get_retrieval_profile(payload.retrieval_profile, settings, payload.retrieval_overrides)
    kb_id = payload.knowledge_base_ids[0] if payload.knowledge_base_ids else settings.default_kb_id
    async with connect() as conn:
        try:
            result = await retrieve(
                conn,
                payload.message,
                tenant_id=settings.default_tenant_id,
                knowledge_base_id=kb_id,
                query_run_id=None,
                trace_id=trace_id,
                settings=settings,
                top_k=payload.top_k,
                profile=profile,
                profile_overrides=payload.retrieval_overrides,
            )
        except KnowledgeBaseNotReady as exc:
            raise _kb_not_ready_http(exc, trace_id) from exc
    return result.model_dump()


def _event(event: SseEvent) -> str:
    return _sse(event.event, event.model_dump(mode="json"))


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(_jsonable(data), ensure_ascii=False)}\n\n"


def _combined_timings(retrieval: dict[str, Any], validation: dict[str, object]) -> dict[str, int]:
    timings: dict[str, int] = {}
    for event in retrieval.get("events", []):
        if not isinstance(event, dict):
            continue
        if event.get("stage") == "timings" and isinstance(event.get("timings_ms"), dict):
            timings.update(_safe_timing_dict(event["timings_ms"]))
        if event.get("stage") == "harness_tool" and isinstance(event.get("latency_ms"), int | float):
            timings["extended_tool_search_total"] = timings.get("extended_tool_search_total", 0) + int(
                event["latency_ms"]
            )
        if event.get("stage") == "harness" and isinstance(event.get("timings_ms"), dict):
            timings.update(_safe_timing_dict(event["timings_ms"]))
    validation_timings = validation.get("timings_ms")
    if isinstance(validation_timings, dict):
        timings.update(_safe_timing_dict(validation_timings))
    return timings


def _safe_timing_dict(payload: dict[Any, Any]) -> dict[str, int]:
    return {
        str(key): max(0, int(value)) for key, value in payload.items() if isinstance(value, int | float) and str(key)
    }


def _safe_failure_payload(
    exc: Exception,
    *,
    stage: str,
    last_successful_stage: str,
    trace_id: str,
    retrieval: Any | None,
) -> dict[str, Any]:
    return {
        "error": "chat run failed",
        "stage": stage,
        "code": type(exc).__name__,
        "retryable": _retryable_error(exc),
        "attempt": 1,
        "last_successful_stage": last_successful_stage,
        "trace_id": trace_id,
        "safe_message": type(exc).__name__,
        "retrieval": _safe_retrieval_snapshot(retrieval),
    }


def _retryable_error(exc: Exception) -> bool:
    return type(exc).__name__ not in {"ValueError", "ValidationError", "KnowledgeBaseNotReady"}


def _safe_retrieval_snapshot(retrieval: Any | None) -> dict[str, Any]:
    if retrieval is None:
        return {}
    payload = retrieval.model_dump() if hasattr(retrieval, "model_dump") else {}
    evidence = []
    for item in payload.get("evidence", []):
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "evidence_id": item.get("evidence_id"),
                "chunk_id": item.get("chunk_id"),
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                "scores": item.get("scores", {}),
            }
        )
    events = []
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        safe_event = {
            "stage": event.get("stage"),
            "count": event.get("count"),
            "latency_ms": event.get("latency_ms"),
            "stage_latency_ms": event.get("stage_latency_ms"),
            "top": event.get("top", []),
            "run_contract_id": event.get("run_contract_id"),
            "index_contract_id": event.get("index_contract_id"),
        }
        candidates = []
        for candidate in list(event.get("candidates") or [])[:20]:
            if isinstance(candidate, dict):
                candidates.append(
                    {
                        "chunk_id": candidate.get("chunk_id"),
                        "title": candidate.get("title"),
                        "source_url": candidate.get("source_url"),
                        "scores": candidate.get("scores", {}),
                        "ranks": candidate.get("ranks", {}),
                    }
                )
        if candidates:
            safe_event["candidates"] = candidates
        if event.get("decision"):
            safe_event["decision"] = event.get("decision")
        events.append(safe_event)
    return {
        "trace_id": payload.get("trace_id", ""),
        "index_contract_id": payload.get("index_contract_id", ""),
        "run_contract_id": payload.get("run_contract_id", ""),
        "evidence": evidence,
        "events": events,
    }


def _kb_not_ready_http(exc: KnowledgeBaseNotReady, request_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "code": exc.code,
                "message": str(exc),
                "request_id": request_id,
                "details": exc.details,
            }
        },
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def main() -> None:
    uvicorn.run("wikipediarag.api_app:app", host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
