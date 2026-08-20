from __future__ import annotations

from typing import Any

import wikipediarag.api.handlers as api_app
from wikipediarag.auth import ActorContext, AuthenticationMethod, PlatformRole


async def test_persisted_research_evidence_is_hidden_after_document_revocation(monkeypatch: Any) -> None:
    class Repository:
        def __init__(self, _conn: object) -> None:
            pass

        async def authorized_document_ids(self, **_kwargs: object) -> frozenset[str]:
            return frozenset({"doc:still-visible"})

    actor = ActorContext(
        user_id="user",
        platform_role=PlatformRole.user,
        session_id="session",
        authentication_method=AuthenticationMethod.test,
        request_id="request",
        trace_id="trace",
    )
    monkeypatch.setattr(api_app, "WorkspaceGrantRepository", Repository)

    evidence = await api_app._reauthorize_research_evidence(
        object(),
        [{"id": "e1", "document_id": "doc:still-visible"}, {"id": "e2", "document_id": "doc:revoked"}],
        actor=actor,
    )

    assert [row["id"] for row in evidence] == ["e1"]
