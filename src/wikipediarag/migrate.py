import asyncio

from wikipediarag.db import WorkspaceResetRequiredError, ensure_schema


async def run() -> None:
    try:
        await ensure_schema()
    except WorkspaceResetRequiredError as exc:
        print(str(exc))
        raise SystemExit(2) from None
    print("database schema is ready")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
