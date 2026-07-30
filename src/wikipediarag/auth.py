from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class PlatformRole(StrEnum):
    user = "USER"
    platform_admin = "PLATFORM_ADMIN"


class TenantRole(StrEnum):
    member = "MEMBER"
    tenant_admin = "TENANT_ADMIN"


class KnowledgeBaseRole(StrEnum):
    viewer = "VIEWER"
    editor = "EDITOR"
    manager = "MANAGER"
    owner = "OWNER"


class GroupType(StrEnum):
    local = "LOCAL"
    oidc = "OIDC"


class GrantSubjectType(StrEnum):
    user = "USER"
    group = "GROUP"


class AuthenticationMethod(StrEnum):
    local = "local"
    oidc = "oidc"
    test = "test"


@dataclass(frozen=True, slots=True)
class ActorContext:
    user_id: str
    platform_role: PlatformRole
    active_tenant_id: str | None
    tenant_role: TenantRole | None
    session_id: str
    authentication_method: AuthenticationMethod
    request_id: str
    trace_id: str


class AuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


KB_ROLE_RANK: dict[KnowledgeBaseRole, int] = {
    KnowledgeBaseRole.viewer: 10,
    KnowledgeBaseRole.editor: 20,
    KnowledgeBaseRole.manager: 30,
    KnowledgeBaseRole.owner: 40,
}

KB_ROLE_CAPABILITIES: dict[KnowledgeBaseRole, frozenset[str]] = {
    KnowledgeBaseRole.viewer: frozenset({"query", "citations", "metadata"}),
    KnowledgeBaseRole.editor: frozenset({"query", "citations", "metadata", "upload", "reprocess", "debug"}),
    KnowledgeBaseRole.manager: frozenset(
        {
            "query",
            "citations",
            "metadata",
            "upload",
            "reprocess",
            "debug",
            "source_config",
            "grant_viewer",
            "grant_editor",
        }
    ),
    KnowledgeBaseRole.owner: frozenset(
        {
            "query",
            "citations",
            "metadata",
            "upload",
            "reprocess",
            "debug",
            "source_config",
            "grant_viewer",
            "grant_editor",
            "grant_manager",
            "ownership",
            "delete",
        }
    ),
}


def require_active_tenant(actor: ActorContext) -> str:
    if actor.active_tenant_id is None:
        raise AuthorizationError(
            "ACTIVE_TENANT_REQUIRED",
            "select an active tenant before tenant-scoped operations",
            status_code=409,
        )
    return actor.active_tenant_id


def can_manage_tenant(actor: ActorContext) -> bool:
    return actor.platform_role == PlatformRole.platform_admin or actor.tenant_role == TenantRole.tenant_admin


def require_tenant_admin(actor: ActorContext) -> None:
    require_active_tenant(actor)
    if not can_manage_tenant(actor):
        raise AuthorizationError("TENANT_ADMIN_REQUIRED", "tenant administrator access is required")


def effective_knowledge_base_role(
    *,
    platform_role: PlatformRole,
    tenant_role: TenantRole | None,
    direct_user_role: KnowledgeBaseRole | None = None,
    local_group_roles: Iterable[KnowledgeBaseRole] = (),
    oidc_group_roles: Iterable[KnowledgeBaseRole] = (),
) -> KnowledgeBaseRole | None:
    if platform_role == PlatformRole.platform_admin:
        return KnowledgeBaseRole.owner

    candidates: list[KnowledgeBaseRole] = []
    if tenant_role == TenantRole.tenant_admin:
        candidates.append(KnowledgeBaseRole.manager)
    if direct_user_role is not None:
        candidates.append(direct_user_role)
    candidates.extend(local_group_roles)
    candidates.extend(oidc_group_roles)
    if not candidates:
        return None
    return max(candidates, key=lambda role: KB_ROLE_RANK[role])


def has_kb_role(actual: KnowledgeBaseRole | None, required: KnowledgeBaseRole) -> bool:
    return actual is not None and KB_ROLE_RANK[actual] >= KB_ROLE_RANK[required]


def require_kb_role(actual: KnowledgeBaseRole | None, required: KnowledgeBaseRole) -> None:
    if not has_kb_role(actual, required):
        raise AuthorizationError(
            "KNOWLEDGE_BASE_ROLE_REQUIRED",
            f"{required.value} knowledge-base role is required",
        )


def has_kb_capability(actual: KnowledgeBaseRole | None, capability: str) -> bool:
    return actual is not None and capability in KB_ROLE_CAPABILITIES[actual]


def require_kb_capability(actual: KnowledgeBaseRole | None, capability: str) -> None:
    if not has_kb_capability(actual, capability):
        raise AuthorizationError(
            "KNOWLEDGE_BASE_CAPABILITY_REQUIRED",
            f"{capability} knowledge-base capability is required",
        )
