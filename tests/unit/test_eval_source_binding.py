from __future__ import annotations

from typing import Any

import pytest

from wikipediarag.eval.source_binding import (
    RuntimeBindingError,
    bind_runtime_tasks,
    build_runtime_binding,
    verify_binding,
)


def _context(_document_id: str) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": "chunk-1",
            "content": "The unique gold quotation is here.",
            "locator": {"page": 1},
            "provenance": {
                "origin": "source_ref",
                "source_namespace": "eval:p0-search-quality-v2:dataset",
                "source_external_id": "source-1",
                "document_id": "doc-1",
                "document_version_id": "docv-1",
                "source_version": "sha256:abc",
                "content_sha256": "abc",
                "source_chunk_id": "srcchunk-1",
                "locator": {"page": 1},
                "processing_contract": {"chunker": "document_chunker_v1"},
            },
        }
    ]


def _binding() -> dict[str, object]:
    return build_runtime_binding(
        suite="p0-search-quality-v2",
        dataset_hash="dataset",
        material_hash="materials",
        namespace="eval:p0-search-quality-v2:dataset",
        source_mappings={
            "source-1": {"knowledge_base_id": "kb-1", "document_id": "doc-1", "document_version_id": "docv-1"}
        },
        tasks=[
            {
                "task_id": "task-1",
                "source_ids": ["source-1"],
                "gold_evidence": [{"evidence_id": "e-1", "source_id": "source-1", "quote": "unique gold quotation"}],
            }
        ],
        fetch_context=_context,
        signing_key="test-signing-key",
    )


def test_runtime_binding_is_signed_and_creates_runtime_task_copy() -> None:
    binding = _binding()
    verify_binding(binding, signing_key="test-signing-key")
    tasks = bind_runtime_tasks(
        [
            {
                "task_id": "task-1",
                "source_ids": ["source-1"],
                "gold_evidence": [{"evidence_id": "e-1", "source_id": "source-1", "chunk_id": "logical"}],
            }
        ],
        binding,
    )
    assert tasks[0]["gold_document_ids"] == ["doc-1"]
    assert tasks[0]["gold_chunk_ids"] == ["chunk-1"]
    assert tasks[0]["gold_evidence"][0]["chunk_id"] == "chunk-1"


def test_runtime_binding_rejects_tampering_and_ambiguous_quotes() -> None:
    binding = _binding()
    binding["dataset_hash"] = "tampered"
    with pytest.raises(RuntimeBindingError, match="RUNTIME_BINDING_STALE"):
        verify_binding(binding, signing_key="test-signing-key")

    def ambiguous(_document_id: str) -> list[dict[str, object]]:
        return _context("doc-1") * 2

    with pytest.raises(RuntimeBindingError, match="GOLD_EVIDENCE_AMBIGUOUS"):
        build_runtime_binding(
            suite="suite",
            dataset_hash="dataset",
            material_hash="materials",
            namespace="eval:p0-search-quality-v2:dataset",
            source_mappings={
                "source-1": {
                    "knowledge_base_id": "kb",
                    "document_id": "doc-1",
                    "document_version_id": "docv-1",
                }
            },
            tasks=[
                {
                    "task_id": "t",
                    "gold_evidence": [{"evidence_id": "e", "source_id": "source-1", "quote": "unique gold quotation"}],
                }
            ],
            fetch_context=ambiguous,
            signing_key="test-signing-key",
        )


def test_runtime_binding_allows_only_explicit_continuous_span() -> None:
    chunks = _context("doc-1")
    chunks[0]["content"] = "first half "
    second = dict(chunks[0])
    second["chunk_id"] = "chunk-2"
    second["content"] = "second half"
    second["provenance"] = {**dict(second["provenance"]), "source_chunk_id": "srcchunk-2", "chunk_ordinal": 2}

    with pytest.raises(RuntimeBindingError, match="GOLD_EVIDENCE_SPAN_NOT_ALLOWED"):
        build_runtime_binding(
            suite="suite",
            dataset_hash="dataset",
            material_hash="materials",
            namespace="eval:p0-search-quality-v2:dataset",
            source_mappings={
                "source-1": {"knowledge_base_id": "kb", "document_id": "doc-1", "document_version_id": "docv-1"}
            },
            tasks=[
                {
                    "task_id": "t",
                    "gold_evidence": [{"evidence_id": "e", "source_id": "source-1", "quote": "first half second half"}],
                }
            ],
            fetch_context=lambda _id: [chunks[0], second],
            signing_key="test-signing-key",
        )
