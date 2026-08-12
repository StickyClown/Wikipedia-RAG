from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, date, datetime
from io import StringIO
from typing import Any, Literal

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from wikipediarag.config import Settings, get_settings
from wikipediarag.ids import stable_hash
from wikipediarag.reliability import safe_failure_from_exception
from wikipediarag.wiki_dump import Chunk

NORMALIZED_DOCUMENT_SCHEMA_VERSION = "normalized_document_v1"
DOCUMENT_CHUNKER_VERSION = "document_chunker_v1"
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm"}
STRUCTURED_EXTENSIONS = {".csv", ".tsv", ".json", ".jsonl"}
SERVICE_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | STRUCTURED_EXTENSIONS | SERVICE_EXTENSIONS
MACRO_EXTENSIONS = {".docm", ".xlsm", ".pptm"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
REMOTE_HTML_RE = re.compile(r"""(?:src|href)\s*=\s*["']\s*https?://""", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])\b")
RU_DATE_RE = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
DOT_DATE_RE = re.compile(r"\b(0?[1-9]|[12]\d|3[01])[.](0?[1-9]|1[0-2])[.]((?:19|20)\d{2})\b")
RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_PARSER_LIMITERS: dict[tuple[str, int], asyncio.Semaphore] = {}
_PARSER_ENDPOINT_LOCK = asyncio.Lock()
_PARSER_ENDPOINT_INDEX: dict[tuple[str, tuple[str, ...]], int] = {}


class UploadValidationError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class ParserServiceError(RuntimeError):
    def __init__(self, parser: str, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.parser = parser
        self.code = code
        self.safe_message = safe_message


def _parser_failure_code(exc: BaseException) -> str:
    """Map parser transport failures to the public reliability taxonomy."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = int(exc.response.status_code)
        if status in {429, 502, 503, 504}:
            return "DEPENDENCY_UNAVAILABLE"
        if 400 <= status < 500:
            return "PARSER_REJECTED"
    failure = safe_failure_from_exception(exc, stage="parsing")
    return failure.error_code


class FileValidation(BaseModel):
    filename: str
    extension: str
    supplied_content_type: str
    detected_mime: str
    signature: str
    size_bytes: int = Field(ge=0)
    checksum_sha256: str


class DocumentMetadata(BaseModel):
    detected_language: str = "und"
    language_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    language_alternatives: list[dict[str, float]] = Field(default_factory=list)
    document_date: str | None = None
    document_date_source: str | None = None
    document_date_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    document_date_candidates: list[dict[str, Any]] = Field(default_factory=list)


class NormalizedBlock(BaseModel):
    block_id: str
    kind: Literal["heading", "paragraph", "table", "list", "code", "metadata"] = "paragraph"
    text: str
    level: int | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedTable(BaseModel):
    table_id: str
    rows: list[list[str]]
    locator: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedDocument(BaseModel):
    schema_version: str = NORMALIZED_DOCUMENT_SCHEMA_VERSION
    title: str
    text: str
    blocks: list[NormalizedBlock]
    tables: list[NormalizedTable] = Field(default_factory=list)
    parser_name: str
    parser_version: str
    parser_route: str
    parser_options: dict[str, Any] = Field(default_factory=dict)
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_upload_filename(filename: str) -> str:
    return _clean_filename(filename)


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalized_document_hash(document: NormalizedDocument) -> str:
    payload = document.model_dump(mode="json", exclude={"metadata": {"document_date_candidates"}})
    return stable_hash([json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))], 64)


def validate_upload_bytes(
    data: bytes,
    *,
    filename: str,
    supplied_content_type: str,
    expected_size_bytes: int,
    expected_sha256: str,
    settings: Settings | None = None,
) -> FileValidation:
    resolved = settings or get_settings()
    clean_filename = _clean_filename(filename)
    extension = _extension(clean_filename)
    actual_size = len(data)
    if actual_size == 0:
        raise UploadValidationError("ZERO_BYTE", "file is empty")
    if actual_size > resolved.upload_max_bytes:
        raise UploadValidationError("OVERSIZED", "file exceeds configured upload size limit")
    if expected_size_bytes != actual_size:
        raise UploadValidationError("SIZE_MISMATCH", "uploaded object size does not match the session")
    actual_sha256 = sha256_hex(data)
    if expected_sha256.lower() != actual_sha256:
        raise UploadValidationError("CHECKSUM_MISMATCH", "uploaded object checksum does not match the session")
    if extension in ARCHIVE_EXTENSIONS:
        raise UploadValidationError("ARCHIVE_NOT_ALLOWED", "archives are not accepted")
    if extension in MACRO_EXTENSIONS:
        raise UploadValidationError("MACRO_OFFICE_NOT_ALLOWED", "macro-enabled Office files are not accepted")
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadValidationError("UNSUPPORTED_EXTENSION", "file extension is not supported")
    signature = _signature(data)
    detected_mime = _detected_mime(data, extension)
    _validate_signature(extension, data, signature)
    if extension == ".html" or extension == ".htm":
        html = _decode_utf8(data)
        if REMOTE_HTML_RE.search(html):
            raise UploadValidationError("REMOTE_HTML_RESOURCE", "html with remote resources is not accepted")
    if extension in {".json", ".jsonl"}:
        _validate_json_depth(data, resolved.upload_json_max_depth, json_lines=extension == ".jsonl")
    return FileValidation(
        filename=clean_filename,
        extension=extension,
        supplied_content_type=supplied_content_type,
        detected_mime=detected_mime,
        signature=signature,
        size_bytes=actual_size,
        checksum_sha256=actual_sha256,
    )


async def extract_metadata(
    text_sample: str,
    *,
    filename: str,
    settings: Settings | None = None,
) -> DocumentMetadata:
    resolved = settings or get_settings()
    if not text_sample.strip():
        return DocumentMetadata()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{resolved.metadata_service_url.rstrip('/')}/v1/metadata:extract",
                json={"text": text_sample[:20000], "filename": filename},
            )
            response.raise_for_status()
            return DocumentMetadata.model_validate(response.json())
    except httpx.HTTPStatusError as exc:
        raise ParserServiceError("xberg", _parser_failure_code(exc), "xberg parser request failed") from exc
    except Exception as exc:
        if resolved.document_parser_services_required:
            raise ParserServiceError("metadata-service", _parser_failure_code(exc), "metadata service failed") from exc
        return extract_metadata_local(text_sample)


def extract_metadata_local(text: str) -> DocumentMetadata:
    language, confidence, alternatives = detect_language(text)
    document_date, date_candidates = detect_document_date(text)
    return DocumentMetadata(
        detected_language=language,
        language_confidence=confidence,
        language_alternatives=alternatives,
        document_date=document_date,
        document_date_source="content" if document_date else None,
        document_date_confidence=0.8 if document_date else 0.0,
        document_date_candidates=date_candidates,
    )


def detect_language(text: str) -> tuple[str, float, list[dict[str, float]]]:
    sample = text[:20000]
    cyrillic = sum(1 for char in sample if "а" <= char.casefold() <= "я" or char in "ёЁ")
    latin = sum(1 for char in sample if "a" <= char.casefold() <= "z")
    total = cyrillic + latin
    if total == 0:
        return "und", 0.0, []
    ru_score = cyrillic / total
    en_score = latin / total
    if ru_score >= en_score:
        return "ru", min(0.99, max(0.5, ru_score)), [{"en": round(en_score, 3)}]
    return "en", min(0.99, max(0.5, en_score)), [{"ru": round(ru_score, 3)}]


def detect_document_date(text: str) -> tuple[str | None, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for match in ISO_DATE_RE.finditer(text[:50000]):
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            candidates.append({"value": parsed.isoformat(), "source": "content", "confidence": 0.8})
    for match in DOT_DATE_RE.finditer(text[:50000]):
        parsed = _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if parsed:
            candidates.append({"value": parsed.isoformat(), "source": "content", "confidence": 0.8})
    for match in RU_DATE_RE.finditer(text[:50000]):
        month = RU_MONTHS[match.group(2).casefold()]
        parsed = _safe_date(int(match.group(3)), month, int(match.group(1)))
        if parsed:
            candidates.append({"value": parsed.isoformat(), "source": "content", "confidence": 0.8})
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate["value"])
        if value not in seen:
            deduped.append(candidate)
            seen.add(value)
    return (str(deduped[0]["value"]) if deduped else None, deduped[:8])


async def normalize_uploaded_document(
    data: bytes,
    *,
    validation: FileValidation,
    parser_profile: str,
    settings: Settings | None = None,
) -> NormalizedDocument:
    resolved = settings or get_settings()
    if validation.extension in STRUCTURED_EXTENSIONS:
        return await _normalize_structured(
            data, validation=validation, parser_profile=parser_profile, settings=resolved
        )
    if validation.extension in TEXT_EXTENSIONS:
        return await _normalize_text(data, validation=validation, parser_profile=parser_profile, settings=resolved)
    xberg_error: ParserServiceError | None = None
    try:
        xberg = await _call_xberg(data, validation=validation, parser_profile=parser_profile, settings=resolved)
        fallback_reasons = quality_gate_fallback_reasons(xberg, validation=validation, parser_profile=parser_profile)
        if not fallback_reasons:
            return xberg
        xberg.warnings.extend(f"docling_fallback:{reason}" for reason in fallback_reasons)
    except ParserServiceError as exc:
        xberg_error = exc
    try:
        docling = await _call_docling(data, validation=validation, parser_profile=parser_profile, settings=resolved)
        if xberg_error:
            docling.warnings.append(f"xberg_error:{xberg_error.code}")
        return docling
    except ParserServiceError as docling_error:
        if xberg_error is not None:
            raise xberg_error from docling_error
        raise


def quality_gate_fallback_reasons(
    document: NormalizedDocument,
    *,
    validation: FileValidation,
    parser_profile: str,
) -> list[str]:
    reasons: list[str] = []
    if parser_profile == "high_quality":
        reasons.append("high_quality_profile")
    if not document.text.strip():
        reasons.append("empty_text")
    if _replacement_ratio(document.text) > 0.01:
        reasons.append("replacement_character_ratio")
    if validation.extension == ".pdf" and len(document.text.split()) < 30:
        reasons.append("scanned_or_low_text_pdf")
    lowered = " ".join([validation.filename, *document.warnings]).casefold()
    if "table" in lowered and not document.tables:
        reasons.append("expected_tables_missing")
    if any(marker in lowered for marker in ("formula", "reading_order", "layout_warning")):
        reasons.append("complex_layout_signal")
    return reasons


def chunks_for_normalized_document(
    document: NormalizedDocument,
    *,
    document_id: str,
    document_version_id: str,
    source_url: str,
    dimensions: int,
) -> list[Chunk]:
    words = document.text.split()
    if not words:
        return []
    normalized_hash = normalized_document_hash(document)
    block_contexts: list[tuple[int, int, dict[str, Any], list[str]]] = []
    consumed_words = 0
    fallback_section_path = [document.title]
    for block in document.blocks:
        metadata = dict(block.metadata or {})
        section_path = metadata.get("section_path")
        if isinstance(section_path, list) and section_path:
            fallback_section_path = [str(item) for item in section_path if str(item)]
        block_word_count = len(block.text.split())
        block_contexts.append(
            (
                consumed_words,
                consumed_words + max(block_word_count, 1),
                dict(block.locator),
                list(fallback_section_path),
            )
        )
        consumed_words += block_word_count
    chunks: list[Chunk] = []
    context_index = 0
    for ordinal, start in enumerate(range(0, len(words), 220), start=1):
        body = " ".join(words[start : start + 220])
        while context_index < len(block_contexts):
            block_start, block_end, _locator, _section_path = block_contexts[context_index]
            if block_start <= start < block_end:
                break
            context_index += 1
        if context_index < len(block_contexts):
            _block_start, _block_end, locator, section_path = block_contexts[context_index]
        else:
            locator = {"page": 1}
            section_path = list(fallback_section_path)
        section_id = _section_id(document_version_id, section_path)
        content_hash = stable_hash([body], 64)
        chunk_id = "doc:" + stable_hash(
            [document_version_id, DOCUMENT_CHUNKER_VERSION, ordinal, stable_hash([body], 32)],
            32,
        )
        metadata = {
            "source_type": "upload_document",
            "document_version_id": document_version_id,
            "chunk_ordinal": ordinal,
            "locator": locator,
            "content_hash": content_hash,
            "normalized_hash": normalized_hash,
            "parser_route": document.parser_route,
            "parser_name": document.parser_name,
            "parser_version": document.parser_version,
            "language": document.metadata.detected_language,
            "document_date": document.metadata.document_date,
            "chunker_version": DOCUMENT_CHUNKER_VERSION,
            "section_id": section_id,
        }
        chunks.append(
            Chunk(
                id=chunk_id,
                document_id=document_id,
                page_id=int(locator.get("page") or ordinal),
                revision_id=0,
                title=document.title,
                section_path=tuple(section_path),
                content=body,
                parent_chunk_id=section_id,
                prev_chunk_id=None,
                next_chunk_id=None,
                source_uri=f"document://{document_version_id}#{ordinal}",
                source_url=source_url,
                content_hash=content_hash,
                embedding=[0.0 for _ in range(dimensions)],
                metadata=metadata,
            )
        )
    return _link_neighbors(chunks)


def safe_public_metadata(
    *,
    validation: FileValidation,
    metadata: DocumentMetadata,
    normalized_hash: str,
    parser_route: str,
    parser_name: str,
    parser_version: str,
    warnings: Iterable[str],
) -> dict[str, Any]:
    return {
        "filename": validation.filename,
        "content_type": validation.supplied_content_type,
        "detected_mime": validation.detected_mime,
        "size_bytes": validation.size_bytes,
        "checksum_sha256": validation.checksum_sha256,
        "normalized_hash": normalized_hash,
        "parser_route": parser_route,
        "parser_name": parser_name,
        "parser_version": parser_version,
        "detected_language": metadata.detected_language,
        "language_confidence": metadata.language_confidence,
        "language_alternatives": metadata.language_alternatives,
        "document_date": metadata.document_date,
        "document_date_source": metadata.document_date_source,
        "document_date_confidence": metadata.document_date_confidence,
        "warnings": [_safe_parser_warning(warning) for warning in warnings][:20],
    }


async def _normalize_text(
    data: bytes,
    *,
    validation: FileValidation,
    parser_profile: str,
    settings: Settings,
) -> NormalizedDocument:
    text = _decode_utf8(data)
    metadata = await extract_metadata(text, filename=validation.filename, settings=settings)
    title = _title_from_filename(validation.filename)
    return _document_from_text(
        text,
        title=title,
        metadata=metadata,
        parser_name="local_text_adapter",
        parser_version=NORMALIZED_DOCUMENT_SCHEMA_VERSION,
        parser_route="local_text_adapter",
        parser_options={"profile": parser_profile},
        source_metadata={"filename": validation.filename, "detected_mime": validation.detected_mime},
        warnings=[],
    )


async def _normalize_structured(
    data: bytes,
    *,
    validation: FileValidation,
    parser_profile: str,
    settings: Settings,
) -> NormalizedDocument:
    text = _decode_utf8(data)
    rows: list[list[str]] = []
    if validation.extension in {".csv", ".tsv"}:
        dialect = "excel-tab" if validation.extension == ".tsv" else "excel"
        rows = [[cell.strip() for cell in row] for row in csv.reader(StringIO(text), dialect=dialect)]
        normalized_text = "\n".join(" | ".join(row) for row in rows)
    elif validation.extension == ".jsonl":
        rows = [[line] for line in text.splitlines() if line.strip()]
        normalized_text = "\n".join(row[0] for row in rows)
    else:
        payload = json.loads(text)
        normalized_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    metadata = await extract_metadata(normalized_text, filename=validation.filename, settings=settings)
    document = _document_from_text(
        normalized_text,
        title=_title_from_filename(validation.filename),
        metadata=metadata,
        parser_name="local_structured_adapter",
        parser_version=NORMALIZED_DOCUMENT_SCHEMA_VERSION,
        parser_route=f"local_{validation.extension.removeprefix('.')}_adapter",
        parser_options={"profile": parser_profile},
        source_metadata={"filename": validation.filename, "detected_mime": validation.detected_mime},
        warnings=[],
    )
    if rows:
        document.tables.append(
            NormalizedTable(
                table_id="table:1",
                rows=rows,
                locator={"sheet": "default", "row_start": 1, "row_end": len(rows)},
                metadata={"source": validation.extension.removeprefix(".")},
            )
        )
    return document


def _parser_limiter(parser: Literal["xberg", "docling"], settings: Settings) -> asyncio.Semaphore:
    limit = (
        settings.document_parser_xberg_concurrency
        if parser == "xberg"
        else settings.document_parser_docling_concurrency
    )
    normalized_limit = max(1, int(limit))
    key = (parser, normalized_limit)
    limiter = _PARSER_LIMITERS.get(key)
    if limiter is None:
        limiter = asyncio.Semaphore(normalized_limit)
        _PARSER_LIMITERS[key] = limiter
    return limiter


async def _next_parser_endpoint(parser: Literal["xberg", "docling"], settings: Settings) -> tuple[str, int, int]:
    endpoints = _parser_endpoints(parser, settings)
    key = (parser, tuple(endpoints))
    async with _PARSER_ENDPOINT_LOCK:
        index = _PARSER_ENDPOINT_INDEX.get(key, 0)
        _PARSER_ENDPOINT_INDEX[key] = (index + 1) % len(endpoints)
    return endpoints[index], index, len(endpoints)


def _parser_endpoints(parser: Literal["xberg", "docling"], settings: Settings) -> list[str]:
    raw = settings.xberg_urls if parser == "xberg" else settings.docling_urls
    fallback = settings.xberg_url if parser == "xberg" else settings.docling_url
    endpoints = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    if not endpoints:
        endpoints = [fallback.rstrip("/")]
    return endpoints


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


async def _call_xberg(
    data: bytes,
    *,
    validation: FileValidation,
    parser_profile: str,
    settings: Settings,
) -> NormalizedDocument:
    queue_started = time.perf_counter()
    try:
        async with _parser_limiter("xberg", settings):
            queue_wait_ms = _elapsed_ms(queue_started)
            endpoint, endpoint_index, endpoint_pool_size = await _next_parser_endpoint("xberg", settings)
            url = f"{endpoint}/extract"
            parser_started = time.perf_counter()
            async with httpx.AsyncClient(timeout=settings.document_parser_timeout_seconds) as client:
                response = await client.post(
                    url,
                    files={"files": (validation.filename, data, validation.detected_mime)},
                )
                response.raise_for_status()
            payload = await asyncio.to_thread(response.json)
            parser_latency_ms = _elapsed_ms(parser_started)
    except httpx.HTTPStatusError as exc:
        raise ParserServiceError("docling", _parser_failure_code(exc), "docling parser request failed") from exc
    except Exception as exc:
        raise ParserServiceError("xberg", _parser_failure_code(exc), "xberg parser request failed") from exc
    return await _document_from_parser_payload(
        payload,
        validation=validation,
        parser_name="xberg",
        parser_version="1.0.3",
        parser_route="xberg",
        parser_profile=parser_profile,
        settings=settings,
        parser_runtime={
            "endpoint_index": endpoint_index,
            "endpoint_pool_size": endpoint_pool_size,
            "queue_wait_ms": queue_wait_ms,
            "parser_latency_ms": parser_latency_ms,
        },
    )


async def _call_docling(
    data: bytes,
    *,
    validation: FileValidation,
    parser_profile: str,
    settings: Settings,
) -> NormalizedDocument:
    queue_started = time.perf_counter()
    try:
        async with _parser_limiter("docling", settings):
            queue_wait_ms = _elapsed_ms(queue_started)
            endpoint, endpoint_index, endpoint_pool_size = await _next_parser_endpoint("docling", settings)
            url = f"{endpoint}/v1/convert/file"
            parser_started = time.perf_counter()
            async with httpx.AsyncClient(timeout=settings.document_parser_timeout_seconds) as client:
                response = await client.post(
                    url,
                    data={"to_formats": "md", "target_type": "inbody", "do_ocr": "true", "table_mode": "accurate"},
                    files={"files": (validation.filename, data, validation.detected_mime)},
                )
                response.raise_for_status()
            payload = await asyncio.to_thread(response.json)
            parser_latency_ms = _elapsed_ms(parser_started)
    except Exception as exc:
        raise ParserServiceError("docling", _parser_failure_code(exc), "docling parser request failed") from exc
    return await _document_from_parser_payload(
        payload,
        validation=validation,
        parser_name="docling",
        parser_version="serve-cpu:v1.28.0",
        parser_route="docling",
        parser_profile=parser_profile,
        settings=settings,
        parser_runtime={
            "endpoint_index": endpoint_index,
            "endpoint_pool_size": endpoint_pool_size,
            "queue_wait_ms": queue_wait_ms,
            "parser_latency_ms": parser_latency_ms,
        },
    )


async def _document_from_parser_payload(
    payload: Any,
    *,
    validation: FileValidation,
    parser_name: str,
    parser_version: str,
    parser_route: str,
    parser_profile: str,
    settings: Settings,
    parser_runtime: dict[str, int] | None = None,
) -> NormalizedDocument:
    text = await asyncio.to_thread(_extract_text_payload, payload)
    metadata = await extract_metadata(text, filename=validation.filename, settings=settings)
    title, warnings, tables = await asyncio.gather(
        asyncio.to_thread(_extract_title, payload),
        asyncio.to_thread(_extract_warnings, payload),
        asyncio.to_thread(_extract_tables, payload),
    )
    document = await asyncio.to_thread(
        _document_from_text,
        text,
        title=title or _title_from_filename(validation.filename),
        metadata=metadata,
        parser_name=parser_name,
        parser_version=parser_version,
        parser_route=parser_route,
        parser_options={"profile": parser_profile, **(parser_runtime or {})},
        source_metadata={"filename": validation.filename, "detected_mime": validation.detected_mime},
        warnings=warnings,
    )
    document.tables.extend(tables)
    return document


def _document_from_text(
    text: str,
    *,
    title: str,
    metadata: DocumentMetadata,
    parser_name: str,
    parser_version: str,
    parser_route: str,
    parser_options: dict[str, Any],
    source_metadata: dict[str, Any],
    warnings: list[str],
) -> NormalizedDocument:
    blocks = _blocks_from_text(text, title=title)
    return NormalizedDocument(
        title=title,
        text="\n\n".join(block.text for block in blocks),
        blocks=blocks,
        parser_name=parser_name,
        parser_version=parser_version,
        parser_route=parser_route,
        parser_options=parser_options,
        metadata=metadata,
        source_metadata=source_metadata,
        warnings=warnings,
    )


def _blocks_from_text(text: str, *, title: str) -> list[NormalizedBlock]:
    stripped = text.strip()
    if not stripped:
        return []
    if _looks_like_html(stripped):
        blocks = _blocks_from_html(stripped, title=title)
        if blocks:
            return blocks
    return _blocks_from_markdown_like(stripped, title=title)


def _blocks_from_html(text: str, *, title: str) -> list[NormalizedBlock]:
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    body = soup.body or soup
    blocks: list[NormalizedBlock] = []
    state = _SectionState(title)
    for tag in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th"], recursive=True):
        value = " ".join(tag.get_text(" ", strip=True).split())
        if not value:
            continue
        if tag.name and tag.name.startswith("h"):
            level = max(1, min(int(tag.name[1]), 6))
            path = state.enter_heading(value, level)
            kind: Literal["heading", "paragraph"] = "heading"
        else:
            level = None
            path = state.current_path()
            kind = "paragraph"
        blocks.append(_normalized_block(len(blocks) + 1, value, kind=kind, level=level, section_path=path))
    return blocks


def _blocks_from_markdown_like(text: str, *, title: str) -> list[NormalizedBlock]:
    blocks: list[NormalizedBlock] = []
    state = _SectionState(title)
    buffer: list[str] = []

    def flush_paragraph() -> None:
        if not buffer:
            return
        value = " ".join(" ".join(buffer).split())
        buffer.clear()
        if value:
            blocks.append(
                _normalized_block(
                    len(blocks) + 1,
                    value,
                    kind="paragraph",
                    level=None,
                    section_path=state.current_path(),
                )
            )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = _markdown_heading(line) or _inline_html_heading(line)
        if heading is not None:
            flush_paragraph()
            level, value = heading
            path = state.enter_heading(value, level)
            blocks.append(
                _normalized_block(
                    len(blocks) + 1,
                    value,
                    kind="heading",
                    level=level,
                    section_path=path,
                )
            )
            continue
        buffer.append(line)
    flush_paragraph()
    if blocks:
        return blocks
    return [_normalized_block(1, text, kind="paragraph", level=None, section_path=[title])]


class _SectionState:
    def __init__(self, title: str) -> None:
        self.title = title.strip() or "Document"
        self.stack: list[str] = []

    def current_path(self) -> list[str]:
        return [self.title, *self.stack]

    def enter_heading(self, heading: str, level: int) -> list[str]:
        normalized_heading = " ".join(heading.split())
        if not normalized_heading:
            return self.current_path()
        if normalized_heading.casefold() == self.title.casefold():
            self.stack = []
            return self.current_path()
        depth = max(0, min(level, 6) - 1)
        self.stack = [*self.stack[:depth], normalized_heading]
        return self.current_path()


def _normalized_block(
    index: int,
    text: str,
    *,
    kind: Literal["heading", "paragraph"],
    level: int | None,
    section_path: list[str],
) -> NormalizedBlock:
    return NormalizedBlock(
        block_id=f"block:{index}",
        kind=kind,
        text=text,
        level=level,
        locator={"page": 1, "block_index": index},
        metadata={"section_path": section_path},
    )


def _looks_like_html(text: str) -> bool:
    lowered = text[:4000].casefold()
    return "<html" in lowered or "<body" in lowered or bool(re.search(r"<h[1-6][\s>]", lowered))


def _markdown_heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
    if not match:
        return None
    return len(match.group(1)), match.group(2).strip()


def _inline_html_heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^<h([1-6])[^>]*>(.*?)</h\1>\s*$", line, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    value = re.sub(r"<[^>]+>", " ", match.group(2))
    return int(match.group(1)), " ".join(value.split())


def _extract_text_payload(payload: Any) -> str:
    candidates = _find_text_values(payload, keys=("text", "content", "markdown", "md_content", "text_content"))
    candidates = [candidate.strip() for candidate in candidates if candidate.strip()]
    if not candidates:
        return ""
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def _find_text_values(payload: Any, *, keys: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).casefold()
            if normalized_key in keys and isinstance(value, str):
                found.append(value)
            elif normalized_key in {"pages", "documents", "results", "document", "data"}:
                found.extend(_find_text_values(value, keys=keys))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_find_text_values(item, keys=keys))
    return found


def _extract_title(payload: Any) -> str | None:
    titles = _find_text_values(payload, keys=("title", "filename"))
    return titles[0].strip() if titles and titles[0].strip() else None


def _extract_warnings(payload: Any) -> list[str]:
    warnings: list[str] = []
    for value in _find_list_values(payload, keys=("warnings", "errors")):
        for item in value:
            if isinstance(item, str):
                warnings.append(_safe_parser_warning(item))
            elif isinstance(item, dict):
                code = item.get("code") or item.get("type") or item.get("message") or "parser_warning"
                warnings.append(_safe_parser_warning(str(code)))
    return warnings[:20]


def _extract_tables(payload: Any) -> list[NormalizedTable]:
    raw_tables = _find_list_values(payload, keys=("tables",))
    tables: list[NormalizedTable] = []
    for table_index, raw in enumerate((item for table in raw_tables for item in table), start=1):
        rows = _rows_from_table(raw)
        if rows:
            tables.append(
                NormalizedTable(
                    table_id=f"table:{table_index}",
                    rows=rows,
                    locator={"table_index": table_index},
                    metadata={"source": "parser"},
                )
            )
    return tables[:50]


def _find_list_values(payload: Any, *, keys: tuple[str, ...]) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).casefold()
            if normalized_key in keys and isinstance(value, list):
                found.append(value)
            else:
                found.extend(_find_list_values(value, keys=keys))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_find_list_values(item, keys=keys))
    return found


def _rows_from_table(raw: Any) -> list[list[str]]:
    if isinstance(raw, dict):
        for key in ("rows", "data", "cells"):
            nested_rows = _rows_from_table(raw.get(key))
            if nested_rows:
                return nested_rows
    if isinstance(raw, list):
        rows: list[list[str]] = []
        for row in raw:
            if isinstance(row, list):
                rows.append([str(cell) for cell in row])
            elif isinstance(row, dict):
                rows.append([str(value) for value in row.values()])
        return rows
    return []


def _clean_filename(filename: str) -> str:
    raw_name = filename.strip()
    normalized = raw_name.replace("\\", "/")
    if "/" in normalized or ".." in normalized:
        raise UploadValidationError("PATH_TRAVERSAL", "filename must not contain path traversal")
    name = normalized
    if not name or name in {".", ".."}:
        raise UploadValidationError("INVALID_FILENAME", "filename is invalid")
    return name[:240]


def _extension(filename: str) -> str:
    lowered = filename.casefold()
    if "." not in lowered:
        return ""
    return "." + lowered.rsplit(".", 1)[-1]


def _signature(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    if data.startswith(b"MZ"):
        return "exe"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole_cfb"
    if b"\x00" in data[:2048]:
        return "binary"
    return "text"


def _detected_mime(data: bytes, extension: str) -> str:
    signature = _signature(data)
    if signature == "pdf":
        return "application/pdf"
    if signature == "zip" and extension == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if signature == "zip" and extension == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if signature == "zip" and extension == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if extension in {".html", ".htm"}:
        return "text/html"
    if extension in {".csv", ".tsv"}:
        return "text/tabular"
    if extension in {".json", ".jsonl"}:
        return "application/json"
    if signature == "text":
        return "text/plain"
    return "application/octet-stream"


def _validate_signature(extension: str, data: bytes, signature: str) -> None:
    if signature == "exe":
        raise UploadValidationError("RENAMED_EXECUTABLE", "renamed executables are not accepted")
    if signature == "ole_cfb":
        raise UploadValidationError("LEGACY_OR_ENCRYPTED_OFFICE", "legacy or encrypted Office files are not accepted")
    if extension == ".pdf":
        if signature != "pdf":
            raise UploadValidationError("SIGNATURE_MISMATCH", "file signature does not match extension")
        if b"/Encrypt" in data[:200000]:
            raise UploadValidationError("ENCRYPTED_PDF", "encrypted PDF files are not accepted")
    if extension in {".docx", ".pptx", ".xlsx"} and signature != "zip":
        raise UploadValidationError("SIGNATURE_MISMATCH", "file signature does not match extension")
    if extension in TEXT_EXTENSIONS | STRUCTURED_EXTENSIONS and signature not in {"text"}:
        raise UploadValidationError("SIGNATURE_MISMATCH", "file signature does not match extension")


def _validate_json_depth(data: bytes, max_depth: int, *, json_lines: bool = False) -> None:
    try:
        if json_lines:
            payloads = [json.loads(line) for line in _decode_utf8(data).splitlines() if line.strip()]
            too_deep = any(_json_depth(payload) > max_depth for payload in payloads)
        else:
            payload = json.loads(_decode_utf8(data))
            too_deep = _json_depth(payload) > max_depth
    except json.JSONDecodeError as exc:
        raise UploadValidationError("JSON_DECODE_FAILED", "json content must be valid") from exc
    if too_deep:
        raise UploadValidationError("JSON_TOO_DEEP", "json nesting exceeds configured limit")


def _json_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 0


def _decode_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadValidationError("TEXT_DECODE_FAILED", "text content must be valid UTF-8") from exc


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _replacement_ratio(text: str) -> float:
    if not text:
        return 0.0
    return text.count("\ufffd") / len(text)


def _title_from_filename(filename: str) -> str:
    base = filename.rsplit(".", 1)[0].strip()
    return base or filename


def _locator_for_offset(blocks: list[NormalizedBlock], word_offset: int) -> dict[str, Any]:
    consumed = 0
    for block in blocks:
        block_words = len(block.text.split())
        if consumed <= word_offset < consumed + max(block_words, 1):
            return dict(block.locator)
        consumed += block_words
    return {"page": 1}


def _section_path_for_offset(blocks: list[NormalizedBlock], word_offset: int, title: str) -> list[str]:
    consumed = 0
    fallback = [title]
    for block in blocks:
        block_words = len(block.text.split())
        metadata = dict(block.metadata or {})
        section_path = metadata.get("section_path")
        if isinstance(section_path, list) and section_path:
            fallback = [str(item) for item in section_path if str(item)]
        if consumed <= word_offset < consumed + max(block_words, 1):
            return fallback
        consumed += block_words
    return fallback


def _section_id(document_version_id: str, section_path: list[str]) -> str:
    return "section:" + stable_hash([document_version_id, *section_path], 24)


def _link_neighbors(chunks: list[Chunk]) -> list[Chunk]:
    linked: list[Chunk] = []
    for index, chunk in enumerate(chunks):
        linked.append(
            replace(
                chunk,
                prev_chunk_id=chunks[index - 1].id if index > 0 else None,
                next_chunk_id=chunks[index + 1].id if index + 1 < len(chunks) else None,
            )
        )
    return linked


def _safe_parser_warning(value: str) -> str:
    lowered = value.casefold()
    if "stderr" in lowered or "traceback" in lowered or "s3://" in lowered or "/" in value or "\\" in value:
        return "parser_warning"
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())[:120]
    return normalized or "parser_warning"
