from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from wikipediarag.api.handlers import _upload_document_identity, _upload_session_metadata
from wikipediarag.document_ingestion import (
    DocumentMetadata,
    NormalizedBlock,
    NormalizedDocument,
    chunks_for_normalized_document,
)
from wikipediarag.schemas import SourceReferenceInput


def _source_ref() -> SourceReferenceInput:
    return SourceReferenceInput(
        namespace="eval:p0-search-quality-v2:dataset-hash",
        external_id="logical-source-17",
        attributes={"original_system_name": "legacy-export"},
    )


def test_source_reference_allows_future_display_attribute_but_rejects_authoritative_metadata() -> None:
    reference = _source_ref()
    assert reference.attributes["original_system_name"] == "legacy-export"

    with pytest.raises(ValidationError, match="reserved"):
        SourceReferenceInput(namespace="manual", external_id="a", attributes={"filename": "secret.pdf"})
    with pytest.raises(ValidationError):
        SourceReferenceInput.model_validate({"namespace": "manual", "external_id": "a", "tenant_id": "bad"})


def test_source_identity_keeps_document_but_versions_changed_bytes() -> None:
    reference = _source_ref()
    first = _upload_document_identity(
        tenant_id="tenant-a",
        knowledge_base_id="kb-a",
        checksum_sha256="a" * 64,
        filename="first-name.txt",
        parser_profile="standard",
        source_ref=reference,
    )
    renamed = _upload_document_identity(
        tenant_id="tenant-a",
        knowledge_base_id="kb-a",
        checksum_sha256="a" * 64,
        filename="second-name.txt",
        parser_profile="standard",
        source_ref=reference,
    )
    changed = _upload_document_identity(
        tenant_id="tenant-a",
        knowledge_base_id="kb-a",
        checksum_sha256="b" * 64,
        filename="second-name.txt",
        parser_profile="standard",
        source_ref=reference,
    )
    isolated = _upload_document_identity(
        tenant_id="tenant-a",
        knowledge_base_id="kb-b",
        checksum_sha256="a" * 64,
        filename="first-name.txt",
        parser_profile="standard",
        source_ref=reference,
    )

    assert first[:2] == renamed[:2]
    assert first[0] == changed[0]
    assert first[1] != changed[1]
    assert first[0] != isolated[0]


def test_upload_metadata_cannot_override_provenance() -> None:
    with pytest.raises(Exception, match="RESERVED_UPLOAD_METADATA"):
        _upload_session_metadata({"source_provenance": {}})
    stored = _upload_session_metadata({"label": "visible"}, source_ref=_source_ref())
    assert stored["label"] == "visible"
    assert stored["source_reference"]["external_id"] == "logical-source-17"


def test_source_chunk_ids_are_deterministic_and_bound_to_processing_contract() -> None:
    document = NormalizedDocument(
        title="Example",
        text=" ".join(f"word-{index}" for index in range(260)),
        blocks=[
            NormalizedBlock(
                block_id="block-1",
                kind="paragraph",
                text=" ".join(f"word-{index}" for index in range(260)),
                locator={"page": 1},
            )
        ],
        metadata=DocumentMetadata(detected_language="en", language_confidence=1.0),
        parser_name="test-parser",
        parser_version="1",
        parser_route="test",
        parser_options={},
        source_metadata={},
    )
    kwargs: dict[str, Any] = {
        "document_id": "doc:test",
        "document_version_id": "docv:test",
        "source_url": "https://example.test/document",
        "dimensions": 4,
        "source_reference": _source_ref().model_dump(mode="json"),
        "source_version_metadata": {
            "content_sha256": "a" * 64,
            "original_filename": "example.txt",
            "content_type": "text/plain",
            "size_bytes": 1024,
        },
    }
    first = chunks_for_normalized_document(document, **kwargs)
    second = chunks_for_normalized_document(document, **kwargs)

    assert [chunk.metadata["source_chunk_id"] for chunk in first] == [
        chunk.metadata["source_chunk_id"] for chunk in second
    ]
    provenance = cast(dict[str, Any], first[0].metadata["source_provenance"])
    attributes = cast(dict[str, Any], provenance["attributes"])
    assert provenance["origin"] == "source_ref"
    assert attributes["original_system_name"] == "legacy-export"
