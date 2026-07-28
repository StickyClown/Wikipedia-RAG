# Codex repository instructions

## Mission

Последовательно реализовать Production RAG Platform согласно `SPEC.md` и `docs/architecture.md`. Работать только по одному утверждённому ExecPlan за раз.

## Mandatory reading before any implementation

1. `SPEC.md`
2. `docs/architecture.md`
3. `docs/DECISIONS_REQUIRED.md`
4. `.agent/PLANS.md`
5. текущий файл в `docs/exec-plans/`
6. `docs/STATUS.md`
7. релевантные файлы в `docs/contracts/`, `docs/decisions/` и `docs/quality/`

Перед изменениями кратко перечисли прочитанные управляющие документы и текущий milestone.

## Scope control

- Не реализуй несколько ExecPlan одновременно.
- Не расширяй scope из-за того, что соседняя функция кажется полезной.
- Не заменяй утверждённые компоненты библиотеками-аналогами без ADR.
- Не добавляй GraphRAG, multi-agent swarm, ColBERT, learned sparse или proposition indexing до отдельного утверждённого R&D-плана.
- Не создавай синхронный ingestion больших файлов внутри HTTP request.
- Не вызывай OpenRouter или `llama-server` напрямую из бизнес-кода: только через Model Gateway contract.

## Baseline technology decisions

- Python 3.12.
- Backend dependency manager: `uv` with lockfile.
- FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, asyncpg.
- Test stack: pytest, pytest-asyncio, testcontainers where suitable.
- Python quality: Ruff formatting/linting and mypy strict for domain/contracts.
- UI: React + Vite + TypeScript, package manager `pnpm`.
- Jobs: Dramatiq with Redis/Valkey broker for MVP.
- PostgreSQL, OpenSearch, MinIO, Redis/Valkey via pinned Docker images.
- OpenTelemetry from the first vertical slice.

Changing one of these requires a new ADR with consequences and migration impact.

## Architecture invariants

- API and workers are stateless where possible.
- Every persistent entity and search document carries `tenant_id` where applicable.
- Tenant filters are injected server-side; never trust a tenant filter supplied in a raw client query.
- Original files and normalized artifacts live in object storage; OpenSearch is not the source of truth.
- Physical indices are versioned and published by atomic alias switch.
- Document, parser template, embedding and index versions remain reproducible.
- Every query run has a deterministic request/trace ID and structured retrieval events.
- Secrets and document contents must be redacted from normal logs.
- All background transitions must be idempotent and resumable.

## Work protocol

For each milestone:

1. Inspect the current code and tests before editing.
2. Update the ExecPlan `Progress`, `Discoveries`, and `Decision log` as work proceeds.
3. Implement the smallest complete vertical behavior.
4. Add or update deterministic tests.
5. Run the milestone validation commands.
6. Review the diff for security, tenancy, error handling, migrations, observability and accidental scope growth.
7. Update `docs/STATUS.md` with exact commands and results.
8. Do not mark complete while required checks fail.

## Long-running command observability

- Do not run long commands silently. Any command expected to take more than a few minutes must expose live progress, status polling, or append-only logs that make the current stage, processed/total counts, last update time and failure state visible.
- If an existing CLI does not provide real-time progress, first add or use a status/logging wrapper before running it for a milestone.
- Do not leave a long-running eval, ingestion, release-gate, provider-backed generation or benchmark command running when its progress cannot be inspected.
- If a long command is interrupted or times out, immediately check for remaining processes, stop only the affected command if needed, and record the partial artifact state before continuing.

## Commands contract

Until the bootstrap milestone creates the commands, document missing commands rather than inventing successful results. After bootstrap, the repository must expose stable commands through `make`:

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

Use these commands rather than ad hoc variants once they exist.

## Definition of done

A task is done only when:

- requested behavior exists end-to-end;
- success and important failure paths are tested;
- lint, formatting, type checks and relevant tests pass;
- public contracts and migrations are documented;
- no secrets, unbounded retries, cross-tenant paths or silent failures were introduced;
- `docs/STATUS.md` and the active ExecPlan reflect reality;
- the final response reports files changed, commands run, results, risks and remaining work.

## Forbidden shortcuts

- Do not claim a command passed unless it was run and its exit code was successful.
- Do not replace required integrations with permanent mocks. Mocks are allowed only in tests and explicit local demo profiles.
- Do not suppress type/lint errors broadly.
- Do not use mutable Docker tags in production profiles.
- Do not commit `.env`, keys, passwords, model files, ZIM snapshots, uploads or generated indices.
- Do not delete or rewrite existing migrations after they have been committed; add a new migration.
- Do not run destructive Git, Docker volume, database or index commands without explicit user approval.

## Code review rules

Prioritize findings in this order:

1. cross-tenant data exposure;
2. secret leakage or unsafe parsing;
3. data loss, non-idempotent jobs, broken migrations;
4. incorrect citations or provenance;
5. unbounded loops/retries/concurrency;
6. API compatibility and error semantics;
7. observability gaps;
8. performance regressions;
9. maintainability.
