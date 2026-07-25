from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from wikipediarag.answering import generate_answer
from wikipediarag.config import get_settings
from wikipediarag.db import connect, ensure_schema, json_dumps
from wikipediarag.embedding import embed_text
from wikipediarag.extended import run_extended_search
from wikipediarag.ids import stable_hash
from wikipediarag.repository import (
    complete_query_run,
    create_ingestion_job,
    create_query_run,
    fail_query_run,
    list_knowledge_bases,
    load_retrieval_events,
    request_cancel,
    request_resume,
)
from wikipediarag.retrieval import retrieve
from wikipediarag.schemas import (
    ChatRequest,
    DebugSearchRequest,
    ImportRequest,
    KnowledgeBaseCreate,
    SseEvent,
    UploadResponse,
)
from wikipediarag.search_index import bulk_index_chunks
from wikipediarag.wiki_dump import Chunk

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
            response = await client.get(f"{settings.model_gateway_url.rstrip('/')}/health")
            components["model_gateway"] = "ok" if response.status_code == 200 else "failed"
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


@app.post("/api/v1/uploads")
async def upload_document(file: UploadFile = File(...)) -> UploadResponse:  # noqa: B008
    settings = get_settings()
    data = await file.read()
    if len(data) > 2_000_000:
        raise HTTPException(status_code=413, detail="file too large for development upload")
    try:
        text_content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="development upload supports UTF-8 text") from exc
    document_hash = stable_hash([file.filename, text_content], 24)
    document_id = f"upload:{document_hash}"
    title = file.filename or "uploaded document"
    words = text_content.split()
    chunks: list[Chunk] = []
    for index in range(0, max(len(words), 1), 220):
        body = " ".join(words[index : index + 220]) or text_content
        chunk_id = "upload:" + stable_hash([document_id, index, body], 32)
        chunks.append(
            Chunk(
                id=chunk_id,
                document_id=document_id,
                page_id=0,
                revision_id=0,
                title=title,
                section_path=(title,),
                content=body,
                parent_chunk_id=None,
                prev_chunk_id=None,
                next_chunk_id=None,
                source_uri=f"upload://{document_id}",
                source_url=f"upload://{document_id}",
                content_hash=stable_hash([body]),
                embedding=embed_text(body, settings.embedding_dimensions),
            )
        )
    async with connect() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO documents(id, tenant_id, knowledge_base_id, source_type, title, source_uri, metadata)
                VALUES (:id, :tenant_id, :kb_id, 'upload_text', :title, :source_uri,
                        CAST(:metadata AS jsonb))
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": document_id,
                "tenant_id": settings.default_tenant_id,
                "kb_id": settings.default_kb_id,
                "title": title,
                "source_uri": f"upload://{document_id}",
                "metadata": json_dumps({"filename": file.filename}),
            },
        )
        from wikipediarag.repository import upsert_chunk

        for chunk in chunks:
            await upsert_chunk(
                conn,
                tenant_id=settings.default_tenant_id,
                knowledge_base_id=settings.default_kb_id,
                chunk=chunk,
            )
    await asyncio.to_thread(
        bulk_index_chunks,
        chunks,
        tenant_id=settings.default_tenant_id,
        knowledge_base_id=settings.default_kb_id,
        settings=settings,
    )
    return UploadResponse(document_id=document_id, chunks_indexed=len(chunks))


@app.post("/api/v1/chat")
async def chat(payload: ChatRequest) -> StreamingResponse:
    settings = get_settings()
    request_id = str(uuid.uuid4())
    trace_id = stable_hash([request_id, payload.message], 32)
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
                kb_id = payload.knowledge_base_ids[0] if payload.knowledge_base_ids else settings.default_kb_id
                if payload.mode.value == "extended":
                    retrieval = await run_extended_search(
                        conn,
                        payload.message,
                        tenant_id=settings.default_tenant_id,
                        knowledge_base_id=kb_id,
                        query_run_id=str(query_run_id),
                        trace_id=trace_id,
                        settings=settings,
                    )
                else:
                    retrieval = await retrieve(
                        conn,
                        payload.message,
                        tenant_id=settings.default_tenant_id,
                        knowledge_base_id=kb_id,
                        query_run_id=str(query_run_id),
                        trace_id=trace_id,
                        settings=settings,
                    )
            answer, validation = await generate_answer(payload.message, retrieval)
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
                    data={"retrieval": retrieval.model_dump(), "citation_validation": validation},
                )
            )
            async with connect() as conn:
                await complete_query_run(
                    conn,
                    query_run_id=str(query_run_id),
                    answer=answer,
                    usage={"citations": validation.get("citations", [])},
                )
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
        except Exception as exc:
            async with connect() as conn:
                await fail_query_run(conn, query_run_id=str(query_run_id), error_code=type(exc).__name__)
            sequence += 1
            yield _event(
                SseEvent(
                    event="run.failed",
                    request_id=request_id,
                    query_run_id=str(query_run_id),
                    sequence=sequence,
                    data={"error": "chat run failed"},
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
    async with connect() as conn:
        result = await retrieve(
            conn,
            payload.message,
            tenant_id=settings.default_tenant_id,
            knowledge_base_id=settings.default_kb_id,
            query_run_id=None,
            trace_id=trace_id,
            settings=settings,
            top_k=payload.top_k,
        )
    return result.model_dump()


def _event(event: SseEvent) -> str:
    return _sse(event.event, event.model_dump(mode="json"))


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(_jsonable(data), ensure_ascii=False)}\n\n"


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
