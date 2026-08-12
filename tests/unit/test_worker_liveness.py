from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import wikipediarag.worker as worker


class _ConnectionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_lane_heartbeat_continues_while_job_processing_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat_lanes: list[str] = []
    two_heartbeats = asyncio.Event()
    processing = asyncio.Event()

    async def touch(_conn: object, *, worker_id: str, lane: str) -> None:
        assert worker_id == "worker-unit"
        heartbeat_lanes.append(lane)
        if len(heartbeat_lanes) >= 2:
            two_heartbeats.set()

    async def claim(*_args: object, **_kwargs: object) -> bool:
        await processing.wait()
        return True

    monkeypatch.setattr(worker, "connect", lambda *_args: _ConnectionContext())
    monkeypatch.setattr(worker, "touch_worker_heartbeat", touch)
    monkeypatch.setattr(worker, "claim_and_process_once", claim)

    task = asyncio.create_task(
        worker._run_lane(
            SimpleNamespace(worker_job_heartbeat_seconds=1),
            worker.BACKGROUND_KINDS,
            1,
            worker_id="worker-unit",
        )
    )
    try:
        await asyncio.wait_for(two_heartbeats.wait(), timeout=2.5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert heartbeat_lanes == ["/".join(worker.BACKGROUND_KINDS)] * 2
