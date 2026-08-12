from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from wikipediarag.config import Settings, get_settings
from wikipediarag.repository import get_knowledge_base, load_index_version_by_read_alias
from wikipediarag.retrieval_contract import (
    KnowledgeBaseNotReady,
    RetrievalProfileIncompatible,
    _source_type_for_profile,
    validate_index_version_contract,
)
from wikipediarag.retrieval_profile import RetrievalProfile, get_retrieval_profile

_AUTO_PROFILE_ORDER = ("upload_sota_mvp", "upload_mock", "sota_mvp", "test_mock")


def normalize_retrieval_profile_request(requested: str | None) -> str | None:
    """Translate the public UI's automatic-profile sentinel to server defaulting.

    ``auto`` is deliberately not a configured retrieval profile.  Keeping the
    normalization at the resolver boundary preserves backward compatibility
    for older clients while ensuring persisted research plans always contain a
    concrete profile name.
    """

    normalized = str(requested or "").strip()
    return None if not normalized or normalized.casefold() == "auto" else normalized


async def resolve_retrieval_profile(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
    requested: str | None,
    overrides: dict[str, Any] | None = None,
    settings: Settings | None = None,
    get_knowledge_base_fn: Callable[[AsyncConnection, str, str], Awaitable[dict[str, Any] | None]] = get_knowledge_base,
    load_index_fn: Callable[..., Awaitable[dict[str, Any] | None]] = load_index_version_by_read_alias,
) -> RetrievalProfile:
    """Resolve one profile for the entire scope and validate every active contract.

    The resolver deliberately uses only server-owned index metadata.  A profile
    compatible with the first KB is not sufficient for a multi-KB request.
    """

    resolved = settings or get_settings()
    requested = normalize_retrieval_profile_request(requested)
    if not knowledge_base_ids:
        return get_retrieval_profile(requested, resolved, overrides)

    index_rows: list[dict[str, Any]] = []
    for knowledge_base_id in knowledge_base_ids:
        kb = await get_knowledge_base_fn(conn, tenant_id, knowledge_base_id)
        if kb is None or not kb.get("active_index"):
            raise KnowledgeBaseNotReady(
                "knowledge base has no active retrieval contract",
                details={"knowledge_base_id": knowledge_base_id},
            )
        row = await load_index_fn(
            conn,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            read_alias=str(kb["active_index"]),
        )
        if row is None:
            raise KnowledgeBaseNotReady(
                "active retrieval contract is unavailable",
                details={"knowledge_base_id": knowledge_base_id},
            )
        index_rows.append(dict(row))

    if requested:
        profile = get_retrieval_profile(requested, resolved, overrides)
        _validate_profile(index_rows, profile=profile, overrides=overrides, settings=resolved)
        return profile

    compatible: list[str] = []
    for name in _AUTO_PROFILE_ORDER:
        try:
            candidate = get_retrieval_profile(name, resolved, overrides)
        except (KeyError, ValueError):
            continue
        if all(
            str(row.get("source_type") or "") == _source_type_for_profile(candidate)
            and str(row.get("embedding_alias") or "") == candidate.model_aliases.embed
            for row in index_rows
        ):
            compatible.append(name)
    if not compatible:
        raise RetrievalProfileIncompatible(
            "no retrieval profile is compatible with every scoped knowledge base",
            details={"knowledge_base_count": len(knowledge_base_ids)},
        )

    preferred = resolved.retrieval_profile if resolved.retrieval_profile in compatible else compatible[0]
    profile = get_retrieval_profile(preferred, resolved, overrides)
    _validate_profile(index_rows, profile=profile, overrides=overrides, settings=resolved)
    return profile


def _validate_profile(
    index_rows: list[dict[str, Any]],
    *,
    profile: RetrievalProfile,
    overrides: dict[str, Any] | None,
    settings: Settings,
) -> None:
    try:
        for row in index_rows:
            validate_index_version_contract(
                row,
                profile=profile,
                retrieval_overrides=overrides,
                settings=settings,
            )
    except RetrievalProfileIncompatible:
        raise
    except Exception as exc:
        details = getattr(exc, "details", {})
        allowed_keys = {
            "expected_source_type",
            "actual_source_type",
            "profile_embedding_alias",
            "index_embedding_alias",
            "read_alias",
        }
        if isinstance(details, dict) and any(
            key in details for key in ("expected_source_type", "actual_source_type", "profile_embedding_alias")
        ):
            raise RetrievalProfileIncompatible(
                "retrieval profile is incompatible with the active index contract",
                details={key: value for key, value in details.items() if key in allowed_keys},
            ) from exc
        raise
