from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.auth import PlatformRole, TenantRole
from wikipediarag.auth_service import (
    auth_disabled_actor,
    bootstrap_admin_password,
    ensure_bootstrap_admin,
    hash_password,
    hash_secret,
    local_login_enabled,
    read_secret_file,
    verify_password,
)
from wikipediarag.config import Settings


class _FakeResult:
    def __init__(self, mapping: dict[str, Any] | None = None) -> None:
        self._mapping = mapping

    def mappings(self) -> _FakeResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._mapping


class _FakeBootstrapConnection:
    def __init__(self, existing_user: dict[str, Any] | None = None) -> None:
        self.existing_user = existing_user
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement)
        resolved_params = params or {}
        self.executed.append((sql, resolved_params))
        if "SELECT id, password_hash" in sql:
            return _FakeResult(self.existing_user)
        return _FakeResult()


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


def test_bootstrap_admin_password_defaults_to_admin_when_file_is_missing(tmp_path: Path) -> None:
    settings = Settings(bootstrap_admin_password_file=tmp_path / "missing")

    assert bootstrap_admin_password(settings) == "admin"


def test_bootstrap_admin_password_prefers_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "bootstrap_admin_password"
    secret_file.write_text("from-file\n", encoding="utf-8")
    settings = Settings(bootstrap_admin_password="admin", bootstrap_admin_password_file=secret_file)  # noqa: S106

    assert bootstrap_admin_password(settings) == "from-file"


async def test_bootstrap_admin_is_created_with_default_admin_password(tmp_path: Path) -> None:
    settings = Settings(auth_mode="local", bootstrap_admin_password_file=tmp_path / "missing")
    conn = _FakeBootstrapConnection()

    changed = await ensure_bootstrap_admin(cast(AsyncConnection, conn), settings)

    assert changed
    inserted = [params for sql, params in conn.executed if "INSERT INTO users" in sql][0]
    assert inserted["username"] == "admin"
    assert verify_password(str(inserted["password_hash"]), "admin")
    insert_sql = [sql for sql, _ in conn.executed if "INSERT INTO users" in sql][0]
    assert ":password_hash, false, false" in insert_sql


async def test_bootstrap_admin_resets_existing_bootstrap_user_to_default_admin(tmp_path: Path) -> None:
    existing_user_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    settings = Settings(auth_mode="local", bootstrap_admin_password_file=tmp_path / "missing")
    conn = _FakeBootstrapConnection(
        existing_user={"id": existing_user_id, "password_hash": hash_password("old-password")}
    )

    changed = await ensure_bootstrap_admin(cast(AsyncConnection, conn), settings)

    assert changed
    updates = [(sql, params) for sql, params in conn.executed if "UPDATE users" in sql]
    assert len(updates) == 1
    update_sql, update_params = updates[0]
    assert update_params["id"] == existing_user_id
    assert verify_password(str(update_params["password_hash"]), "admin")
    assert "platform_role = 'PLATFORM_ADMIN'" in update_sql
    assert "password_change_required = false" in update_sql
    assert "is_disabled = false" in update_sql


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


def test_auth_disabled_actor_is_platform_admin_in_default_tenant() -> None:
    settings = Settings(auth_disabled=True)

    actor = auth_disabled_actor(settings, user_id="user-id", request_id="request-id", trace_id="trace-id")

    assert actor.user_id == "user-id"
    assert actor.platform_role == PlatformRole.platform_admin
    assert actor.active_tenant_id == settings.default_tenant_id
    assert actor.tenant_role == TenantRole.tenant_admin
    assert str(uuid.UUID(actor.session_id)) == actor.session_id


def test_auth_disabled_actor_session_id_is_stable_uuid_per_user() -> None:
    settings = Settings(auth_disabled=True)

    first = auth_disabled_actor(settings, user_id="user-id", request_id="request-1", trace_id="trace-1")
    second = auth_disabled_actor(settings, user_id="user-id", request_id="request-2", trace_id="trace-2")
    other = auth_disabled_actor(settings, user_id="other-user", request_id="request-3", trace_id="trace-3")

    assert first.session_id == second.session_id
    assert first.session_id != other.session_id
