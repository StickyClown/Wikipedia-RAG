"""Immutable runtime binding for quality-evaluation source evidence.

The dataset stores logical source keys and quotations.  This module is the only
place that resolves those keys to mutable document/chunk runtime identifiers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any


class RuntimeBindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def binding_hash(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key not in {"binding_hash", "signature"}}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def sign_binding(payload: dict[str, Any], *, signing_key: str) -> str:
    if not signing_key:
        raise RuntimeBindingError("BINDING_SIGNING_KEY_MISSING", "a local binding signing key is required")
    return hmac.new(signing_key.encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest()


def verify_binding(payload: dict[str, Any], *, signing_key: str) -> None:
    expected_hash = binding_hash(payload)
    if not hmac.compare_digest(str(payload.get("binding_hash") or ""), expected_hash):
        raise RuntimeBindingError("RUNTIME_BINDING_STALE", "binding checksum does not match its payload")
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    expected_signature = sign_binding(unsigned, signing_key=signing_key)
    if not hmac.compare_digest(str(payload.get("signature") or ""), expected_signature):
        raise RuntimeBindingError("RUNTIME_BINDING_STALE", "binding signature does not match")


def build_runtime_binding(
    *,
    suite: str,
    dataset_hash: str,
    material_hash: str,
    namespace: str,
    source_mappings: dict[str, dict[str, str]],
    tasks: list[dict[str, Any]],
    fetch_context: Callable[[str], list[dict[str, Any]]],
    signing_key: str,
) -> dict[str, Any]:
    """Resolve quoted logical gold evidence through the ACL-protected viewer path."""

    source_entries: dict[str, dict[str, Any]] = {}
    evidence_entries: dict[str, dict[str, Any]] = {}
    for source_id, mapping in sorted(source_mappings.items()):
        document_id = str(mapping.get("document_id") or "")
        version_id = str(mapping.get("document_version_id") or "")
        kb_id = str(mapping.get("knowledge_base_id") or "")
        if not document_id or not version_id or not kb_id:
            raise RuntimeBindingError("RUNTIME_BINDING_STALE", f"source mapping is incomplete: {source_id}")
        chunks = fetch_context(document_id)
        if not chunks:
            raise RuntimeBindingError("RUNTIME_BINDING_STALE", f"document viewer returned no chunks: {source_id}")
        provenance = dict(chunks[0].get("provenance") or {})
        if (
            provenance.get("origin") != "source_ref"
            or provenance.get("source_namespace") != namespace
            or provenance.get("source_external_id") != source_id
            or provenance.get("document_id") != document_id
            or provenance.get("document_version_id") != version_id
        ):
            raise RuntimeBindingError("RUNTIME_BINDING_STALE", f"provenance mismatch: {source_id}")
        source_entries[source_id] = {
            "knowledge_base_id": kb_id,
            "document_id": document_id,
            "document_version_id": version_id,
            "source_version": str(provenance.get("source_version") or ""),
            "content_sha256": str(provenance.get("content_sha256") or ""),
            "processing_contract": dict(provenance.get("processing_contract") or {}),
        }
        for task in tasks:
            for evidence in task.get("gold_evidence") or []:
                if str(evidence.get("source_id") or "") != source_id:
                    continue
                quote = str(evidence.get("quote") or "")
                matches = [chunk for chunk in chunks if quote and quote in str(chunk.get("content") or "")]
                span_matches: list[list[dict[str, Any]]] = []
                if not matches and quote:
                    ordered = sorted(
                        chunks, key=lambda chunk: int(dict(chunk.get("provenance") or {}).get("chunk_ordinal") or 0)
                    )
                    for start in range(len(ordered)):
                        text = ""
                        for end in range(start, min(start + 4, len(ordered))):
                            text += str(ordered[end].get("content") or "")
                            if quote in text:
                                span_matches.append(ordered[start : end + 1])
                                break
                if not matches:
                    if span_matches and not bool(evidence.get("allow_continuous_span")):
                        raise RuntimeBindingError(
                            "GOLD_EVIDENCE_SPAN_NOT_ALLOWED", str(evidence.get("evidence_id") or source_id)
                        )
                    if len(span_matches) == 1:
                        matches = span_matches[0]
                    elif len(span_matches) > 1:
                        raise RuntimeBindingError(
                            "GOLD_EVIDENCE_AMBIGUOUS", str(evidence.get("evidence_id") or source_id)
                        )
                if not matches:
                    raise RuntimeBindingError("GOLD_QUOTE_UNRESOLVED", str(evidence.get("evidence_id") or source_id))
                if len(matches) != 1 and not span_matches:
                    raise RuntimeBindingError("GOLD_EVIDENCE_AMBIGUOUS", str(evidence.get("evidence_id") or source_id))
                chunk_provenance = [dict(chunk.get("provenance") or {}) for chunk in matches]
                evidence_entries[str(evidence.get("evidence_id") or "")] = {
                    "task_id": str(task.get("task_id") or ""),
                    "source_id": source_id,
                    "document_id": document_id,
                    "document_version_id": version_id,
                    "chunk_ids": [str(chunk.get("chunk_id") or "") for chunk in matches],
                    "source_chunk_ids": [str(value.get("source_chunk_id") or "") for value in chunk_provenance],
                    "locators": [
                        dict(value.get("locator") or chunk.get("locator") or {})
                        for chunk, value in zip(matches, chunk_provenance, strict=True)
                    ],
                    "content_hashes": [str(value.get("fragment_content_hash") or "") for value in chunk_provenance],
                }
    payload: dict[str, Any] = {
        "schema_version": "eval_runtime_binding_v2",
        "suite": suite,
        "dataset_hash": dataset_hash,
        "material_hash": material_hash,
        "namespace": namespace,
        "sources": source_entries,
        "gold_evidence": evidence_entries,
        "binding_hash": "",
    }
    payload["binding_hash"] = binding_hash(payload)
    payload["signature"] = sign_binding(payload, signing_key=signing_key)
    return payload


def bind_runtime_tasks(tasks: list[dict[str, Any]], binding: dict[str, Any]) -> list[dict[str, Any]]:
    """Return runtime copies; never mutate the frozen logical task files."""

    bound: list[dict[str, Any]] = []
    sources = dict(binding.get("sources") or {})
    evidence_by_id = dict(binding.get("gold_evidence") or {})
    for task in tasks:
        runtime = dict(task)
        source_ids = [str(value) for value in runtime.get("source_ids") or []]
        runtime["gold_document_ids"] = [
            str(sources[source_id]["document_id"]) for source_id in source_ids if source_id in sources
        ]
        runtime["gold_chunk_ids"] = []
        bound_evidence: list[dict[str, Any]] = []
        for item in runtime.get("gold_evidence") or []:
            updated = dict(item)
            resolved = evidence_by_id.get(str(updated.get("evidence_id") or ""))
            if resolved:
                updated["document_id"] = resolved["document_id"]
                updated["chunk_id"] = resolved["chunk_ids"][0]
                runtime["gold_chunk_ids"].extend(resolved["chunk_ids"])
            bound_evidence.append(updated)
        runtime["gold_evidence"] = bound_evidence
        runtime["gold_chunk_ids"] = list(dict.fromkeys(runtime["gold_chunk_ids"]))
        bound.append(runtime)
    return bound
