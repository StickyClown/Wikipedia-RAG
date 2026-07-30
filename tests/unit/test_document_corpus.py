from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from wikipediarag.config import Settings
from wikipediarag.document_corpus import (
    DOCUMENT_CORPUS_SCHEMA_VERSION,
    corpus_summary,
    load_manifest_corpus,
    materialize_corpus_item,
    synthetic_document_corpus,
)
from wikipediarag.document_ingestion import UploadValidationError, validate_upload_bytes


def test_synthetic_standard_corpus_is_deterministic_and_covers_core_formats() -> None:
    first = synthetic_document_corpus(fixture_set="standard")
    second = synthetic_document_corpus(fixture_set="standard")

    assert [item.id for item in first] == [item.id for item in second]
    assert [item.expected_sha256() for item in first] == [item.expected_sha256() for item in second]
    extensions = corpus_summary(first)["by_extension"]
    assert {".txt", ".md", ".html", ".csv", ".tsv", ".json", ".jsonl", ".pdf"}.issubset(extensions)
    assert corpus_summary(first)["by_outcome"]["completed"] >= 8
    assert corpus_summary(first)["by_outcome"]["failed"] >= 6


def test_synthetic_full_corpus_adds_office_formats() -> None:
    full = synthetic_document_corpus(fixture_set="full")
    by_extension = corpus_summary(full)["by_extension"]

    assert {".docx", ".pptx", ".xlsx"}.issubset(by_extension)
    for item in full:
        if item.filename.endswith((".docx", ".pptx", ".xlsx")):
            assert item.content is not None
            assert item.content.startswith(b"PK\x03\x04")


def test_synthetic_negative_fixtures_match_upload_validator_codes() -> None:
    settings = Settings(upload_json_max_depth=32)
    negative_codes = {
        "negative-archive": "ARCHIVE_NOT_ALLOWED",
        "negative-renamed-exe": "RENAMED_EXECUTABLE",
        "negative-signature-mismatch": "SIGNATURE_MISMATCH",
        "negative-encrypted-pdf": "ENCRYPTED_PDF",
        "negative-macro-office": "MACRO_OFFICE_NOT_ALLOWED",
        "negative-deep-json": "JSON_TOO_DEEP",
        "negative-remote-html": "REMOTE_HTML_RESOURCE",
    }

    items = {item.id: item for item in synthetic_document_corpus(fixture_set="standard")}
    for item_id, expected_code in negative_codes.items():
        item = items[item_id]
        assert item.content is not None
        with pytest.raises(UploadValidationError) as error:
            validate_upload_bytes(
                item.content,
                filename=item.filename,
                supplied_content_type=item.content_type,
                expected_size_bytes=len(item.content),
                expected_sha256=hashlib.sha256(item.content).hexdigest(),
                settings=settings,
            )
        assert error.value.code == expected_code


def test_manifest_corpus_requires_pinned_sha256_and_materializes_bytes(tmp_path: Path) -> None:
    content = b"external corpus sample 2026-07-29"
    manifest = {
        "schema_version": DOCUMENT_CORPUS_SCHEMA_VERSION,
        "documents": [
            {
                "id": "external-sample",
                "source_id": "legal_sample",
                "filename": "external.txt",
                "content_type": "text/plain",
                "url": "https://example.invalid/external.txt",
                "sha256": hashlib.sha256(content).hexdigest(),
                "license": "test-license",
                "expected_language": "en",
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    [item] = load_manifest_corpus(manifest_path)
    assert item.is_external
    materialized = materialize_corpus_item(item, data=content)
    assert materialized.content == content
    assert materialized.expected_sha256() == hashlib.sha256(content).hexdigest()

    with pytest.raises(ValueError, match="checksum mismatch"):
        materialize_corpus_item(item, data=b"wrong")


def test_manifest_corpus_skips_disabled_documents_by_default(tmp_path: Path) -> None:
    manifest = {
        "schema_version": DOCUMENT_CORPUS_SCHEMA_VERSION,
        "documents": [
            {
                "id": "disabled",
                "enabled": False,
                "source_id": "legal_sample",
                "filename": "disabled.txt",
                "content_type": "text/plain",
                "url": "https://example.invalid/disabled.txt",
                "sha256": hashlib.sha256(b"disabled").hexdigest(),
                "license": "test-license",
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert load_manifest_corpus(manifest_path) == []
    assert len(load_manifest_corpus(manifest_path, include_disabled=True)) == 1
