from __future__ import annotations

from typing import Any, cast

import pytest

import wikipediarag.worker as worker
from wikipediarag import search_index
from wikipediarag.config import Settings
from wikipediarag.search_index import projection_fingerprint


class _Connection:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_projection_fingerprint_is_independent_of_json_object_field_order() -> None:
    expected = [
        {
            "chunk_id": "chunk",
            "document_version_id": "version",
            "content_hash": "hash",
            "metadata": {
                "publication_status": "published",
                "document_access": {"policy": "restricted", "users": ["u"]},
            },
        }
    ]
    observed = [
        {
            "_source": {
                "chunk_id": "chunk",
                "document_version_id": "version",
                "content_hash": "hash",
                "metadata": {
                    "document_access": {"users": ["u"], "policy": "restricted"},
                    "publication_status": "published",
                },
            }
        }
    ]

    assert projection_fingerprint(expected) == projection_fingerprint(observed)


def test_projection_bulk_rejects_http_success_with_failed_item(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_index,
        "bulk",
        lambda *_args, **_kwargs: (1, [{"index": {"status": 429, "error": {"type": "busy"}}}]),
    )
    with pytest.raises(RuntimeError, match="SEARCH_PROJECTION_BULK_ITEM_FAILED:429"):
        search_index._apply_projection_bulk(cast(Any, object()), [{"_op_type": "index", "_id": "chunk"}], refresh=False)


@pytest.mark.asyncio
async def test_access_projection_is_retried_after_index_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    retried: list[dict[str, Any]] = []

    async def claim(*_args: object, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": "event",
            "event_kind": "document_access",
            "tenant_id": "tenant",
            "knowledge_base_id": "kb",
            "document_id": "document",
            "payload": {"document_access": {"policy": "restricted"}, "origin": "manual"},
        }

    async def kb(*_args: object) -> dict[str, str]:
        return {"active_index": "read-kb"}

    async def retry(*_args: object, **kwargs: Any) -> None:
        retried.append(kwargs)

    async def index_failure(*_args: object, **_kwargs: Any) -> None:
        raise RuntimeError("index down")

    monkeypatch.setattr(worker, "connect", lambda *_args: _Connection())
    monkeypatch.setattr(worker, "claim_next_search_projection_event", claim)
    monkeypatch.setattr(worker, "get_knowledge_base", kb)
    monkeypatch.setattr(worker, "retry_search_projection_event", retry)
    monkeypatch.setattr("wikipediarag.worker.asyncio.to_thread", index_failure)

    assert await worker._process_search_projection_once(Settings(), lease_id="lease") is True
    assert retried[0]["error_code"]


@pytest.mark.asyncio
async def test_access_projection_is_completed_after_idempotent_index_update(monkeypatch: pytest.MonkeyPatch) -> None:
    completed: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []

    async def claim(*_args: object, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": "event",
            "event_kind": "document_access",
            "tenant_id": "tenant",
            "knowledge_base_id": "kb",
            "document_id": "document",
            "payload": {"document_access": {"policy": "restricted"}, "origin": "manual"},
        }

    async def kb(*_args: object) -> dict[str, str]:
        return {"active_index": "read-kb"}

    async def complete(*_args: object, **kwargs: Any) -> None:
        completed.append(kwargs)

    async def repair(*_args: object, **kwargs: Any) -> tuple[str, str]:
        repairs.append(kwargs)
        return "version", "fingerprint"

    monkeypatch.setattr(worker, "connect", lambda *_args: _Connection())
    monkeypatch.setattr(worker, "claim_next_search_projection_event", claim)
    monkeypatch.setattr(worker, "complete_search_projection_event", complete)
    monkeypatch.setattr(worker, "_repair_search_projection_document", repair)

    assert await worker._process_search_projection_once(Settings(), lease_id="lease") is True
    assert repairs[0]["document_id"] == "document"
    assert completed[0]["event_id"] == "event"


@pytest.mark.asyncio
async def test_publication_projection_replaces_one_exact_document_version(monkeypatch: pytest.MonkeyPatch) -> None:
    completed: list[dict[str, Any]] = []
    operations: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def claim(*_args: object, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": "event",
            "event_kind": "document_publication",
            "tenant_id": "tenant",
            "knowledge_base_id": "kb",
            "document_id": "document",
            "payload": {"document_version_id": "version", "chunk_ids": ["chunk-1"], "chunk_count": 1},
        }

    async def kb(*_args: object, **_kwargs: Any) -> dict[str, str]:
        return {"active_index": "read-kb"}

    async def complete(*_args: object, **kwargs: Any) -> None:
        completed.append(kwargs)

    async def repair(*_args: object, **kwargs: Any) -> tuple[str, str]:
        operations.append(("repair", (), kwargs))
        return "version", "fingerprint"

    monkeypatch.setattr(worker, "connect", lambda *_args: _Connection())
    monkeypatch.setattr(worker, "claim_next_search_projection_event", claim)
    monkeypatch.setattr(worker, "complete_search_projection_event", complete)
    monkeypatch.setattr(worker, "_repair_search_projection_document", repair)

    assert await worker._process_search_projection_once(Settings(), lease_id="lease") is True
    assert [name for name, _, _ in operations] == ["repair"]
    assert completed[0]["event_id"] == "event"


@pytest.mark.asyncio
async def test_reconciliation_lease_loss_does_not_fail_or_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    async def schedule(*_args: object, **_kwargs: Any) -> int:
        return 0

    async def claim(*_args: object, **_kwargs: Any) -> list[dict[str, str]]:
        return [{"document_id": "document", "tenant_id": "tenant", "knowledge_base_id": "kb"}]

    async def repair(*_args: object, **kwargs: Any) -> tuple[str | None, str]:
        assert not await kwargs["lease_guard"]()
        raise worker._SearchProjectionLeaseLost("SEARCH_PROJECTION_LEASE_LOST")

    async def renew(*_args: object, **_kwargs: Any) -> bool:
        return False

    async def finish(*_args: object, **kwargs: Any) -> None:
        completed.append(kwargs)

    async def fail(*_args: object, **kwargs: Any) -> None:
        failed.append(kwargs)

    async def cleanup(*_args: object, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(worker, "connect", lambda *_args: _Connection())
    monkeypatch.setattr(worker, "schedule_historical_search_projection_reconciliations", schedule)
    monkeypatch.setattr(worker, "claim_due_search_projection_reconciliations", claim)
    monkeypatch.setattr(worker, "renew_search_projection_reconciliation_lease", renew)
    monkeypatch.setattr(worker, "_repair_search_projection_document", repair)
    monkeypatch.setattr(worker, "finish_search_projection_reconciliation", finish)
    monkeypatch.setattr(worker, "fail_search_projection_reconciliation", fail)
    monkeypatch.setattr(worker, "cleanup_completed_search_projection_events", cleanup)

    assert await worker._process_search_projection_reconciliation_once(Settings(), lease_id="lost-owner") == 1
    assert completed == []
    assert failed == []


@pytest.mark.asyncio
async def test_reconciliation_renews_before_repair_and_completes_when_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    completed: list[dict[str, Any]] = []
    renewals = 0

    async def schedule(*_args: object, **_kwargs: Any) -> int:
        return 0

    async def claim(*_args: object, **_kwargs: Any) -> list[dict[str, str]]:
        return [{"document_id": "document", "tenant_id": "tenant", "knowledge_base_id": "kb"}]

    async def renew(*_args: object, **_kwargs: Any) -> bool:
        nonlocal renewals
        renewals += 1
        return True

    async def repair(*_args: object, **kwargs: Any) -> tuple[str | None, str]:
        assert await kwargs["lease_guard"]()
        return "version", "fingerprint"

    async def finish(*_args: object, **kwargs: Any) -> None:
        completed.append(kwargs)

    async def cleanup(*_args: object, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(worker, "connect", lambda *_args: _Connection())
    monkeypatch.setattr(worker, "schedule_historical_search_projection_reconciliations", schedule)
    monkeypatch.setattr(worker, "claim_due_search_projection_reconciliations", claim)
    monkeypatch.setattr(worker, "renew_search_projection_reconciliation_lease", renew)
    monkeypatch.setattr(worker, "_repair_search_projection_document", repair)
    monkeypatch.setattr(worker, "finish_search_projection_reconciliation", finish)
    monkeypatch.setattr(worker, "cleanup_completed_search_projection_events", cleanup)

    assert await worker._process_search_projection_reconciliation_once(Settings(), lease_id="owner") == 1
    assert renewals == 1
    assert completed[0]["document_id"] == "document"
