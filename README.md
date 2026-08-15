# WikipediaRag

WikipediaRag is a Docker-first RAG platform for Russian Wikipedia and uploaded
document knowledge bases. It provides authenticated multi-tenant ingestion,
hybrid retrieval, grounded answers, Extended Search and durable Deep Research.

The repository is a production-shaped MVP, not a finished external production
deployment. See [current status](docs/STATUS.md) for the active goal and blockers.

## Capabilities

- React/Vite UI for login, knowledge bases, Wikipedia import, uploads, search,
  chat, research, citations and retrieval debugging.
- FastAPI API with local/OIDC authentication, server-owned tenant context, KB
  roles, document ACLs, CSRF protection and safe errors.
- Asynchronous Wikipedia and document ingestion with validation, parsing,
  normalization, chunking, embedding and versioned publication.
- Hybrid BM25/vector retrieval, fusion, rerank, parent expansion,
  answerability and citation validation.
- Durable Deep Research over one to three authorized KBs with bounded tool
  episodes, evidence memory, verified claims and pause/resume/cancel.
- Deterministic tests, functional workflows, eval suites and release gates.

## Model Endpoints

Business code calls provider-neutral aliases through Model Gateway. Chat,
embeddings, rerank and token counting are separate operations; an alias is
usable when its configured endpoint supports the required contract and passes
readiness checks.

The current development setup uses OpenRouter endpoints. OpenRouter, vLLM,
llama.cpp, text-generation-webui, generic OpenAI-compatible endpoints and the
test-only mock are adapters behind the same Gateway boundary. Local runtimes
are optional and their absence alone must not block a healthy configured setup.

## Requirements

- Docker Desktop or compatible Docker Compose runtime.
- Python 3.12 with `uv`.
- Node.js 22.14+ with `pnpm`.
- GNU Make when available; on Windows use the equivalent commands from the
  `Makefile` directly.

## Quick Start

```bash
cp .env.example .env
make up
```

Default development credentials are `admin` / `admin`. Do not use them outside
an explicit development environment.

For Wikipedia import, place a ZIM file under ignored `zim/*.zim`, then run:

```bash
make import-zim-small WIKI_LIMIT=10000
```

Main local URLs:

- UI: <http://localhost:5173>
- API health/readiness: <http://localhost:8000/health> and <http://localhost:8000/ready>
- Model Gateway: <http://localhost:8081>
- MinIO console: <http://localhost:9001>
- OpenSearch: <http://localhost:9200>

## Validation

Choose checks by the changed contract; do not run unrelated long gates.

```bash
make lint
make format-check
make typecheck
make test-unit
make test-integration
make smoke
make smoke-models PROVIDER=mock
make verify-document-upload
make test-functional-retrieval
make test-functional-ui
make verify-cross-tenant-hardening
```

UI-only checks:

```bash
cd services/ui
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm test:e2e
```

### Search-quality workflow

Prepare and freeze reviewed material before starting a run:

```bash
make eval-quality-scaffold
make eval-quality-prepare
make eval-quality-review
make eval-quality-freeze
```

Ingest once, run dev, then resume the same immutable run for test:

```bash
make eval-quality-ingest EVAL_QUALITY_INGEST_ARGS="--api http://localhost:8000 --run-id <ingest-id>"
make eval-quality-run EVAL_QUALITY_RUN_ARGS="--api http://localhost:8000 --split dev --run-id <run-id>"
make eval-quality-run EVAL_QUALITY_RUN_ARGS="--api http://localhost:8000 --split test --resume-run-id <run-id>"
make eval-quality-report
make eval-quality-status
```

P0.1 is closed as an execution and compatibility workflow. Its synthetic result
is documented in [the 2026-08-15 baseline note](docs/eval-quality-p0-baseline-2026-08-15.md).
The separately pinned RRNCB run is a document-retrieval reference baseline; it
does not claim chunk-level evidence or answer quality.

## Documentation

- [Project status](docs/STATUS.md) — current goal, state, blocker and next step.
- [Architecture overview](docs/architecture.md) — system boundaries and data ownership.
- [Contract map](docs/architecture/contract-map.md) — canonical executable contracts.
- [Web](docs/architecture/web.md) — UI/API behavior.
- [Services](docs/architecture/services.md) — runtime responsibilities.
- [Data and storage](docs/architecture/data-and-storage.md) — authority and rebuild boundaries.
- [Flows](docs/architecture/flows.md) — authentication, ingestion, retrieval and research flows.
- [Security and tenancy](docs/architecture/security-and-tenancy.md) — authorization and redaction.
- [Search and Deep Research](docs/architecture/search-and-deep-research.md) — retrieval and research contracts.
- [Deployment and operations](docs/architecture/deployment-and-operations.md) — configuration and runtime checks.
- [Functional verification](docs/functional-verification.md) — checks selected by changed contract.
- [Agent guide](AGENTS.md) — repository engineering rules.

Historical status, research notes and completed execution plans remain under
`docs/history/`, `docs/research/` and `docs/exec-plans/`.
