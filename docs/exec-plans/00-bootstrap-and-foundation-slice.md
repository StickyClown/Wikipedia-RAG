# ExecPlan 00 — Repository bootstrap and foundation vertical slice

## Outcome

A clean checkout can start a minimal Docker Compose stack and execute `POST /api/v1/chat` through FastAPI and an internal Model Gateway to a deterministic mock OpenAI-compatible provider, stream SSE events, persist a `query_run` in PostgreSQL and emit an OpenTelemetry trace.

## Why this plan exists

This proves repository conventions, service boundaries, migrations, streaming, provider abstraction and observability before retrieval complexity is added.

## In scope

- monorepo directories from architecture;
- Python and frontend workspace bootstrapping, but no UI feature implementation;
- Makefile command surface;
- Compose services: PostgreSQL, Redis/Valkey, MinIO, OpenSearch, OTel collector and mock model provider; optional profiles are acceptable;
- API and Model Gateway service skeletons;
- Phase 0 database tables;
- health/readiness;
- SSE chat vertical slice;
- typed provider client and normalized errors;
- unit/integration tests and one curl demo;
- CI for lint, types and tests.

## Out of scope

Authentication UI, retrieval, embeddings, reranking, ingestion, Wikipedia, document upload, real production deployment and local llama.cpp.

## Preconditions

Read `AGENTS.md`, `SPEC.md`, architecture sections 3–5, 14–15, 17, 23–27, API/DB contracts and ADR-001/005.

## Contracts and invariants

- provider-specific code exists only in Model Gateway;
- API accepts no raw provider URL/model from clients;
- test suite never needs a real OpenRouter key;
- request ID, query run ID and trace ID correlate logs/events/DB;
- provider timeout/error becomes safe `run.failed` and persisted failed status;
- cancellation/disconnect does not leave a completed status falsely recorded.

## Milestones

### M0.1 Repository toolchain

Create pinned Python/Node tooling, workspace layout, `.gitignore`, Makefile and CI skeleton.

Validation:

```bash
make bootstrap
make lint
make format-check
make typecheck
```

### M0.2 Infrastructure and migrations

Create Compose config, health checks, PostgreSQL migrations and test fixtures.

Validation:

```bash
make up
make migrate
make test-integration TEST=database
```

### M0.3 Model Gateway

Implement logical alias registry, mock provider adapter, `/v1/chat/completions`, health/readiness, error normalization and telemetry.

Validation:

```bash
make test-unit TEST=model-gateway
make test-integration TEST=model-gateway
```

### M0.4 API streaming slice

Implement `/api/v1/chat`, persisted run lifecycle and SSE events.

Validation:

```bash
make test-unit TEST=api
make test-integration TEST=chat-stream
make smoke
```

### M0.5 Review and documentation

Generate OpenAPI, document clean-room commands, run full checks and review diff.

Validation:

```bash
make lint
make format-check
make typecheck
make test-unit
make test-integration
make smoke
```

## Acceptance criteria

- all validation commands exit 0;
- `curl` receives ordered `run.started`, one or more `message.delta`, and `run.completed` events;
- corresponding DB row is completed and contains correlation IDs;
- forced provider timeout yields `run.failed`, safe error and failed DB status;
- no real network/API key is needed for tests;
- OpenAPI matches the implemented foundation contract;
- `docs/STATUS.md` contains actual evidence.

## Demo

```bash
cp .env.example .env
make bootstrap
make up
make migrate
make smoke
```

Then inspect the persisted query run and trace using documented commands.

## Rollback and recovery

`make down` may stop containers but must not delete volumes. A separate destructive cleanup target must require explicit confirmation and is not run by Codex automatically.

## Progress

- [ ] M0.1
- [ ] M0.2
- [ ] M0.3
- [ ] M0.4
- [ ] M0.5

## Discoveries

None yet.

## Decision log

None yet.

## Final evidence

Not executed.
