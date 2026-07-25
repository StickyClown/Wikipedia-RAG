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


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
