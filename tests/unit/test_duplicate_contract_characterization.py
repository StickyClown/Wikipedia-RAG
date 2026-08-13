"""Observed contracts for implementations intentionally kept separate today."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wikipediarag.api.handlers import _decrypt_api_credentials, _parse_search_date
from wikipediarag.eval import (
    generator,
    progress,
    reporting,
    retrieval_reporting,
    retrieval_runner,
    review,
    runner,
    trusted,
)
from wikipediarag.eval.generate_runs import normalize_family_targets
from wikipediarag.eval.trusted import trusted_family_targets
from wikipediarag.ingestion import _decrypt_connector_credentials
from wikipediarag.schemas import DocumentReprocessResponse, UploadCompleteResponse
from wikipediarag.search_service import _parse_date
from wikipediarag.wiki_dump import Chunk, link_neighbors
from wikipediarag.zim_dump import _link_neighbors


def _chunk(index: int) -> Chunk:
    return Chunk(
        id=f"chunk-{index}",
        document_id="document",
        page_id=1,
        revision_id=1,
        title="Title",
        section_path=("Lead",),
        content=f"content {index}",
        parent_chunk_id=None,
        prev_chunk_id=None,
        next_chunk_id=None,
        source_uri="source",
        source_url="https://example.test/source",
        content_hash=f"h{index}",
        embedding=[],
        metadata={"ordinal": index},
    )


@pytest.mark.parametrize("count", [0, 1, 3])
def test_d01_xml_and_zim_neighbor_links_are_equivalent(count: int) -> None:
    chunks = [_chunk(index) for index in range(count)]
    assert link_neighbors(chunks) == _link_neighbors(chunks)


@pytest.mark.parametrize(
    "value", [None, date(2026, 8, 12), datetime(2026, 8, 12, 10, 30), "2026-08-12T10:30:00", "bad", "2026-08"]
)
def test_d02_optional_date_parsers_are_equivalent(value: object) -> None:
    assert _parse_date(value) == _parse_search_date(value)


@pytest.mark.parametrize("payload", [{}, {"access": "encrypted"}, {"broken": object()}])
def test_d03_credential_boundaries_have_same_conversion_and_error_semantics(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    calls: list[dict[str, str]] = []

    def decrypt(_settings: object, received: dict[str, str]) -> dict[str, str]:
        calls.append(received)
        if "broken" in received:
            raise ValueError("corrupted credentials")
        return {"decrypted": "ok"}

    monkeypatch.setattr("wikipediarag.oidc_service.decrypt_server_tokens", decrypt)
    outcomes: list[object] = []
    for boundary in (_decrypt_connector_credentials, _decrypt_api_credentials):
        try:
            outcomes.append(boundary(cast(Any, object()), payload))
        except ValueError as exc:
            outcomes.append((type(exc), str(exc)))
    assert outcomes[0] == outcomes[1]
    if payload:
        assert calls[0] == calls[1]


def test_d04_quota_allocation_matches_for_valid_weights() -> None:
    generated = normalize_family_targets(37, {family: 1.0 for family in normalize_family_targets(1, None)})
    trusted_targets = trusted_family_targets(37, {family: 1.0 for family in trusted.TRUSTED_FAMILY_ORDER})
    for targets in (generated, trusted_targets):
        assert sum(targets.values()) == 37
        assert max(targets.values()) - min(targets.values()) <= 1
        assert list(targets.values())[0] == max(targets.values())
    with pytest.raises(ValueError, match="eval-generate"):
        normalize_family_targets(0, None)
    with pytest.raises(ValueError, match="eval-trusted"):
        trusted_family_targets(0)


@pytest.mark.parametrize("value", [None, [], ["  one ", "", "two"], "  one  two "])
def test_d05_list_normalization_is_equivalent(value: object) -> None:
    assert generator._string_list(value) == trusted._string_list(value)


@pytest.mark.parametrize("value", ["", "  one   sentence. Second sentence", "x" * 300])
def test_d05_short_answer_normalization_is_equivalent(value: str) -> None:
    assert generator._short_answer(value) == trusted._short_answer(value)


@pytest.mark.parametrize("seconds", [-4, 0, 3661.8])
def test_d06_elapsed_formatters_are_equivalent(seconds: float) -> None:
    values = [
        progress._format_elapsed(seconds),
        retrieval_runner._format_elapsed(seconds),
        review._format_elapsed(seconds),
        runner._format_elapsed(seconds),
        trusted._format_elapsed(seconds),
    ]
    assert values == [values[0]] * len(values)


def test_d07_report_rows_sort_the_same_way() -> None:
    manifest = SimpleNamespace(
        config_summaries=[
            SimpleNamespace(
                config_id="z",
                metrics={"stage_latency_b_p95_ms": 2, "stage_latency_b_p50_ms": 1, "stage_latency_a_p95_ms": 4},
                contract_ids={"z": ["2"], "a": ["1"]},
            )
        ]
    )
    assert reporting._stage_timing_rows(cast(Any, manifest)) == retrieval_reporting._stage_timing_rows(
        cast(Any, manifest)
    )
    assert reporting._contract_rows(cast(Any, manifest)) == retrieval_reporting._contract_rows(cast(Any, manifest))


def test_d08_shared_run_artifact_rules_are_equivalent() -> None:
    results = [
        SimpleNamespace(contract_ids={"index": "i", "run": "r"}, latency_ms={"search": 5.0}),
        SimpleNamespace(contract_ids={"index": "i", "run": "r"}, latency_ms={"search": 9.0}),
    ]
    assert runner._summary_contract_ids(cast(Any, results)) == retrieval_runner._summary_contract_ids(
        cast(Any, results)
    )
    summary = runner._summary_contract_ids(cast(Any, results))
    assert runner._contract_mix_errors(summary) == retrieval_runner._contract_mix_errors(summary)
    assert runner._stage_latency_metrics(cast(Any, results)) == retrieval_runner._stage_latency_metrics(
        cast(Any, results)
    )
    configs = [SimpleNamespace(config_id="a"), SimpleNamespace(config_id="b")]
    assert runner._filter_configs(cast(Any, configs), {"b"}) == retrieval_runner._filter_configs(
        cast(Any, configs), {"b"}
    )
    assert runner._format_eta(None) == retrieval_runner._format_eta(None)
    assert runner._format_eta(3661) == retrieval_runner._format_eta(3661)


def test_d09_ingestion_completion_schemas_have_one_observed_wire_contract() -> None:
    payload = {"document_id": "d", "document_version_id": "v", "job_id": "j", "status": "queued"}
    assert (
        UploadCompleteResponse.model_validate(payload).model_dump()
        == DocumentReprocessResponse.model_validate(payload).model_dump()
    )
    for schema in (UploadCompleteResponse, DocumentReprocessResponse):
        with pytest.raises(ValueError):
            schema.model_validate({"document_id": "d"})
