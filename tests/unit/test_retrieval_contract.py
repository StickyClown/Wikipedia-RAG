from __future__ import annotations

import pytest

from wikipediarag.config import Settings
from wikipediarag.retrieval_contract import (
    IndexContract,
    KnowledgeBaseNotReady,
    build_index_contract,
    build_run_contract,
    index_contract_metadata,
    validate_index_version_contract,
)
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile


def _profile() -> RetrievalProfile:
    return get_retrieval_profile("test_mock", Settings())


def _index_contract(dimensions: int = 64) -> IndexContract:
    return build_index_contract(
        index_version="wikipedia_xml:snapshot:test_mock:mock_embed_default:64",
        source_type="wikipedia_xml",
        snapshot_id="snapshot",
        physical_index="wiki-chunks-v1",
        read_alias="wiki-chunks-read",
        embedding_alias="mock_embed_default",
        embedding_dimensions=dimensions,
        profile=_profile(),
        settings=Settings(),
    )


def test_index_contract_hash_is_stable_and_changes_on_index_inputs() -> None:
    first = _index_contract()
    second = _index_contract()
    changed = _index_contract(dimensions=128)

    assert first.contract_id == second.contract_id
    assert first.contract_id != changed.contract_id


def test_run_contract_hash_changes_with_overrides() -> None:
    index = _index_contract()
    profile = _profile()

    first = build_run_contract(index_contract_id=index.contract_id, profile=profile, settings=Settings())
    changed = build_run_contract(
        index_contract_id=index.contract_id,
        profile=profile,
        retrieval_overrides={"retrieval": {"rerank": False}},
        settings=Settings(),
    )

    assert first.contract_id != changed.contract_id


def test_run_contract_hash_changes_with_verification_policy() -> None:
    index = _index_contract()
    base = _profile()
    verified = get_retrieval_profile(
        "test_mock",
        Settings(),
        overrides={"answer": {"verification": {"claim_verification": "deterministic_strict"}}},
    )

    first = build_run_contract(index_contract_id=index.contract_id, profile=base, settings=Settings())
    changed = build_run_contract(index_contract_id=index.contract_id, profile=verified, settings=Settings())

    assert first.verification_policy["claim_verification"] == "off"
    assert changed.verification_policy["claim_verification"] == "deterministic_strict"
    assert first.contract_id != changed.contract_id


def test_run_contract_hash_changes_with_policy_versions() -> None:
    index = _index_contract()
    contract = build_run_contract(index_contract_id=index.contract_id, profile=_profile(), settings=Settings())
    changed_answerability = contract.model_copy(update={"answerability_gate_version": "answerability_gate_v5"})
    changed_negative_policy = contract.model_copy(
        update={"negative_evidence_policy_version": "explicit_negative_title_v2"}
    )

    assert contract.answerability_gate_version == "answerability_gate_v4"
    assert contract.negative_evidence_policy_version == "explicit_negative_title_v1"
    assert contract.contract_id != changed_answerability.contract_id
    assert contract.contract_id != changed_negative_policy.contract_id


def test_index_version_validation_rejects_incompatible_source() -> None:
    index = _index_contract()
    metadata = index_contract_metadata(index)
    row = {
        "id": index.index_version,
        "source_type": "zim",
        "snapshot_id": index.snapshot_id,
        "embedding_alias": "mock_embed_default",
        "embedding_dimensions": 64,
        "physical_index": index.physical_index,
        "read_alias": index.read_alias,
        "metadata": metadata,
    }

    with pytest.raises(KnowledgeBaseNotReady) as exc:
        validate_index_version_contract(row, profile=_profile(), settings=Settings())

    assert exc.value.code == "KB_NOT_READY"
    assert exc.value.details["expected_source_type"] == "wikipedia_xml"


def test_index_version_validation_returns_contract_ids_for_matching_row() -> None:
    index = _index_contract()
    row = {
        "id": index.index_version,
        "source_type": index.source_type,
        "snapshot_id": index.snapshot_id,
        "embedding_alias": "mock_embed_default",
        "embedding_dimensions": 64,
        "physical_index": index.physical_index,
        "read_alias": index.read_alias,
        "metadata": index_contract_metadata(index),
    }

    active = validate_index_version_contract(row, profile=_profile(), settings=Settings())

    assert active.index_contract_id == index.contract_id
    assert active.run_contract_id.startswith("sha256:")
