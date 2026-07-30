from __future__ import annotations

import pytest

from wikipediarag.auth import (
    ActorContext,
    AuthenticationMethod,
    AuthorizationError,
    KnowledgeBaseRole,
    PlatformRole,
    TenantRole,
    effective_knowledge_base_role,
    has_kb_capability,
    require_active_tenant,
    require_kb_role,
)
from wikipediarag.config import Settings


def test_auth_mode_test_is_limited_to_test_environment() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=test"):
        Settings(auth_mode="test", app_env="development")

    settings = Settings(auth_mode="test", app_env="test")

    assert settings.auth_mode == "test"


def test_effective_kb_role_uses_highest_grant_without_deny_rules() -> None:
    role = effective_knowledge_base_role(
        platform_role=PlatformRole.user,
        tenant_role=TenantRole.member,
        direct_user_role=KnowledgeBaseRole.editor,
        local_group_roles=[KnowledgeBaseRole.manager],
        oidc_group_roles=[KnowledgeBaseRole.viewer],
    )

    assert role == KnowledgeBaseRole.manager


def test_platform_admin_and_tenant_admin_have_expected_effective_kb_roles() -> None:
    assert (
        effective_knowledge_base_role(
            platform_role=PlatformRole.platform_admin,
            tenant_role=None,
            direct_user_role=None,
        )
        == KnowledgeBaseRole.owner
    )
    assert (
        effective_knowledge_base_role(
            platform_role=PlatformRole.user,
            tenant_role=TenantRole.tenant_admin,
            direct_user_role=None,
        )
        == KnowledgeBaseRole.manager
    )


def test_kb_capabilities_match_exec_plan_role_table() -> None:
    assert has_kb_capability(KnowledgeBaseRole.viewer, "query")
    assert not has_kb_capability(KnowledgeBaseRole.viewer, "upload")
    assert has_kb_capability(KnowledgeBaseRole.editor, "debug")
    assert has_kb_capability(KnowledgeBaseRole.manager, "grant_editor")
    assert not has_kb_capability(KnowledgeBaseRole.manager, "delete")
    assert has_kb_capability(KnowledgeBaseRole.owner, "delete")


def test_kb_role_requirement_is_ordered() -> None:
    require_kb_role(KnowledgeBaseRole.manager, KnowledgeBaseRole.editor)

    with pytest.raises(AuthorizationError) as exc_info:
        require_kb_role(KnowledgeBaseRole.viewer, KnowledgeBaseRole.editor)

    assert exc_info.value.code == "KNOWLEDGE_BASE_ROLE_REQUIRED"


def test_platform_admin_must_select_active_tenant_for_tenant_scoped_operations() -> None:
    actor = ActorContext(
        user_id="user",
        platform_role=PlatformRole.platform_admin,
        active_tenant_id=None,
        tenant_role=None,
        session_id="session",
        authentication_method=AuthenticationMethod.local,
        request_id="00000000-0000-4000-8000-000000000000",
        trace_id="trace",
    )

    with pytest.raises(AuthorizationError) as exc_info:
        require_active_tenant(actor)

    assert exc_info.value.code == "ACTIVE_TENANT_REQUIRED"
    assert exc_info.value.status_code == 409
