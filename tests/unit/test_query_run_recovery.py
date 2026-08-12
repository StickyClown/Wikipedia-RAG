from __future__ import annotations

from typing import Any, cast

import pytest

from wikipediarag.repository import recover_stale_chat_query_runs


class _Result:
    rowcount = 3


class _Connection:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: dict[str, Any] = {}

    async def execute(self, statement: Any, parameters: dict[str, Any]) -> _Result:
        self.statement = str(statement)
        self.parameters = parameters
        return _Result()


@pytest.mark.asyncio
async def test_stale_chat_recovery_only_targets_active_normal_or_extended_runs() -> None:
    connection = _Connection()

    recovered = await recover_stale_chat_query_runs(cast(Any, connection), max_age_seconds=75)

    assert recovered == 3
    assert "status IN ('received', 'running')" in connection.statement
    assert "mode IN ('normal', 'extended')" in connection.statement
    assert "STALE_QUERY_RUN_RECOVERED" in connection.statement
    assert connection.parameters == {"max_age_seconds": 75}
