from __future__ import annotations

import asyncio
import logging

from wikipediarag.db import connect, ensure_schema
from wikipediarag.ingestion import claim_and_process_once
from wikipediarag.repository import enqueue_due_source_sync_jobs

LOGGER = logging.getLogger(__name__)


async def run_worker() -> None:
    logging.basicConfig(level=logging.INFO)
    await ensure_schema()
    while True:
        try:
            async with connect() as conn:
                await enqueue_due_source_sync_jobs(conn)
            processed = await claim_and_process_once()
            if not processed:
                await asyncio.sleep(2)
        except Exception:
            LOGGER.exception("worker failed to process job")
            await asyncio.sleep(2)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
