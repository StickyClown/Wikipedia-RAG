from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable


def stable_hash(parts: Iterable[object], length: int = 40) -> str:
    h = hashlib.sha1()  # noqa: S324 - deterministic IDs, not a security boundary.
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:length]


def stable_uuid(parts: Iterable[object]) -> uuid.UUID:
    digest = stable_hash(parts, length=32)
    return uuid.UUID(digest)


def scoped_id(
    namespace: str,
    native_id: object,
    *,
    tenant_id: str | None = None,
    knowledge_base_id: str | None = None,
    source_type: str | None = None,
    snapshot_id: str | None = None,
) -> str:
    """Build an opaque, deterministic id whose scope is part of its identity.

    Native source identifiers are intentionally kept out of the primary key
    namespace.  This prevents importing the same snapshot into two scopes from
    turning an upsert into a cross-tenant overwrite while retaining the native
    value in metadata for citations and migrations.
    """
    scope = [
        namespace,
        tenant_id or "legacy",
        knowledge_base_id or "legacy",
        source_type or "unknown",
        snapshot_id or "unknown",
        native_id,
    ]
    return f"{namespace}:{stable_hash(scope, 40)}"


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
