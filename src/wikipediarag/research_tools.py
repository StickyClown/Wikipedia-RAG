from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from httpx import HTTPStatusError, NetworkError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.answerability import decide_answerability, is_insufficient
from wikipediarag.config import Settings
from wikipediarag.extended import run_extended_search
from wikipediarag.ids import stable_hash
from wikipediarag.observability import ModelGatewayError, safe_error_code
from wikipediarag.repository import (
    fetch_document_context_chunks,
    get_document_public,
    list_document_sections,
    search_document_chunks,
)
from wikipediarag.research_tool_registry import (
    ALLOWED_RESEARCH_TOOLS,
    normalize_allowed_research_tools,
)
from wikipediarag.retrieval import retrieve_multi
from wikipediarag.retrieval_profile import RetrievalProfile
from wikipediarag.schemas import Evidence, RetrievalResult

UNSAFE_TOOL_METADATA_TOKENS = (
    "SECRET",
    "object_key",
    "original_artifact_key",
    "normalized_artifact_key",
    "server_side_tokens",
    "access_token",
    "refresh_token",
    "s3://",
    "raw_provider_payload",
)


ToolErrorClass = Literal["transient", "permanent", "security", "controller_bug"]


@dataclass(frozen=True, slots=True)
class ToolResult:
    retrieval: RetrievalResult | None
    result_summary: dict[str, Any] = field(default_factory=dict)
    error_class: ToolErrorClass | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.retrieval is not None and self.error_class is None

    @property
    def status(self) -> str:
        if self.succeeded:
            return "succeeded"
        if self.error_class in {"transient", "controller_bug"}:
            return "transient_failure"
        return "permanent_failure"


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """Controller-owned, validated request passed to a research tool."""

    tool_name: str
    query: str
    args: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 90.0
    idempotency_key: str = ""
    # Section/metadata tools intentionally have an empty wire query. Keep the
    # immutable research question separately for evidence sufficiency checks.
    evaluation_query: str = ""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Retrieval-layer hit before it is promoted to durable evidence."""

    chunk_id: str
    document_id: str | None = None
    document_version_id: str | None = None
    knowledge_base_id: str = ""
    title: str = ""
    snippet: str = ""
    source_url: str = ""
    section_path: tuple[str, ...] = ()
    score: float | None = None
    ranks: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """Stable document identity used when a hit is expanded into evidence."""

    document_id: str
    document_version_id: str | None = None
    knowledge_base_id: str = ""
    title: str = ""
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """Evidence record with a stable identity independent of its display ref."""

    evidence_id: str
    fingerprint: str
    chunk_id: str
    document_id: str | None = None
    document_version_id: str | None = None
    knowledge_base_id: str = ""
    title: str = ""
    content_abstract: str = ""
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class VerifiedClaim:
    """Claim after evidence verification, kept separate from raw retrieval."""

    claim_id: str
    text: str
    support_status: str
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""


class ResearchTool(Protocol):
    name: str

    async def execute(self, **kwargs: Any) -> ToolResult: ...


# Compatibility name for callers that still use the pre-controller contract.
ResearchToolExecutionResult = ToolResult


class ExtendedSearchResearchTool:
    """ResearchTool adapter for the existing extended_search implementation."""

    name = "extended_search"

    async def execute(self, **kwargs: Any) -> ToolResult:
        arguments = dict(kwargs)
        arguments.pop("tool_name", None)
        request = arguments.pop(
            "request",
            ToolRequest(tool_name=self.name, query=str(arguments.pop("tool_query", ""))),
        )
        return await execute_research_tool(request=request, **arguments)


def classify_tool_error(exc: Exception) -> ToolErrorClass:
    metadata = getattr(exc, "metadata", {}) if isinstance(getattr(exc, "metadata", {}), dict) else {}
    code = str(metadata.get("safe_error_code") or safe_error_code(exc))
    cause = getattr(exc, "__cause__", None)
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(
        getattr(cause, "response", None), "status_code", None
    )
    transient_codes = {
        "provider_timeout",
        "provider_network",
        "provider_rate_limit",
        "provider_5xx",
        "timeout",
        "network_error",
        "http_408",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "provider_network_error",
        "provider_http_408",
        "provider_http_429",
        "provider_http_500",
        "provider_http_502",
        "provider_http_503",
        "provider_http_504",
    }
    if isinstance(exc, (TimeoutError, ConnectionError, NetworkError)) or isinstance(
        cause, (TimeoutError, ConnectionError, NetworkError)
    ):
        return "transient"
    if isinstance(exc, (ModelGatewayError, HTTPStatusError)) and (
        code in transient_codes or status in {408, 429, 500, 502, 503, 504}
    ):
        return "transient"
    if status in {401, 403} or code in {"http_401", "http_403", "security"}:
        return "security"
    if isinstance(exc, PermissionError):
        return "security"
    if isinstance(exc, (ValueError, LookupError)):
        return "permanent"
    return "controller_bug"


async def execute_research_tool(
    conn: AsyncConnection,
    *,
    request: ToolRequest,
    allowed_tools: list[str] | tuple[str, ...] | set[str] | None = None,
    visible_evidence_records: list[dict[str, Any]] | None = None,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_ids: list[str] | None = None,
    query_run_id: str,
    trace_id: str,
    settings: Settings,
    profile: RetrievalProfile,
    profile_overrides: dict[str, Any],
    search_filters: dict[str, Any],
    max_rewrites: int = 2,
) -> ToolResult:
    started = time.perf_counter()
    if not isinstance(request, ToolRequest):
        raise TypeError("research tool execution requires a validated ToolRequest")
    tool_name = request.tool_name
    tool_query = request.query
    tool_args = request.args
    if request.timeout_seconds <= 0:
        raise ValueError("research tool timeout must be positive")
    allowed_tool_names = normalize_allowed_research_tools(allowed_tools)
    if tool_name not in allowed_tool_names:
        raise ValueError(f"research tool is not allowed: {tool_name}")
    args = dict(tool_args or {})
    allowed_arg_keys = {
        "extended_search": set(),
        "document_section_lookup": {"source_evidence_id", "section_title"},
        "search_within_document": {"source_evidence_id"},
        "table_csv_lookup": {"source_evidence_id"},
        "metadata_lookup": {"source_evidence_id"},
    }.get(tool_name)
    if allowed_arg_keys is None:
        raise ValueError(f"research tool is not registered: {tool_name}")
    unknown_arg_keys = sorted(set(args) - allowed_arg_keys)
    if unknown_arg_keys:
        raise ValueError(f"research tool arguments are not allowed: {unknown_arg_keys}")
    if tool_name == "extended_search" and args:
        raise ValueError("extended_search does not accept arguments")
    if tool_name != "extended_search" and not args.get("source_evidence_id"):
        raise ValueError("document tools require source_evidence_id")
    if tool_name in {"search_within_document", "table_csv_lookup"} and not tool_query:
        raise ValueError(f"{tool_name} requires a query")
    if tool_name in {"document_section_lookup", "metadata_lookup"} and tool_query:
        raise ValueError(f"{tool_name} does not accept a query")
    scope_ids = list(dict.fromkeys(str(item) for item in (knowledge_base_ids or [knowledge_base_id]) if str(item)))
    if knowledge_base_id not in scope_ids:
        scope_ids.insert(0, knowledge_base_id)
    try:
        if tool_name == "extended_search":
            retrieval = await _execute_broad_research_search(
                conn,
                tool_query=tool_query,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_ids=scope_ids,
                query_run_id=query_run_id,
                trace_id=trace_id,
                settings=settings,
                profile=profile,
                profile_overrides=profile_overrides,
                search_filters=search_filters,
                max_rewrites=max_rewrites,
            )
        else:
            retrieval = await _execute_document_tool(
                conn,
                tool_name=tool_name,
                tool_query=tool_query,
                evaluation_query=request.evaluation_query,
                tool_args=args,
                visible_evidence_records=visible_evidence_records or [],
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_ids=scope_ids,
                trace_id=trace_id,
                search_filters=search_filters,
                profile=profile,
            )
    except DBAPIError:
        # Database defects are controller/persistence failures, not an
        # ordinary tool branch result.  Re-raise so the controller keeps the
        # run visibly failed and records the safe stage diagnostics.
        raise
    except Exception as exc:
        error_class = classify_tool_error(exc)
        return ToolResult(
            retrieval=None,
            result_summary=assert_safe_tool_metadata(
                {
                    "version": "research_tool_error_v1",
                    "tool_name": tool_name,
                    "error_class": error_class,
                    "error_code": safe_error_code(exc),
                    "latency_ms": _elapsed_ms(started),
                }
            ),
            error_class=error_class,
            error_code=safe_error_code(exc),
            error_message="research tool branch failed",
        )
    summary = research_tool_result_summary(retrieval)
    summary["latency_ms"] = _elapsed_ms(started)
    return ToolResult(retrieval=retrieval, result_summary=summary)


def research_tool_call_metadata(
    *,
    tool_name: str,
    tool_query: str,
    tool_args: dict[str, Any] | None = None,
    planner: dict[str, Any],
    context: dict[str, Any],
    routing_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if tool_name not in ALLOWED_RESEARCH_TOOLS:
        raise ValueError(f"research tool is not allowed: {tool_name}")
    query_hash = stable_hash(["research_tool_query", _normalize_query(tool_query)], 32)
    args = dict(tool_args or {})
    metadata = {
        "version": "research_tool_call_metadata_v1",
        "tool_name": tool_name,
        "tool_query_hash": query_hash,
        "tool_query_length": len(tool_query),
        "tool_args_hash": stable_hash(["research_tool_args", _safe_args(args)], 32),
        "tool_arg_keys": sorted(args),
        "planner": {
            "tool_candidate_count": len(planner.get("tool_candidates") or []),
            "discovered_question_count": int(planner.get("discovered_question_count") or 0),
        },
        "context": {
            "token_estimate": context.get("token_estimate"),
            "over_soft_limit": context.get("over_soft_limit"),
            "over_hard_input_limit": context.get("over_hard_input_limit"),
            "trimming": list(context.get("trimming") or []),
        },
        "source_routing": {
            key: dict(routing_metadata or {})[key]
            for key in ("policy_version", "reason", "candidate_count", "source_evidence_ref")
            if key in dict(routing_metadata or {})
        },
    }
    return assert_safe_tool_metadata(metadata)


def research_tool_result_summary(
    retrieval: RetrievalResult,
    *,
    evidence_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    answerability = retrieval.answerability.model_dump(mode="json") if retrieval.answerability else {}
    stop_reason = ""
    timings_ms: dict[str, int] = {}
    event_count_by_stage: dict[str, int] = {}
    for event in retrieval.events:
        stage = str(event.get("stage") or "")
        if stage:
            event_count_by_stage[stage] = event_count_by_stage.get(stage, 0) + 1
        if stage == "harness":
            stop_reason = str(event.get("stop_reason") or "")
            raw_timings = event.get("timings_ms")
            if isinstance(raw_timings, dict):
                timings_ms.update(
                    {str(key): int(value) for key, value in raw_timings.items() if isinstance(value, int | float)}
                )
    return assert_safe_tool_metadata(
        {
            "version": "research_tool_result_summary_v1",
            "query_hash": stable_hash(["research_tool_query", _normalize_query(retrieval.query)], 32),
            "evidence_count": len(retrieval.evidence),
            "evidence_record_ids": list(evidence_record_ids or []),
            "evidence_refs": [item.evidence_id for item in retrieval.evidence],
            "answerability": {
                "status": answerability.get("status"),
                "confidence": answerability.get("confidence"),
                "reason": answerability.get("reason"),
                "reason_codes": list(answerability.get("reason_codes") or []),
            },
            "insufficient_evidence": retrieval.insufficient_evidence,
            "index_contract_id": retrieval.index_contract_id,
            "run_contract_id": retrieval.run_contract_id,
            "stop_reason": stop_reason,
            "event_count_by_stage": event_count_by_stage,
            "timings_ms": timings_ms,
        }
    )


def assert_safe_tool_metadata(value: dict[str, Any]) -> dict[str, Any]:
    serialized = str(value)
    leaked = [token for token in UNSAFE_TOOL_METADATA_TOKENS if token in serialized]
    if leaked:
        raise ValueError(f"unsafe research tool metadata tokens: {leaked}")
    return value


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip().casefold()


async def _execute_document_tool(
    conn: AsyncConnection,
    *,
    tool_name: str,
    tool_query: str,
    evaluation_query: str,
    tool_args: dict[str, Any],
    visible_evidence_records: list[dict[str, Any]],
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_ids: list[str],
    trace_id: str,
    search_filters: dict[str, Any],
    profile: RetrievalProfile,
) -> RetrievalResult:
    source_id = str(tool_args.get("source_evidence_id") or "")
    source = next((row for row in visible_evidence_records if str(row.get("id")) == source_id), None)
    if source is None:
        raise PermissionError("research document tool requires a visible source evidence handle")
    source_kb_id = str(source.get("knowledge_base_id") or knowledge_base_id)
    if source_kb_id not in knowledge_base_ids:
        raise PermissionError("research source evidence handle is outside the current scope")
    document_id = str(source.get("document_id") or "")
    document_version_id = str(source.get("document_version_id") or "") or None
    source_metadata = dict(source.get("metadata") or {})
    document_metadata = {
        key: value
        for key, value in dict(source_metadata.get("document_metadata") or {}).items()
        if key not in {"document_access", "document_access_origin"}
    }
    if not document_id:
        raise ValueError("source evidence does not contain a document handle")

    rows: list[dict[str, Any]] = []
    if tool_name == "document_section_lookup":
        sections = await list_document_sections(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=source_kb_id,
            document_id=document_id,
            document_version_id=document_version_id,
        )
        requested_title = str(tool_args.get("section_title") or "").casefold()
        selected = next(
            (
                item
                for item in sections
                if requested_title and str(item.get("title") or "").casefold() == requested_title
            ),
            None,
        )
        section_path = list(selected.get("path") or []) if selected else list(source.get("section_path") or [])
        rows = await fetch_document_context_chunks(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=source_kb_id,
            document_id=document_id,
            document_version_id=document_version_id,
            section_path=section_path or None,
            limit=12,
        )
    elif tool_name == "search_within_document":
        rows = await search_document_chunks(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=source_kb_id,
            document_id=document_id,
            document_version_id=document_version_id,
            query=tool_query[:500],
            limit=12,
            offset=0,
        )
    elif tool_name == "table_csv_lookup":
        candidates = await search_document_chunks(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=source_kb_id,
            document_id=document_id,
            document_version_id=document_version_id,
            query=tool_query[:500],
            limit=8,
            offset=0,
        )
        rows = [_csv_matching_row(row, tool_query) for row in candidates]
    elif tool_name == "metadata_lookup":
        document = await get_document_public(conn, tenant_id, document_id)
        if document is not None:
            safe = _safe_document_metadata(document)
            rows = [
                {
                    "chunk_id": str(source.get("chunk_id") or ""),
                    "document_id": document_id,
                    "document_version_id": document_version_id,
                    "knowledge_base_id": source_kb_id,
                    "title": str(document.get("title") or source.get("title") or "Document metadata"),
                    "section_path": list(source.get("section_path") or []),
                    "content": json.dumps(safe, ensure_ascii=False, sort_keys=True),
                    "source_url": str(document.get("source_url") or source.get("source_url") or ""),
                    "ranks": {"metadata_lookup": 1},
                }
            ]
    evidence = [_evidence_from_document_row(row, index=index) for index, row in enumerate(rows, start=1)]
    for item in evidence:
        item.metadata["document_metadata"] = document_metadata
    answerability = decide_answerability(evaluation_query or tool_query, evidence, profile)
    return RetrievalResult(
        query=tool_query or tool_name,
        trace_id=trace_id,
        evidence=evidence,
        events=[{"stage": "research_tool", "tool_name": tool_name, "evidence_count": len(evidence)}],
        insufficient_evidence=is_insufficient(answerability),
        answerability=answerability,
    )


def _evidence_from_document_row(row: dict[str, Any], *, index: int) -> Evidence:
    return Evidence(
        evidence_id=f"D{index}",
        chunk_id=str(row.get("chunk_id") or ""),
        knowledge_base_id=str(row.get("knowledge_base_id") or ""),
        title=str(row.get("title") or "Document evidence"),
        section_path=[str(item) for item in row.get("section_path") or []],
        content=str(row.get("content") or "")[:12000],
        source_url=str(row.get("source_url") or ""),
        scores={"document_tool": float(row.get("score") or 1.0)},
        ranks={str(key): int(value) for key, value in dict(row.get("ranks") or {}).items()},
        metadata={
            "document_id": row.get("document_id"),
            "document_version_id": row.get("document_version_id"),
            "document_metadata": {},
        },
    )


def _csv_matching_row(row: dict[str, Any], query: str) -> dict[str, Any]:
    content = str(row.get("content") or "")
    lines = content.splitlines()
    lowered = query.casefold()
    matches = [line for line in lines if lowered in line.casefold()]
    selected = matches[:8] or lines[:8]
    return {**row, "content": "\n".join(selected)}


def _safe_document_metadata(document: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "title",
        "source_type",
        "source_kind",
        "filename",
        "parser_route",
        "parser_name",
        "parser_version",
        "content_hash",
        "uploaded_at",
        "ingested_at",
        "published_at",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        value = document.get(key)
        if value is not None:
            result[key] = str(value)
    return result


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in sorted(args.items()) if str(key) not in {"content", "raw"}}


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


async def _execute_broad_research_search(
    conn: AsyncConnection,
    *,
    tool_query: str,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_ids: list[str],
    query_run_id: str,
    trace_id: str,
    settings: Settings,
    profile: RetrievalProfile,
    profile_overrides: dict[str, Any],
    search_filters: dict[str, Any],
    max_rewrites: int = 2,
) -> RetrievalResult:
    if len(knowledge_base_ids) > 1 and not isinstance(settings, Settings):
        return await retrieve_multi(
            conn,
            tool_query,
            tenant_id=tenant_id,
            knowledge_base_ids=knowledge_base_ids,
            query_run_id=query_run_id,
            trace_id=trace_id,
            settings=settings,
            top_k=profile.retrieval.top_k,
            profile=profile,
            profile_overrides=profile_overrides,
            search_filters=search_filters,
        )
    primary = await run_extended_search(
        conn,
        tool_query,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        knowledge_base_ids=knowledge_base_ids,
        query_run_id=query_run_id,
        trace_id=trace_id,
        settings=settings,
        profile=profile,
        profile_overrides=profile_overrides,
        search_filters=search_filters,
    )
    # ``run_extended_search`` already performs bounded decomposition and query
    # reformulation.  Running another rewrite loop here doubled model/retrieval
    # work and made a single tool branch look like several hidden branches.
    # Further formulations are selected by the controller on the next attempt.
    return primary


def _bounded_rewrite_queries(query: str, *, max_rewrites: int) -> list[str]:
    if max_rewrites <= 0:
        return []
    normalized = " ".join(query.split())
    variants: list[str] = []
    identifiers = re.findall(r"\b[A-Z]{2,}[A-Z0-9]*-\d+\b", normalized)
    if identifiers:
        variants.append(" ".join(dict.fromkeys(identifiers)))
    tokens = [token for token in re.split(r"\s+", normalized) if token]
    stopwords = {
        "что",
        "какой",
        "какая",
        "какие",
        "как",
        "ли",
        "кто",
        "где",
        "when",
        "what",
        "which",
        "who",
        "how",
        "is",
        "the",
        "a",
        "an",
    }
    compact = [token for token in tokens if token.casefold().strip(".,?!:;()[]{}\"'") not in stopwords]
    if len(compact) >= 3:
        variants.append(" ".join(compact[: min(6, len(compact))]))
    deduped: list[str] = []
    seen: set[str] = {normalized.casefold()}
    for variant in variants:
        cleaned = " ".join(variant.split())
        if cleaned and cleaned.casefold() not in seen:
            deduped.append(cleaned[:500])
            seen.add(cleaned.casefold())
        if len(deduped) >= max_rewrites:
            break
    return deduped


def _merge_research_retrievals(
    query: str,
    results: list[RetrievalResult],
    *,
    rewrites: list[str],
    profile: RetrievalProfile,
) -> RetrievalResult:
    evidence_by_chunk: dict[str, Evidence] = {}
    events: list[dict[str, Any]] = []
    for result in results:
        events.extend(result.events)
        for evidence in result.evidence:
            existing = evidence_by_chunk.get(evidence.chunk_id)
            if existing is None or _evidence_best_score(evidence) > _evidence_best_score(existing):
                evidence_by_chunk[evidence.chunk_id] = evidence
    merged_evidence = list(evidence_by_chunk.values())
    answerability = decide_answerability(query, merged_evidence, profile)
    events.append(
        {
            "stage": "research_rewrite",
            "stable_stage": "query_transform",
            "tool": "extended_search",
            "original_query_hash": stable_hash(["research_tool_query", _normalize_query(query)], 32),
            "rewrite_query_hashes": [
                stable_hash(["research_tool_query", _normalize_query(item)], 32) for item in rewrites
            ],
            "rewrite_count": len(rewrites),
        }
    )
    primary = results[0]
    return RetrievalResult(
        query=query,
        trace_id=primary.trace_id,
        evidence=merged_evidence,
        events=events,
        insufficient_evidence=is_insufficient(answerability),
        answerability=answerability,
        index_contract_id=primary.index_contract_id,
        run_contract_id=primary.run_contract_id,
    )


def _evidence_best_score(evidence: Evidence) -> float:
    values = [float(value) for value in evidence.scores.values() if isinstance(value, int | float)]
    return max(values) if values else 0.0
