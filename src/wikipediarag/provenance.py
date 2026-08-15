"""Canonical, safe source provenance helpers.

The database source state is the owner of external identity.  This module only
builds stable projections for document versions, chunks and public responses;
it never grants authority from caller-provided metadata.
"""

from __future__ import annotations

import json
from typing import Any

from wikipediarag.ids import stable_hash, stable_uuid

SOURCE_REFERENCE_SCHEMA_VERSION = "source_ref_v1"
SOURCE_PROVENANCE_SCHEMA_VERSION = "source_provenance_v1"


def direct_upload_source_id(*, tenant_id: str, knowledge_base_id: str, namespace: str) -> str:
    """Return the server-owned source ID for an upload namespace."""

    return str(stable_uuid([tenant_id, knowledge_base_id, "direct_upload", namespace]))


def source_document_id(*, tenant_id: str, knowledge_base_id: str, source_id: str, external_id: str) -> str:
    return "src:" + stable_hash([tenant_id, knowledge_base_id, source_id, external_id], 32)


def source_document_version_id(
    *, document_id: str, source_version: str, content_sha256: str, parser_profile: str
) -> str:
    return "docv:" + stable_hash(
        [document_id, source_version, content_sha256, parser_profile, "normalized_document_v1"], 32
    )


def source_chunk_id(
    *,
    document_version_id: str,
    normalized_hash: str,
    locator: dict[str, Any],
    ordinal: int,
    content_hash: str,
    parser_contract: str,
    chunker_contract: str,
) -> str:
    """Identify a fragment of one processed document version.

    A chunk is intentionally not stable across a normalization/parser/chunker
    change.  Its source identity is stable only for the exact processing
    contract recorded alongside it.
    """

    return "srcchunk:" + stable_hash(
        [
            document_version_id,
            normalized_hash,
            parser_contract,
            chunker_contract,
            json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ordinal,
            content_hash,
        ],
        32,
    )


def build_source_provenance(
    *,
    source_reference: dict[str, Any] | None,
    document_id: str,
    document_version_id: str,
    checksum_sha256: str = "",
    filename: str = "",
    content_type: str = "",
    size_bytes: int | None = None,
    source_uri: str = "",
    source_url: str = "",
    processing_contract: dict[str, Any] | None = None,
    source_chunk_id_value: str = "",
    fragment_content_hash: str = "",
    chunk_ordinal: int | None = None,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sole public projection shape used by all read paths."""

    reference = dict(source_reference or {})
    has_reference = bool(reference.get("namespace") and reference.get("external_id"))
    return {
        "schema_version": SOURCE_PROVENANCE_SCHEMA_VERSION,
        "origin": "source_ref" if has_reference else "legacy_generated",
        "source_namespace": str(reference.get("namespace") or "legacy"),
        "source_external_id": str(reference.get("external_id") or document_id),
        "source_version": str(
            reference.get("source_version") or (f"sha256:{checksum_sha256}" if checksum_sha256 else "")
        ),
        "document_id": document_id,
        "document_version_id": document_version_id,
        "content_sha256": checksum_sha256,
        "original_filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "source_uri": source_uri,
        "source_url": source_url,
        "attributes": dict(reference.get("attributes") or {}),
        "processing_contract": dict(processing_contract or {}),
        "source_chunk_id": source_chunk_id_value,
        "fragment_content_hash": fragment_content_hash,
        "chunk_ordinal": chunk_ordinal,
        "locator": dict(locator or {}),
    }


def public_provenance_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    document_id: str,
    document_version_id: str = "",
    source_uri: str = "",
    source_url: str = "",
    chunk_id: str = "",
) -> dict[str, Any]:
    """Return a compatible provenance projection for new and legacy rows."""

    values = dict(metadata or {})
    stored = values.get("source_provenance")
    if isinstance(stored, dict):
        projected = dict(stored)
        projected.setdefault("document_id", document_id)
        projected.setdefault("document_version_id", document_version_id)
        projected.setdefault("source_uri", source_uri)
        projected.setdefault("source_url", source_url)
        projected.setdefault("source_chunk_id", str(values.get("source_chunk_id") or chunk_id))
        projected.setdefault("fragment_content_hash", str(values.get("content_hash") or ""))
        projected.setdefault("chunk_ordinal", values.get("chunk_ordinal"))
        projected.setdefault("locator", dict(values.get("locator") or {}))
        return projected
    projected = build_source_provenance(
        source_reference=None,
        document_id=document_id,
        document_version_id=document_version_id,
        checksum_sha256=str(values.get("checksum_sha256") or values.get("content_hash") or ""),
        filename=str(values.get("filename") or ""),
        source_uri=source_uri,
        source_url=source_url,
        source_chunk_id_value=str(values.get("source_chunk_id") or chunk_id),
        fragment_content_hash=str(values.get("content_hash") or ""),
        chunk_ordinal=int(values["chunk_ordinal"]) if isinstance(values.get("chunk_ordinal"), int) else None,
        locator=dict(values.get("locator") or {}),
    )
    projected["source_namespace"] = str(values.get("source_namespace") or "legacy")
    projected["source_external_id"] = str(
        values.get("source_external_id") or values.get("source_document_id") or document_id
    )
    projected["source_version"] = str(values.get("source_version") or projected["source_version"])
    return projected
