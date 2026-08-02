from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from wikipediarag.auth import KB_ROLE_RANK, KnowledgeBaseRole, PlatformRole, TenantRole

DOCUMENT_ACCESS_METADATA_KEY = "document_access"
DOCUMENT_ACCESS_POLICY_KB = "kb"
DOCUMENT_ACCESS_POLICY_TENANT = "tenant"
DOCUMENT_ACCESS_POLICY_RESTRICTED = "restricted"
DOCUMENT_ACCESS_POLICIES = {
    DOCUMENT_ACCESS_POLICY_KB,
    DOCUMENT_ACCESS_POLICY_TENANT,
    DOCUMENT_ACCESS_POLICY_RESTRICTED,
}


@dataclass(frozen=True, slots=True)
class DocumentAccessScope:
    bypass: bool = False
    tenant_id: str = ""
    user_id: str = ""
    kb_role: KnowledgeBaseRole | None = None
    group_ids: frozenset[str] = field(default_factory=frozenset)


def document_access_bypass(
    *,
    platform_role: PlatformRole,
    tenant_role: TenantRole | None,
    kb_role: KnowledgeBaseRole | None,
) -> bool:
    if platform_role == PlatformRole.platform_admin:
        return True
    if tenant_role == TenantRole.tenant_admin:
        return True
    return kb_role is not None and KB_ROLE_RANK[kb_role] >= KB_ROLE_RANK[KnowledgeBaseRole.manager]


def normalize_document_access(raw: Any) -> dict[str, Any]:
    """Normalize trusted document ACL metadata to the minimal v1 contract."""
    if not isinstance(raw, dict):
        return {"policy": DOCUMENT_ACCESS_POLICY_KB, "user_ids": [], "group_ids": []}
    policy = str(raw.get("policy") or DOCUMENT_ACCESS_POLICY_KB).strip().casefold()
    if policy == DOCUMENT_ACCESS_POLICY_TENANT:
        return {"policy": DOCUMENT_ACCESS_POLICY_TENANT, "user_ids": [], "group_ids": []}
    if policy != DOCUMENT_ACCESS_POLICY_RESTRICTED:
        return {"policy": DOCUMENT_ACCESS_POLICY_KB, "user_ids": [], "group_ids": []}
    return {
        "policy": DOCUMENT_ACCESS_POLICY_RESTRICTED,
        "user_ids": _string_list(raw.get("user_ids")),
        "group_ids": _string_list(raw.get("group_ids")),
    }


def normalize_document_metadata_access(metadata: dict[str, Any], *, trusted: bool) -> dict[str, Any]:
    copied = dict(metadata)
    if trusted:
        copied[DOCUMENT_ACCESS_METADATA_KEY] = normalize_document_access(copied.get(DOCUMENT_ACCESS_METADATA_KEY))
    else:
        copied[DOCUMENT_ACCESS_METADATA_KEY] = normalize_document_access(None)
    return copied


def is_document_visible(metadata: dict[str, Any] | None, scope: DocumentAccessScope | None) -> bool:
    if scope is None or scope.bypass:
        return True
    access = normalize_document_access((metadata or {}).get(DOCUMENT_ACCESS_METADATA_KEY))
    if access["policy"] == DOCUMENT_ACCESS_POLICY_KB:
        return scope.kb_role is not None
    if access["policy"] == DOCUMENT_ACCESS_POLICY_TENANT:
        return bool(scope.tenant_id)
    if scope.user_id and scope.user_id in set(access["user_ids"]):
        return True
    return bool(scope.group_ids & set(access["group_ids"]))


def document_access_filter(scope: DocumentAccessScope | None) -> list[dict[str, Any]]:
    if scope is None or scope.bypass:
        return []
    should: list[dict[str, Any]] = [
        {"term": {"metadata.document_access.policy.keyword": DOCUMENT_ACCESS_POLICY_TENANT}},
        {"term": {"metadata.document_access.user_ids.keyword": scope.user_id}},
    ]
    if scope.kb_role is not None:
        should.extend(
            [
                {"bool": {"must_not": [{"exists": {"field": "metadata.document_access.policy"}}]}},
                {"term": {"metadata.document_access.policy.keyword": DOCUMENT_ACCESS_POLICY_KB}},
            ]
        )
    group_ids = sorted(scope.group_ids)
    if group_ids:
        should.append({"terms": {"metadata.document_access.group_ids.keyword": group_ids}})
    return [{"bool": {"should": should, "minimum_should_match": 1}}]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result
