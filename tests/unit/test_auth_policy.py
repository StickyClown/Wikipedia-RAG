from __future__ import annotations

import pytest

from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole
from wikipediarag.config import Settings


def test_auth_mode_test_is_limited_to_test_environment() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=test"):
        Settings(auth_mode="test", app_env="development")

    assert Settings(auth_mode="test", app_env="test").auth_mode == "test"


def test_actor_context_contains_only_workspace_identity_and_request_fields() -> None:
    actor = ActorContext(
        user_id="user",
        platform_role=PlatformRole.platform_admin,
        session_id="session",
        authentication_method=AuthenticationMethod.local,
        request_id="00000000-0000-4000-8000-000000000000",
        trace_id="trace",
    )

    assert actor.user_id == "user"
    assert not hasattr(actor, "active_tenant_id")
    assert not hasattr(actor, "tenant_role")


def test_actor_context_rejects_legacy_tenant_authority_inputs() -> None:
    with pytest.raises(TypeError):
        ActorContext(
            user_id="user",
            platform_role=PlatformRole.user,
            session_id="session",
            authentication_method=AuthenticationMethod.local,
            request_id="00000000-0000-4000-8000-000000000000",
            trace_id="trace",
            active_tenant_id="legacy",  # type: ignore[call-arg]
        )
