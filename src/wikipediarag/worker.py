from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from wikipediarag.db import connect, ensure_schema
from wikipediarag.ingestion import claim_and_process_once
from wikipediarag.model_client import close_http_client
from wikipediarag.repository import enqueue_due_source_sync_jobs, touch_worker_heartbeat

LOGGER = logging.getLogger(__name__)

RESEARCH_KINDS = ("deep_research",)
BACKGROUND_KINDS = ("wikipedia_xml", "wikipedia_zim", "document_upload", "source_sync", "document_delete")


async def _run_lane(
    settings: Any,
    allowed_kinds: tuple[str, ...],
    concurrency: int,
    *,
    worker_id: str,
    enqueue_sources: bool = False,
) -> None:
    lane = "/".join(allowed_kinds)

    async def heartbeat() -> None:
        interval = max(int(settings.worker_job_heartbeat_seconds), 1)
        while True:
            try:
                async with connect(settings) as conn:
                    await touch_worker_heartbeat(conn, worker_id=worker_id, lane=lane)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("worker heartbeat failed lane=%s", lane)
            await asyncio.sleep(interval)

    async def runner() -> None:
        while True:
            try:
                if enqueue_sources:
                    async with connect(settings) as conn:
                        await enqueue_due_source_sync_jobs(conn)
                processed = await claim_and_process_once(
                    settings,
                    allowed_kinds=allowed_kinds,
                    lease_id=str(uuid.uuid4()),
                )
                if not processed:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("worker lane failed")
                await asyncio.sleep(2)

    async with asyncio.TaskGroup() as group:
        group.create_task(heartbeat())
        for _ in range(max(int(concurrency), 1)):
            group.create_task(runner())


async def run_worker() -> None:
    logging.basicConfig(level=logging.INFO)
    from wikipediarag.config import get_settings

    settings = get_settings()
    worker_id = str(uuid.uuid4())
    await ensure_schema(settings)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(
                _run_lane(
                    settings,
                    RESEARCH_KINDS,
                    settings.worker_research_concurrency,
                    worker_id=f"{worker_id}:research",
                )
            )
            group.create_task(
                _run_lane(
                    settings,
                    BACKGROUND_KINDS,
                    settings.worker_background_concurrency,
                    worker_id=f"{worker_id}:background",
                    enqueue_sources=True,
                )
            )
    finally:
        await close_http_client()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
