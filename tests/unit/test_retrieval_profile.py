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


def test_ablation_profiles_reuse_same_profile_shape() -> None:
    bm25_only = get_retrieval_profile("bm25_only")
    rewrite_off = get_retrieval_profile("rewrite_off")
    parent_off = get_retrieval_profile("parent_expansion_off")

    assert bm25_only.retrieval.bm25 is True
    assert bm25_only.retrieval.dense is False
    assert rewrite_off.retrieval.query_rewrite == "off"
    assert parent_off.postprocess.parent_expansion == "off"
