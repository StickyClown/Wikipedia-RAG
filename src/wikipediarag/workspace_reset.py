"""Explicit clean-slate boundary for the workspace-only deployment.

This is deliberately outside normal bootstrap.  It can delete every
WikipediaRag-owned row, object, cache entry and derived search index, so an
operator must opt in both in configuration and on the command line.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from redis import asyncio as redis_async
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.config import Settings
from wikipediarag.db import SCHEMA_SQL
from wikipediarag.search_index import get_client
from wikipediarag.storage import delete_all_objects


class WorkspaceResetSafetyError(RuntimeError):
    """A content-free refusal to perform a destructive reset."""


@dataclass(frozen=True, slots=True)
class WorkspaceResetReport:
    database_tables: dict[str, int]
    search_indices: int

    def public_report(self) -> dict[str, Any]:
        return {
            "database_table_counts": self.database_tables,
            "database": "configured_wikipediarag_public_schema",
            "search_index_count": self.search_indices,
            "object_store": "configured_bucket",
            "cache": "configured_redis_database",
        }


async def preflight_workspace_reset(conn: AsyncConnection, settings: Settings) -> WorkspaceResetReport:
    """Return bounded inventory without exposing row content, keys or URLs."""
    tables = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))
    counts: dict[str, int] = {}
    for (table,) in tables.all():
        name = str(table)
        result = await conn.execute(text(f'SELECT count(*) FROM "{name.replace(chr(34), chr(34) * 2)}"'))  # noqa: S608
        counts[name] = int(result.scalar_one())
    indices = await asyncio.to_thread(_workspace_index_count, settings)
    return WorkspaceResetReport(database_tables=counts, search_indices=indices)


async def apply_workspace_reset(conn: AsyncConnection, settings: Settings) -> WorkspaceResetReport:
    """Delete only configured WikipediaRag stores and recreate the schema."""
    if not settings.workspace_reset_enabled:
        raise WorkspaceResetSafetyError("WORKSPACE_RESET_DISABLED")
    await _require_dedicated_workspace_schema(conn)
    report = await preflight_workspace_reset(conn, settings)
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('wikipediarag_workspace_reset_v1'))"))
    await conn.execute(text("DROP SCHEMA public CASCADE"))
    await conn.execute(text("CREATE SCHEMA public"))
    await asyncio.to_thread(delete_all_objects, settings)
    await _flush_configured_redis(settings)
    await asyncio.to_thread(_delete_workspace_indices, settings)
    return report


async def _require_dedicated_workspace_schema(conn: AsyncConnection) -> None:
    """Refuse to drop ``public`` when it contains another application's table."""
    result = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    actual = {str(row[0]) for row in result.all()}
    known = set(re.findall(r"CREATE TABLE IF NOT EXISTS ([a-z_]+)", SCHEMA_SQL))
    known.update({"resource_grants", "workspace_authorization_state"})
    if not actual or not actual.issubset(known):
        raise WorkspaceResetSafetyError("WORKSPACE_RESET_TARGET_UNVERIFIED")


async def _flush_configured_redis(settings: Settings) -> None:
    client = redis_async.from_url(settings.redis_url, decode_responses=True, max_connections=1)
    try:
        await client.flushdb()
    finally:
        await client.aclose()


def _workspace_index_count(settings: Settings) -> int:
    aliases = get_client(settings).indices.get_alias("wiki-chunks-*")
    return len(aliases)


def _delete_workspace_indices(settings: Settings) -> None:
    client = get_client(settings)
    aliases = client.indices.get_alias("wiki-chunks-*")
    indices = sorted(str(name) for name in aliases if str(name).startswith("wiki-chunks-"))
    if indices:
        client.indices.delete(index=",".join(indices), ignore=[404])
