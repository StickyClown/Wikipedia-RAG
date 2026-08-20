"""PostgreSQL repository for workspace resource grants.

All callers must load current memberships through this repository rather than
putting group authority in a session.  The methods deliberately return only
the typed policy inputs consumed by :mod:`workspace_access`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

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


class InvalidGrantError(ValueError):
    """Safe validation failure raised before any replacement mutation."""


class WorkspaceGrantRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def group_ids_for_user(self, user_id: str) -> frozenset[str]:
        result = await self._conn.execute(
            text("SELECT group_id FROM group_memberships WHERE user_id = :user_id"), {"user_id": user_id}
        )
        return frozenset(str(row["group_id"]) for row in result.mappings())

    async def cache_access_marker(self, user_id: str) -> str:
        """Return the current, non-sensitive cache partition for one actor."""
        groups = await self.group_ids_for_user(user_id)
        revision = await self._conn.execute(text("SELECT revision FROM workspace_authorization_state WHERE id = true"))
        return "|".join([user_id, *sorted(groups), str(revision.scalar_one_or_none() or 0)])

    async def load_grants(self, resource_type: ResourceType, resource_id: str) -> tuple[AccessGrant, ...]:
        result = await self._conn.execute(
            text(
                """
                SELECT id, principal_type, principal_id, permission
                FROM resource_grants
                WHERE resource_type = :resource_type AND resource_id = :resource_id
                ORDER BY created_at, id
                """
            ),
            {"resource_type": resource_type.value, "resource_id": resource_id},
        )
        return tuple(
            AccessGrant(
                PrincipalType(str(row["principal_type"])),
                str(row["principal_id"]),
                ResourcePermission(str(row["permission"])),
                str(row["id"]),
            )
            for row in result.mappings()
        )

    async def load_knowledge_base(self, kb_id: str) -> ResourceAccess | None:
        result = await self._conn.execute(
            text("SELECT id, owner_user_id FROM knowledge_bases WHERE id = :id"), {"id": kb_id}
        )
        row = result.mappings().first()
        if row is None:
            return None
        return ResourceAccess(
            ResourceType.knowledge_base,
            str(row["id"]),
            str(row["owner_user_id"]),
            await self.load_grants(ResourceType.knowledge_base, str(row["id"])),
        )

    async def load_document(self, document_id: str) -> ResourceAccess | None:
        result = await self._conn.execute(
            text(
                """
                SELECT id, knowledge_base_id, owner_user_id, inherits_kb_access
                FROM documents WHERE id = :id
                """
            ),
            {"id": document_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        kb = await self.load_knowledge_base(str(row["knowledge_base_id"]))
        if kb is None:
            return None
        return ResourceAccess(
            ResourceType.document,
            str(row["id"]),
            str(row["owner_user_id"]),
            await self.load_grants(ResourceType.document, str(row["id"])),
            bool(row["inherits_kb_access"]),
            kb,
        )

    async def list_visible_knowledge_bases(
        self, *, user_id: str, platform_role: PlatformRole
    ) -> list[tuple[ResourceAccess, str, bool, bool]]:
        """List full KBs and safe shells for directly readable documents."""
        group_ids = await self.group_ids_for_user(user_id)
        result = await self._conn.execute(text("SELECT id, name, owner_user_id FROM knowledge_bases ORDER BY name, id"))
        kb_rows: list[dict[str, object]] = [dict(row) for row in result.mappings()]
        grants_result = await self._conn.execute(
            text(
                """
                SELECT resource_id, id, principal_type, principal_id, permission
                FROM resource_grants WHERE resource_type = 'KNOWLEDGE_BASE'
                ORDER BY created_at, id
                """
            )
        )
        grants_by_kb: dict[str, list[AccessGrant]] = {}
        for row in grants_result.mappings():
            grants_by_kb.setdefault(str(row["resource_id"]), []).append(
                AccessGrant(
                    PrincipalType(str(row["principal_type"])),
                    str(row["principal_id"]),
                    ResourcePermission(str(row["permission"])),
                    str(row["id"]),
                )
            )
        document_result = await self._conn.execute(
            text(
                """
                SELECT document.id, document.knowledge_base_id, document.owner_user_id,
                       document.inherits_kb_access, grant.id AS grant_id,
                       grant.principal_type, grant.principal_id, grant.permission
                FROM documents document
                LEFT JOIN resource_grants grant
                  ON grant.resource_type = 'DOCUMENT' AND grant.resource_id = document.id
                WHERE document.lifecycle_state = 'active'
                """
            )
        )
        documents_by_kb: dict[str, list[ResourceAccess]] = {}
        pending_documents: dict[str, tuple[str, str, bool, list[AccessGrant]]] = {}
        for row in document_result.mappings():
            document_id = str(row["id"])
            current = pending_documents.setdefault(
                document_id,
                (str(row["knowledge_base_id"]), str(row["owner_user_id"]), bool(row["inherits_kb_access"]), []),
            )
            if row["grant_id"] is not None:
                current[3].append(
                    AccessGrant(
                        PrincipalType(str(row["principal_type"])),
                        str(row["principal_id"]),
                        ResourcePermission(str(row["permission"])),
                        str(row["grant_id"]),
                    )
                )
        for document_id, (kb_id, owner_id, inherit, grants) in pending_documents.items():
            documents_by_kb.setdefault(kb_id, []).append(
                ResourceAccess(ResourceType.document, document_id, owner_id, tuple(grants), inherit)
            )
        visible: list[tuple[ResourceAccess, str, bool, bool]] = []
        for kb_row in kb_rows:
            kb_id = str(kb_row["id"])
            kb = ResourceAccess(
                ResourceType.knowledge_base,
                kb_id,
                str(kb_row["owner_user_id"]),
                tuple(grants_by_kb.get(kb_id, [])),
            )
            documents = [
                ResourceAccess(
                    document.resource_type,
                    document.resource_id,
                    document.owner_user_id,
                    document.grants,
                    document.inherits_kb_access,
                    kb,
                )
                for document in documents_by_kb.get(kb_id, [])
            ]
            decision = partial_kb_shell(
                kb=kb,
                documents=documents,
                user_id=user_id,
                platform_role=platform_role,
                group_ids=group_ids,
            )
            if decision.read:
                visible.append((kb, decision.access_scope or "full", decision.write, decision.share))
        return visible

    async def authorize(
        self,
        *,
        user_id: str,
        platform_role: PlatformRole,
        resource: ResourceAccess,
    ) -> tuple[bool, bool, bool, bool]:
        decision = resolve_access(
            user_id=user_id,
            platform_role=platform_role,
            group_ids=await self.group_ids_for_user(user_id),
            resource=resource,
        )
        return decision.read, decision.write, decision.share, decision.delete

    async def authorized_document_ids(
        self,
        *,
        user_id: str,
        platform_role: PlatformRole,
        document_ids: Iterable[str],
    ) -> frozenset[str]:
        """Batch-confirm active document candidates from an untrusted index/cache."""
        requested = sorted({document_id for document_id in document_ids if document_id})
        if not requested:
            return frozenset()
        group_ids = await self.group_ids_for_user(user_id)
        document_result = await self._conn.execute(
            text(
                """
                SELECT id, knowledge_base_id, owner_user_id, inherits_kb_access
                FROM documents WHERE id = ANY(:ids) AND lifecycle_state = 'active'
                """
            ),
            {"ids": requested},
        )
        document_rows = [dict(row) for row in document_result.mappings()]
        kb_ids = sorted({str(row["knowledge_base_id"]) for row in document_rows})
        if not kb_ids:
            return frozenset()
        kb_result = await self._conn.execute(
            text("SELECT id, owner_user_id FROM knowledge_bases WHERE id = ANY(:ids)"), {"ids": kb_ids}
        )
        kb_rows = {str(row["id"]): dict(row) for row in kb_result.mappings()}
        grant_result = await self._conn.execute(
            text(
                """
                SELECT resource_type, resource_id, id, principal_type, principal_id, permission
                FROM resource_grants
                WHERE (resource_type = 'DOCUMENT' AND resource_id = ANY(:document_ids))
                   OR (resource_type = 'KNOWLEDGE_BASE' AND resource_id = ANY(:kb_ids))
                ORDER BY created_at, id
                """
            ),
            {"document_ids": requested, "kb_ids": kb_ids},
        )
        grants: dict[tuple[str, str], list[AccessGrant]] = {}
        for row in grant_result.mappings():
            key = (str(row["resource_type"]), str(row["resource_id"]))
            grants.setdefault(key, []).append(
                AccessGrant(
                    PrincipalType(str(row["principal_type"])),
                    str(row["principal_id"]),
                    ResourcePermission(str(row["permission"])),
                    str(row["id"]),
                )
            )
        allowed: set[str] = set()
        for document_row in document_rows:
            kb_id = str(document_row["knowledge_base_id"])
            kb_row = kb_rows.get(kb_id)
            if kb_row is None:
                continue
            kb = ResourceAccess(
                ResourceType.knowledge_base,
                kb_id,
                str(kb_row["owner_user_id"]),
                tuple(grants.get((ResourceType.knowledge_base.value, kb_id), [])),
            )
            document_id = str(document_row["id"])
            document = ResourceAccess(
                ResourceType.document,
                document_id,
                str(document_row["owner_user_id"]),
                tuple(grants.get((ResourceType.document.value, document_id), [])),
                bool(document_row["inherits_kb_access"]),
                kb,
            )
            if resolve_access(
                user_id=user_id, platform_role=platform_role, group_ids=group_ids, resource=document
            ).read:
                allowed.add(document_id)
        return frozenset(allowed)

    async def replace_grants(
        self,
        *,
        resource_type: ResourceType,
        resource_id: str,
        grants: Iterable[AccessGrant],
    ) -> tuple[AccessGrant, ...]:
        """Validate all principals, then atomically replace the complete set."""
        normalized = normalize_grants(grants)
        await self._validate_principals(normalized)
        await self._conn.execute(
            text("DELETE FROM resource_grants WHERE resource_type = :type AND resource_id = :id"),
            {"type": resource_type.value, "id": resource_id},
        )
        for grant in normalized:
            await self._conn.execute(
                text(
                    """
                    INSERT INTO resource_grants(
                      id, resource_type, resource_id, principal_type, principal_id, permission
                    )
                    VALUES (:id, :resource_type, :resource_id, :principal_type, :principal_id, :permission)
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "resource_type": resource_type.value,
                    "resource_id": resource_id,
                    "principal_type": grant.principal_type.value,
                    "principal_id": grant.principal_id,
                    "permission": grant.permission.value,
                },
            )
        await self._conn.execute(
            text("UPDATE workspace_authorization_state SET revision = revision + 1 WHERE id = true")
        )
        return await self.load_grants(resource_type, resource_id)

    async def _validate_principals(self, grants: Iterable[AccessGrant]) -> None:
        user_ids = sorted({grant.principal_id for grant in grants if grant.principal_type == PrincipalType.user})
        group_ids = sorted({grant.principal_id for grant in grants if grant.principal_type == PrincipalType.group})
        for principal_type, ids, table in (
            (PrincipalType.user, user_ids, "users"),
            (PrincipalType.group, group_ids, "groups"),
        ):
            if not ids:
                continue
            result = await self._conn.execute(
                text(f"SELECT id::text AS id FROM {table} WHERE id::text = ANY(:ids)"),  # noqa: S608
                {"ids": ids},
            )
            found = {str(row["id"]) for row in result.mappings()}
            if found != set(ids):
                raise InvalidGrantError(f"UNKNOWN_{principal_type.value}_PRINCIPAL")
