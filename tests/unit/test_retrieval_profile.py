from __future__ import annotations

import pytest

from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile


def test_sota_mvp_profile_matches_required_defaults() -> None:
    profile = get_retrieval_profile("sota_mvp")

    assert profile.source == "zim"
    assert profile.retrieval.bm25 is True
    assert profile.retrieval.dense is True
    assert profile.retrieval.fusion == "rrf"
    assert profile.retrieval.bm25_top_k == 100
    assert profile.retrieval.dense_top_k == 100
    assert profile.retrieval.rerank_top_k == 50
    assert profile.postprocess.page_quota == 2
    assert profile.postprocess.parent_expansion == "selective"
    assert profile.model_aliases.generator_main == "generator_main"
    assert profile.answer.verification.citation_validation == "strict"
    assert profile.answer.verification.claim_verification == "off"


def test_verified_profile_enables_claim_verifier_without_changing_base() -> None:
    base = get_retrieval_profile("sota_mvp")
    verified = get_retrieval_profile("sota_mvp_verified")

    assert base.answer.verification.claim_verification == "off"
    assert verified.answer.verification.citation_validation == "strict"
    assert verified.answer.verification.claim_verification == "llm_strict"


def test_profile_rejects_disabling_bm25_and_dense() -> None:
    profile = get_retrieval_profile("test_mock").model_dump()
    profile["retrieval"]["bm25"] = False
    profile["retrieval"]["dense"] = False

    with pytest.raises(ValueError, match="at least one"):
        RetrievalProfile.model_validate(profile)


def test_profile_overrides_are_validated_on_same_shape() -> None:
    profile = get_retrieval_profile(
        "sota_mvp",
        overrides={
            "retrieval": {"bm25": True, "dense": False, "fusion": "none", "rerank": False, "top_k": 7},
            "postprocess": {"parent_expansion": "off", "extended_search": "off"},
        },
    )

    assert profile.retrieval.dense is False
    assert profile.retrieval.fusion == "none"
    assert profile.retrieval.top_k == 7
    assert profile.postprocess.parent_expansion == "off"

    with pytest.raises(ValueError, match="at least one"):
        get_retrieval_profile("sota_mvp", overrides={"retrieval": {"bm25": False, "dense": False}})


def test_legacy_citation_bool_maps_to_typed_policy() -> None:
    profile = get_retrieval_profile(
        "test_mock",
        overrides={"answer": {"deterministic_citation_validation": False}},
    )

    assert profile.answer.verification.citation_validation == "off"
    assert profile.answer.deterministic_citation_validation is False


def test_ablation_profiles_reuse_same_profile_shape() -> None:
    bm25_only = get_retrieval_profile("bm25_only")
    rewrite_off = get_retrieval_profile("rewrite_off")
    parent_off = get_retrieval_profile("parent_expansion_off")

    assert bm25_only.retrieval.bm25 is True
    assert bm25_only.retrieval.dense is False
    assert rewrite_off.retrieval.query_rewrite == "off"
    assert parent_off.postprocess.parent_expansion == "off"


def test_upload_profiles_keep_embedding_dimension_contract() -> None:
    upload_mock = get_retrieval_profile("upload_mock")
    upload_sota = get_retrieval_profile("upload_sota_mvp")

    assert upload_mock.source == "upload"
    assert upload_mock.embedding_dimensions(64) == 64
    assert upload_sota.source == "upload"
    assert upload_sota.model_aliases.embed == "embed_default"
    assert upload_sota.embedding_dimensions(64) == 1024
    assert upload_sota.requires_real_provider is True


def test_deep_research_profile_separates_stage_context_from_normal_search() -> None:
    profile = get_retrieval_profile("sota_mvp")

    planner = profile.deep_research.planner
    verifier = profile.deep_research.verifier
    synthesis = profile.deep_research.synthesis

    assert profile.postprocess.max_context_tokens == 30_000
    assert profile.deep_research.max_episodes == 12
    assert profile.deep_research.max_tool_calls == 12
    assert profile.deep_research.tool_timeout_seconds == 120
    assert planner.model_alias == "generator_fast"
    assert planner.max_context_tokens == 80_000
    assert verifier.model_alias == "generator_main"
    assert verifier.max_context_tokens == 24_000
    assert verifier.max_output_tokens == 2_000
    assert synthesis.model_alias == "generator_main"
    assert synthesis.max_context_tokens == 80_000
