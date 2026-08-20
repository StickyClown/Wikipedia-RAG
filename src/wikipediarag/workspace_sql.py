"""Temporary SQL compatibility normalizer for the clean workspace cutover.

The data stores are reset instead of migrated.  This adapter lets durable job,
ingestion and research call sites be converted incrementally: their obsolete
storage partition argument is ignored at the SQL boundary, never persisted or
used as an authorization predicate.  New queries must be written without a
tenant field; this module is removed once the remaining call signatures are
cleaned up.
"""

from __future__ import annotations

import re

from sqlalchemy import text as sqlalchemy_text
from sqlalchemy.sql.elements import TextClause


def text(statement: str) -> TextClause:
    """Compile legacy tenant-shaped SQL against a workspace-only schema."""
    statement = re.sub(r"(?im)^\s*(?:[a-z_]+\.)?tenant_id\s*=\s*:[a-z_]+\s+AND\s*", "", statement)
    statement = re.sub(r"(?im)\s+AND\s+(?:[a-z_]+\.)?tenant_id\s*=\s*:[a-z_]+", "", statement)
    statement = re.sub(r"(?im)\s+AND\s+[a-z_]+\.tenant_id\s*=\s+[a-z_]+\.tenant_id", "", statement)
    statement = re.sub(r"(?im)^\s*WHERE\s+(?:[a-z_]+\.)?tenant_id\s*=\s*:[a-z_]+\s*", "WHERE ", statement)
    statement = re.sub(r"(?im)^\s*(?:[a-z_]+\.)?tenant_id\s*,\s*", "", statement)
    statement = re.sub(r"(?im)^\s*:tenant_id\s*,\s*", "", statement)
    statement = re.sub(r"(?i)\(\s*tenant_id\s*,\s*", "(", statement)
    statement = re.sub(r"(?i),\s*tenant_id\s*\)", ")", statement)
    statement = re.sub(r"(?i)\(\s*:tenant_id\s*,\s*", "(", statement)
    statement = re.sub(r"(?i),\s*:tenant_id\s*\)", ")", statement)
    return sqlalchemy_text(statement)
