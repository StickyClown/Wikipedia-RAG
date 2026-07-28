# Codex repository instructions

## Mission

Implement the Production RAG Platform incrementally, with one narrow approved task at a time. Keep the working source of truth compact:

1. `AGENTS.md`
2. `README.md`
3. `docs/architecture.md`
4. `docs/STATUS.md`

Before implementation, briefly state which of these files were read and the current milestone from `docs/STATUS.md`.

## Current milestone rule

`docs/STATUS.md` is authoritative for the active milestone, validation evidence and blockers. If status says a provider-backed release gate is blocked, do not rerun it until the documented readiness/smoke prerequisite is satisfied.

## Scope control

- Work on one approved task at a time.
- Do not expand scope because adjacent functionality looks useful.
- Do not replace approved components with similar libraries without an explicit decision.
- Do not add GraphRAG, multi-agent swarm, ColBERT, learned sparse retrieval or proposition indexing without a separate approved research plan.
- Do not create synchronous ingestion of large files inside an HTTP request.
- Business code must not call OpenRouter or `llama-server` directly; use the Model Gateway contract.

## Baseline stack

- Python 3.12 and `uv` with lockfile.
- FastAPI, Pydantic v2, SQLAlchemy 2, asyncpg.
- pytest, pytest-asyncio, Ruff and strict mypy for Python quality.
- React + Vite + TypeScript UI with `pnpm`.
- PostgreSQL, OpenSearch, MinIO, Redis/Valkey and Kiwix via pinned Docker images.
- OpenTelemetry from the first vertical slice.

Changing these requires an explicit decision with consequences and migration impact.

## Architecture invariants

- API and workers are stateless where practical.
- Every persistent entity and search document carries `tenant_id` where applicable.
- Tenant filters are injected server-side; never trust tenant filters supplied by raw client queries.
- Original files and normalized artifacts live in object storage; OpenSearch is not the source of truth.
- Physical indices are versioned and published by atomic alias switch.
- Document, parser template, embedding and index versions remain reproducible.
- Every query run has deterministic request/trace IDs and structured retrieval events.
- Secrets, prompts, provider payloads and document contents must be redacted from normal logs.
- Background transitions must be idempotent and resumable.

## Work protocol

1. Inspect current code, tests and relevant compact docs before editing.
2. Implement the smallest complete behavior.
3. Add or update deterministic tests for success and important failure paths.
4. Run the relevant stable commands.
5. Review the diff for security, tenancy, errors, migrations, observability and accidental scope growth.
6. Update `docs/STATUS.md` with exact commands and results when the task changes project state.
7. Do not claim a command passed unless it was run and exited successfully.

## Commands

Use stable Make targets when available:

```text
make bootstrap
make up
make down
make lint
make format-check
make typecheck
make test-unit
make test-integration
make test-e2e
make smoke
make eval
```

If `make` is unavailable on the host, run equivalent `uv`, `pnpm` or `docker compose` commands and record the exact commands and exit codes.

## Long-running command observability

- Do not run long eval, ingestion, release-gate, provider generation or benchmark commands silently.
- Commands expected to take more than a few minutes must expose live progress, status polling or append-only logs with stage, processed/total, last update and failure state.
- If interrupted or timed out, inspect remaining processes and artifact state before continuing.

## Definition of done

A task is done only when:

- requested behavior exists end-to-end;
- success and important failure paths are tested;
- lint, format, type checks and relevant tests pass or failures are explicitly documented as blockers;
- public contracts and migrations are documented when changed;
- no secrets, unbounded retries, cross-tenant paths or silent failures were introduced;
- `docs/STATUS.md` reflects reality;
- the final response reports files changed, commands run, results, risks and remaining work.

## Forbidden shortcuts

- Do not use destructive Git, Docker volume, database or index commands without explicit user approval.
- Do not delete or rewrite existing migrations after commit; add a new migration.
- Do not suppress type/lint errors broadly.
- Do not use mutable Docker tags in production profiles.
- Do not commit `.env`, keys, passwords, model files, ZIM snapshots, uploads, generated indices or large evaluation artifacts.
- Mocks are allowed only in tests and explicit local demo profiles.

## Code review priority

Prioritize findings in this order:

1. cross-tenant data exposure;
2. secret leakage or unsafe parsing;
3. data loss, non-idempotent jobs and broken migrations;
4. incorrect citations or provenance;
5. unbounded loops, retries or concurrency;
6. API compatibility and error semantics;
7. observability gaps;
8. performance regressions;
9. maintainability.
