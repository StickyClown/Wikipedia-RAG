# ADR-001 — Backend toolchain

Status: accepted for baseline

## Decision

Use Python 3.12, `uv`, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, asyncpg, pytest, Ruff and mypy. Use a monorepo with independently buildable services and shared typed packages.

## Rationale

The architecture is async I/O heavy, requires strict contracts, background workers and multiple HTTP services. This stack is mature, testable and well supported by code-generation agents.

## Consequences

- lock dependencies and Python version;
- no provider SDK in domain/application packages;
- migrations are append-only after commit;
- shared packages must not import service entrypoints.
