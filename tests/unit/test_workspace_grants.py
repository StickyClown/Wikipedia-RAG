from __future__ import annotations

from typing import Any, cast

import pytest

from wikipediarag.workspace_access import AccessGrant, PrincipalType, ResourcePermission, ResourceType
from wikipediarag.workspace_grants import InvalidGrantError, WorkspaceGrantRepository


class _Result:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def mappings(self) -> _Result:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class _Connection:
    def __init__(self, *, known: set[str]) -> None:
        self.known = known
        self.calls: list[str] = []

    async def execute(self, statement: object, params: object = None) -> _Result:
        sql = str(statement)
        self.calls.append(sql)
        if "SELECT id::text AS id" in sql:
            ids = cast(dict[str, Any], params or {}).get("ids", [])
            return _Result([{"id": item} for item in ids if item in self.known])
        return _Result([])


async def test_invalid_replacement_does_not_delete_existing_grants() -> None:
    conn = _Connection(known={"known-user"})
    repository = WorkspaceGrantRepository(conn)  # type: ignore[arg-type]

    with pytest.raises(InvalidGrantError, match="UNKNOWN_USER_PRINCIPAL"):
        await repository.replace_grants(
            resource_type=ResourceType.document,
            resource_id="document",
            grants=[AccessGrant(PrincipalType.user, "missing-user", ResourcePermission.read)],
        )

    assert not any("DELETE FROM resource_grants" in call for call in conn.calls)
