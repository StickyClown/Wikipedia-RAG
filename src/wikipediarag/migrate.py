import asyncio

from wikipediarag.db import ensure_schema


async def run() -> None:
    await ensure_schema()
    print("database schema is ready")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
