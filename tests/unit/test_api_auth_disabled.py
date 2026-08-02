from __future__ import annotations

import pytest
from starlette.requests import Request

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import AuthenticationMethod, PlatformRole, TenantRole
from wikipediarag.auth_service import AuthenticatedUser, AuthenticationError, auth_disabled_actor
from wikipediarag.config import Settings


class _FakeConnectionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


def _request(*, method: str = "GET", path: str = "/api/v1/knowledge-bases") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


async def test_auth_disabled_load_actor_uses_bootstrap_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(auth_disabled=True)
    user = AuthenticatedUser(
        user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        username="admin",
        display_name="admin",
        platform_role=PlatformRole.platform_admin,
        password_change_required=False,
    )

    async def load_user(conn: object, loaded_settings: Settings) -> AuthenticatedUser:
        assert loaded_settings is settings
        return user

    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "load_bootstrap_admin_user", load_user)

    actor = await api_app._load_actor(_request())

    assert actor is not None
    assert actor.user_id == user.user_id
    assert actor.platform_role == PlatformRole.platform_admin
    assert actor.active_tenant_id == settings.default_tenant_id
    assert actor.tenant_role == TenantRole.tenant_admin
    assert actor.authentication_method == AuthenticationMethod.local


async def test_auth_disabled_skips_csrf_for_mutating_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(auth_disabled=True)

    async def load_user(conn: object, loaded_settings: Settings) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            username="admin",
            display_name="admin",
            platform_role=PlatformRole.platform_admin,
            password_change_required=False,
        )

    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    monkeypatch.setattr(api_app, "connect", lambda: _FakeConnectionContext())
    monkeypatch.setattr(api_app, "load_bootstrap_admin_user", load_user)

    actor = await api_app._require_actor(_request(method="POST"))

    assert actor.platform_role == PlatformRole.platform_admin


async def test_csrf_is_required_when_auth_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(auth_disabled=False)
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    actor = auth_disabled_actor(
        settings,
        user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        request_id="request-id",
        trace_id="trace-id",
    )

    with pytest.raises(AuthenticationError, match="CSRF token is required"):
        await api_app._require_csrf(actor, None)
