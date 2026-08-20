from wikipediarag.workspace_access import (
    AccessGrant,
    PlatformRole,
    PrincipalType,
    ResourceAccess,
    ResourcePermission,
    ResourceType,
    normalize_grants,
    partial_kb_shell,
    resolve_access,
)


def kb(*grants: AccessGrant, owner: str = "owner") -> ResourceAccess:
    return ResourceAccess(ResourceType.knowledge_base, "kb", owner, grants)


def document(*grants: AccessGrant, owner: str = "document-owner", inherit: bool = True) -> ResourceAccess:
    return ResourceAccess(ResourceType.document, "doc", owner, grants, inherit, kb())


def test_admin_owner_and_write_grants_have_expected_capabilities() -> None:
    assert resolve_access(user_id="any", platform_role=PlatformRole.platform_admin, group_ids=[], resource=kb()).delete
    assert resolve_access(user_id="owner", platform_role=PlatformRole.user, group_ids=[], resource=kb()).share
    decision = resolve_access(
        user_id="writer",
        platform_role=PlatformRole.user,
        group_ids=[],
        resource=kb(AccessGrant(PrincipalType.user, "writer", ResourcePermission.write)),
    )
    assert decision.read and decision.write and not decision.share


def test_document_inheritance_private_and_direct_group_access() -> None:
    parent = kb(AccessGrant(PrincipalType.group, "readers", ResourcePermission.read))
    inherited = ResourceAccess(ResourceType.document, "doc", "document-owner", (), True, parent)
    private = ResourceAccess(ResourceType.document, "doc", "document-owner", (), False, parent)
    shared = ResourceAccess(
        ResourceType.document,
        "doc",
        "document-owner",
        (AccessGrant(PrincipalType.group, "readers", ResourcePermission.write),),
        False,
        parent,
    )
    assert resolve_access(user_id="u", platform_role=PlatformRole.user, group_ids=["readers"], resource=inherited).read
    assert not resolve_access(
        user_id="u", platform_role=PlatformRole.user, group_ids=["readers"], resource=private
    ).read
    assert resolve_access(user_id="u", platform_role=PlatformRole.user, group_ids=["readers"], resource=shared).write


def test_kb_owner_manages_private_document_and_partial_shell_is_safe() -> None:
    parent = kb(owner="kb-owner")
    private = ResourceAccess(ResourceType.document, "doc", "doc-owner", (), False, parent)
    assert resolve_access(user_id="kb-owner", platform_role=PlatformRole.user, group_ids=[], resource=private).delete
    shared = ResourceAccess(
        ResourceType.document,
        "shared",
        "doc-owner",
        (AccessGrant(PrincipalType.user, "reader", ResourcePermission.read),),
        False,
        parent,
    )
    shell = partial_kb_shell(
        kb=parent, documents=[shared], user_id="reader", platform_role=PlatformRole.user, group_ids=[]
    )
    assert shell.read and shell.access_scope == "partial" and not shell.write and not shell.share


def test_grants_are_additive_and_normalized() -> None:
    grants = normalize_grants(
        [
            AccessGrant(PrincipalType.user, "u", ResourcePermission.read),
            AccessGrant(PrincipalType.user, "u", ResourcePermission.read),
            AccessGrant(PrincipalType.user, "u", ResourcePermission.write),
        ]
    )
    assert len(grants) == 2
    decision = resolve_access(user_id="u", platform_role=PlatformRole.user, group_ids=[], resource=kb(*grants))
    assert decision.read and decision.write
