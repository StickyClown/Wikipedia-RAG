from __future__ import annotations

import pytest

from wikipediarag.config import Settings
from wikipediarag.workspace_reset import WorkspaceResetSafetyError, apply_workspace_reset


class _Result:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows

    def scalar_one(self) -> object:
        return self.rows[0]


class _Connection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, statement: object) -> _Result:
        self.calls.append(str(statement))
        return _Result([])


async def test_reset_is_disabled_before_any_destructive_statement() -> None:
    conn = _Connection()

    with pytest.raises(WorkspaceResetSafetyError, match="WORKSPACE_RESET_DISABLED"):
        await apply_workspace_reset(conn, Settings(workspace_reset_enabled=False))  # type: ignore[arg-type]

    assert conn.calls == []


async def test_reset_refuses_an_unverified_shared_public_schema() -> None:
    conn = _Connection()

    with pytest.raises(WorkspaceResetSafetyError, match="WORKSPACE_RESET_TARGET_UNVERIFIED"):
        await apply_workspace_reset(conn, Settings(workspace_reset_enabled=True))  # type: ignore[arg-type]

    assert len(conn.calls) == 1
    assert "pg_tables" in conn.calls[0]
