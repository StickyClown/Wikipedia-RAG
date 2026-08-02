from __future__ import annotations

import pytest

from wikipediarag.config import Settings
from wikipediarag.document_ingestion import (
    DocumentMetadata,
    FileValidation,
    NormalizedBlock,
    NormalizedDocument,
    UploadValidationError,
    _document_from_text,
    _next_parser_endpoint,
    chunks_for_normalized_document,
    detect_document_date,
    detect_language,
    normalized_document_hash,
    quality_gate_fallback_reasons,
    safe_public_metadata,
    sha256_hex,
    validate_upload_bytes,
)
from wikipediarag.ingestion import _parser_runtime_progress


def _settings(
    *,
    upload_max_bytes: int = 1024,
    upload_json_max_depth: int = 3,
    document_parser_services_required: bool = False,
) -> Settings:
    return Settings(
        upload_max_bytes=upload_max_bytes,
        upload_json_max_depth=upload_json_max_depth,
        document_parser_services_required=document_parser_services_required,
    )


def _validation(filename: str = "document.pdf") -> FileValidation:
    content = b"%PDF-1.4\nbody"
    return FileValidation(
        filename=filename,
        extension=".pdf",
        supplied_content_type="application/pdf",
        detected_mime="application/pdf",
        signature="pdf",
        size_bytes=len(content),
        checksum_sha256=sha256_hex(content),
    )


def _document(text: str = "Проверочный документ от 29.07.2026 содержит таблицу.") -> NormalizedDocument:
    return NormalizedDocument(
        title="Проверка",
        text=text,
        blocks=[
            NormalizedBlock(
                block_id="block:1",
                text=text,
                locator={"page": 1, "block_index": 1},
            )
        ],
        parser_name="xberg",
        parser_version="1.0.3",
        parser_route="xberg",
        metadata=DocumentMetadata(detected_language="ru", language_confidence=0.99, document_date="2026-07-29"),
    )


def test_validate_upload_rejects_unsafe_files() -> None:
    settings = _settings(upload_max_bytes=16)
    content = b"hello"
    checksum = sha256_hex(content)

    with pytest.raises(UploadValidationError, match="file is empty") as zero_byte:
        validate_upload_bytes(
            b"",
            filename="empty.txt",
            supplied_content_type="text/plain",
            expected_size_bytes=0,
            expected_sha256=sha256_hex(b""),
            settings=settings,
        )
    assert zero_byte.value.code == "ZERO_BYTE"

    with pytest.raises(UploadValidationError) as traversal:
        validate_upload_bytes(
            content,
            filename="../secret.txt",
            supplied_content_type="text/plain",
            expected_size_bytes=len(content),
            expected_sha256=checksum,
            settings=settings,
        )
    assert traversal.value.code == "PATH_TRAVERSAL"

    with pytest.raises(UploadValidationError) as archive:
        validate_upload_bytes(
            content,
            filename="docs.zip",
            supplied_content_type="application/zip",
            expected_size_bytes=len(content),
            expected_sha256=checksum,
            settings=settings,
        )
    assert archive.value.code == "ARCHIVE_NOT_ALLOWED"

    executable = b"MZrenamed executable"
    with pytest.raises(UploadValidationError) as renamed_executable:
        validate_upload_bytes(
            executable,
            filename="report.txt",
            supplied_content_type="text/plain",
            expected_size_bytes=len(executable),
            expected_sha256=sha256_hex(executable),
            settings=_settings(upload_max_bytes=64),
        )
    assert renamed_executable.value.code == "RENAMED_EXECUTABLE"


def test_validate_upload_rejects_remote_html_and_deep_json() -> None:
    html = b'<html><img src="https://example.test/pixel.png"></html>'
    with pytest.raises(UploadValidationError) as remote_html:
        validate_upload_bytes(
            html,
            filename="page.html",
            supplied_content_type="text/html",
            expected_size_bytes=len(html),
            expected_sha256=sha256_hex(html),
            settings=_settings(),
        )
    assert remote_html.value.code == "REMOTE_HTML_RESOURCE"

    deep_json = b'{"a":{"b":{"c":{"d":1}}}}'
    with pytest.raises(UploadValidationError) as too_deep:
        validate_upload_bytes(
            deep_json,
            filename="data.json",
            supplied_content_type="application/json",
            expected_size_bytes=len(deep_json),
            expected_sha256=sha256_hex(deep_json),
            settings=_settings(upload_json_max_depth=3),
        )
    assert too_deep.value.code == "JSON_TOO_DEEP"


def test_language_and_document_date_are_fast_local_metadata() -> None:
    language, confidence, alternatives = detect_language("Документ содержит русские слова и дату 29.07.2026")
    document_date, candidates = detect_document_date("Подписано 29 июля 2026 и повторно 2026-07-30.")

    assert language == "ru"
    assert confidence > 0.8
    assert alternatives
    assert document_date == "2026-07-30"
    assert {candidate["value"] for candidate in candidates} == {"2026-07-29", "2026-07-30"}


def test_parser_quality_gate_routes_to_docling_for_low_quality_xberg_output() -> None:
    document = _document(text="bad\ufffd" * 10)
    reasons = quality_gate_fallback_reasons(
        document,
        validation=_validation(filename="table-report.pdf"),
        parser_profile="standard",
    )

    assert "replacement_character_ratio" in reasons
    assert "scanned_or_low_text_pdf" in reasons
    assert "expected_tables_missing" in reasons


@pytest.mark.asyncio
async def test_parser_endpoint_pool_round_robins_without_leaking_urls() -> None:
    settings = Settings(
        xberg_url="http://xberg-default:8000",
        xberg_urls="http://xberg-a:8000, http://xberg-b:8000",
        docling_url="http://docling-default:5001",
        docling_urls="http://docling-a:5001",
    )

    first = await _next_parser_endpoint("xberg", settings)
    second = await _next_parser_endpoint("xberg", settings)
    third = await _next_parser_endpoint("xberg", settings)
    docling = await _next_parser_endpoint("docling", settings)

    assert first == ("http://xberg-a:8000", 0, 2)
    assert second == ("http://xberg-b:8000", 1, 2)
    assert third == ("http://xberg-a:8000", 0, 2)
    assert docling == ("http://docling-a:5001", 0, 1)


def test_parser_runtime_progress_uses_safe_numeric_fields_only() -> None:
    document = _document()
    document.parser_options = {
        "profile": "standard",
        "endpoint_index": 4,
        "endpoint_pool_size": 3,
        "queue_wait_ms": 12,
        "parser_latency_ms": 345,
        "endpoint_url": "http://internal-parser:8000",
    }

    progress = _parser_runtime_progress(document)

    assert progress == {
        "parser_queue_wait_ms": 12,
        "parser_latency_ms": 345,
        "parser_endpoint_pool_size": 3,
    }
    assert "endpoint_url" not in progress


def test_normalized_hash_and_chunk_ids_are_deterministic() -> None:
    words = " ".join(f"слово{i}" for i in range(260))
    first = _document(words)
    second = _document(words)

    assert normalized_document_hash(first) == normalized_document_hash(second)
    first_chunks = chunks_for_normalized_document(
        first,
        document_id="doc:test",
        document_version_id="docv:test",
        source_url="http://localhost/doc",
        dimensions=4,
    )
    second_chunks = chunks_for_normalized_document(
        second,
        document_id="doc:test",
        document_version_id="docv:test",
        source_url="http://localhost/doc",
        dimensions=4,
    )

    assert [chunk.id for chunk in first_chunks] == [chunk.id for chunk in second_chunks]
    assert first_chunks[0].next_chunk_id == first_chunks[1].id
    assert first_chunks[1].prev_chunk_id == first_chunks[0].id
    assert first_chunks[0].metadata["locator"] == {"page": 1, "block_index": 1}


def test_chunker_preserves_uploaded_document_headings_as_sections() -> None:
    document = _document_from_text(
        "# Overview\n\nIntro marker.\n\n## Details\n\nDeep section marker.",
        title="Проверка",
        metadata=DocumentMetadata(detected_language="ru", language_confidence=0.99),
        parser_name="local_text_adapter",
        parser_version="normalized_document_v1",
        parser_route="local_text_adapter",
        parser_options={},
        source_metadata={},
        warnings=[],
    )

    chunks = chunks_for_normalized_document(
        document,
        document_id="doc:test",
        document_version_id="docv:test",
        source_url="http://localhost/doc",
        dimensions=4,
    )

    assert chunks
    assert chunks[0].section_path == ("Проверка", "Overview")
    assert chunks[0].parent_chunk_id
    assert chunks[0].metadata["section_id"] == chunks[0].parent_chunk_id
    assert chunks[0].metadata["chunk_ordinal"] == 1


def test_public_metadata_does_not_leak_private_storage_or_parser_details() -> None:
    validation = _validation(filename="report.pdf")
    metadata = DocumentMetadata(detected_language="ru", language_confidence=0.98, document_date="2026-07-29")
    public = safe_public_metadata(
        validation=validation,
        metadata=metadata,
        normalized_hash="abc",
        parser_route="xberg",
        parser_name="xberg",
        parser_version="1.0.3",
        warnings=["parser_stderr:/tmp/private/key"],
    )

    assert "object_key" not in public
    assert "original_artifact_key" not in public
    assert public["warnings"] == ["parser_warning"]
    assert public["filename"] == "report.pdf"
    assert public["detected_language"] == "ru"
    assert public["document_date"] == "2026-07-29"
