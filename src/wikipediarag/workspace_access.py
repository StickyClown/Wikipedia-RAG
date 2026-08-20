"""Workspace resource-grant policy.

This module intentionally has no HTTP or SQL dependencies.  Storage adapters
load the current ownership, grants and memberships, then use this pure boundary
for every decision.  Keeping the composition rule here makes revocation
effective immediately once the adapter reads PostgreSQL again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class PlatformRole(StrEnum):
    user = "USER"
    platform_admin = "PLATFORM_ADMIN"


class ResourceType(StrEnum):
    knowledge_base = "KNOWLEDGE_BASE"
    document = "DOCUMENT"


class PrincipalType(StrEnum):
    user = "USER"
    group = "GROUP"


class ResourcePermission(StrEnum):
    read = "READ"
    write = "WRITE"


@dataclass(frozen=True, slots=True)
class AccessGrant:
    principal_type: PrincipalType
    principal_id: str
    permission: ResourcePermission
    id: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceAccess:
    """Current, server-derived resource state needed for a decision."""

    resource_type: ResourceType
    resource_id: str
    owner_user_id: str
    grants: tuple[AccessGrant, ...] = ()
    inherits_kb_access: bool = True
    knowledge_base: ResourceAccess | None = None


@dataclass(frozen=True, slots=True)
class AccessDecision:
    read: bool
    write: bool
    share: bool
    delete: bool
    access_scope: str | None = None


def normalize_grants(grants: Iterable[AccessGrant]) -> tuple[AccessGrant, ...]:
    """Deduplicate a replacement payload without weakening a WRITE grant."""
    seen: set[tuple[PrincipalType, str, ResourcePermission]] = set()
    normalized: list[AccessGrant] = []
    for grant in grants:
        principal_id = grant.principal_id.strip()
        if not principal_id:
            raise ValueError("principal_id must not be empty")
        key = (grant.principal_type, principal_id, grant.permission)
        if key not in seen:
            seen.add(key)
            normalized.append(AccessGrant(grant.principal_type, principal_id, grant.permission, grant.id))
    return tuple(normalized)


def resolve_access(
    *,
    user_id: str,
    platform_role: PlatformRole,
    group_ids: Iterable[str],
    resource: ResourceAccess,
) -> AccessDecision:
    """Resolve additive grants for a KB or document.

    Document inheritance deliberately includes the KB owner and grants only
    when enabled.  A KB owner may administer every document in that KB, per
    the product contract, even when the document is private.
    """
    if platform_role == PlatformRole.platform_admin:
        return AccessDecision(True, True, True, True, "full")

    groups = frozenset(group_ids)
    direct_read, direct_write = _principal_permissions(user_id, groups, resource.grants)
    is_owner = resource.owner_user_id == user_id

    kb_owner = False
    inherited_read = inherited_write = False
    if resource.resource_type == ResourceType.document and resource.knowledge_base is not None:
        kb = resource.knowledge_base
        kb_owner = kb.owner_user_id == user_id
        if resource.inherits_kb_access:
            inherited_read, inherited_write = _principal_permissions(user_id, groups, kb.grants)
            inherited_read = inherited_read or kb_owner
            inherited_write = inherited_write or kb_owner

    read = is_owner or direct_read or inherited_read or kb_owner
    write = is_owner or direct_write or inherited_write or kb_owner
    privileged = is_owner or kb_owner
    return AccessDecision(read, write, privileged, privileged, "full" if read else None)


def partial_kb_shell(
    *,
    kb: ResourceAccess,
    documents: Iterable[ResourceAccess],
    user_id: str,
    platform_role: PlatformRole,
    group_ids: Iterable[str],
) -> AccessDecision:
    """Return a minimal KB listing capability when only a child is readable."""
    direct = resolve_access(user_id=user_id, platform_role=platform_role, group_ids=group_ids, resource=kb)
    if direct.read:
        return direct
    if any(
        resolve_access(user_id=user_id, platform_role=platform_role, group_ids=group_ids, resource=document).read
        for document in documents
    ):
        return AccessDecision(True, False, False, False, "partial")
    return AccessDecision(False, False, False, False)


def _principal_permissions(user_id: str, group_ids: frozenset[str], grants: Iterable[AccessGrant]) -> tuple[bool, bool]:
    read = write = False
    for grant in grants:
        match = (grant.principal_type == PrincipalType.user and grant.principal_id == user_id) or (
            grant.principal_type == PrincipalType.group and grant.principal_id in group_ids
        )
        if not match:
            continue
        if grant.permission == ResourcePermission.write:
            write = True
            read = True
        elif grant.permission == ResourcePermission.read:
            read = True
    return read, write
