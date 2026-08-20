import pytest

from wikipediarag.oidc_service import normalized_oidc_group_ids


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({}, set()),
        ({"realm": {"groups": None}}, set()),
        ({"realm": {"groups": 7}}, set()),
        ({"realm": {"groups": " engineering "}}, {"engineering"}),
        ({"realm": {"groups": [" engineering ", "", None, "engineering", "sales"]}}, {"engineering", "sales"}),
    ],
)
def test_oidc_group_claim_normalization_is_strict_and_content_free(
    claims: dict[str, object], expected: set[str]
) -> None:
    assert normalized_oidc_group_ids(claims, "realm.groups") == expected
