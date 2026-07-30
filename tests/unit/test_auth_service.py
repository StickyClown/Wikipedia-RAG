from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wikipediarag.auth_service import hash_password, hash_secret, local_login_enabled, read_secret_file, verify_password
from wikipediarag.config import Settings


def test_password_hashes_are_argon2id_only() -> None:
    password_hash = hash_password("change-me-before-use")

    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, "change-me-before-use")
    assert not verify_password(password_hash, "wrong-password")
    assert not verify_password("sha256:not-argon2", "change-me-before-use")


def test_secret_hash_does_not_store_raw_token() -> None:
    raw_value = "opaque-session-value"

    token_hash = hash_secret(raw_value)

    assert token_hash != raw_value
    assert len(token_hash) == 64
    assert all(character in "0123456789abcdef" for character in token_hash)


def test_bootstrap_secret_file_is_optional_when_not_mounted(tmp_path: Path) -> None:
    missing = tmp_path / "bootstrap_admin_password"

    assert read_secret_file(missing) is None


@pytest.mark.parametrize(
    ("auth_mode", "enabled"),
    [
        ("local", True),
        ("hybrid", True),
        ("oidc", False),
    ],
)
def test_local_login_mode_gating(auth_mode: Any, enabled: bool) -> None:
    settings = Settings(auth_mode=auth_mode)

    assert local_login_enabled(settings) is enabled
