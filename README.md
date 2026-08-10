# WikipediaRag

WikipediaRag is a Docker-first RAG platform for Russian Wikipedia and uploaded
document knowledge bases. The current implementation is a local
production-shaped MVP: it has production-style service boundaries, auth,
tenancy, async ingestion and validation gates, but it is not yet an externally
operated production deployment.

Current project state and the next approved task are tracked in
[docs/STATUS.md](docs/STATUS.md).

## Main Capabilities

- React/Vite web UI for login, knowledge-base selection, Wikipedia import, multi-file upload, search, Deep Research, chat and retrieval debugging.
- FastAPI API with local auth, OIDC foundation, opaque session cookies, CSRF and server-owned `ActorContext`.
- Tenant and knowledge-base role enforcement for chat, debug search, uploads, imports, document reads, reprocess, deletion and sharing APIs.
- Async Wikipedia import from local ZIM via Kiwix/libzim, with XML fallback for local regression paths.
- Async document ingestion through presigned MinIO upload, worker validation, parsing, normalization, chunking, embedding and publication.
- Parser services for document formats: app-owned local adapters, Xberg default parser, Docling high-quality fallback and metadata-service language/date extraction.
- Hybrid retrieval with BM25, dense vectors, RRF, rerank, parent expansion, answerability and citation validation.
- Direct Multi-KB chat/debug retrieval with all-KB role and readiness checks; the chat Extended Search harness remains single-KB.
- Durable Deep Research with one-to-three KB scoped runs, stage-specific 80k/24k context budgets, bounded planner/tool episodes, typed evidence/claim/decision memory, pause/resume/cancel, heartbeat recovery and ACL-trimmed reports.
- Local eval, document corpus verification, release-gate reports and cross-tenant hardening smoke commands.

## User Path

1. Sign in with local credentials or OIDC.
2. Select an active knowledge base, or create one from the toolbar.
3. Import a bounded Wikipedia ZIM subset or upload documents.
4. Watch ingestion progress while the worker validates, parses and publishes chunks.
5. Search documents, open exact chunks in the viewer, or start a Deep Research run for a selected KB.
6. Ask questions in chat, inspect cited sources, and open the retrieval debugger for a query run.

The current UI is a single screen, not a routed multi-page app. Details are in
[docs/architecture/web.md](docs/architecture/web.md).

## Deep Research: Current State

Deep Research investigates a question inside a **server-owned scope of one to
three KBs in the same tenant**. It does not browse the public web. A run keeps
a primary KB for lifecycle ownership plus a durable scope snapshot, packs only
the current episode envelope, validates a planner decision, calls a closed
local-private tool registry (`extended_search`, section lookup, in-document
search, table/CSV lookup and metadata lookup), saves evidence and verified
claims, and can append deduplicated derived questions such as aliases, owners,
exceptions, blockers, budget or scope terms. The final report is rebuilt only
from evidence still visible to the current actor.

The current default Deep Research stage profiles are:

- planner/reflection: `generator_fast`, `80k` declared context, `45/55/70`
  productive/soft/hard ratios;
- claim verification: `generator_main`, `24k` input and `2k` output;
- final synthesis: `generator_main`, `80k` declared context, same `45/55/70`
  ratios;
- ordinary Search / Extended Search outside Deep Research: unchanged `30k`
  normal search context budget.

What has actually been demonstrated:

- The normal mock runtime smoke passed 10/10 synthetic fixtures.
- The isolated hard mock preflight passed the `alias_reformulation_chain`
  upload-to-report case: all three required evidence markers, 9 completed
  hashed tool calls, 7 derived questions, evidence recall `1.0`, zero
  unsupported claims and ACL safety. This confirms the local system path, not
  general research quality.
- The full hard OpenRouter/Qwen proxy baseline at the default 45% context
  target did not complete successfully within its shared 900-second deadline.
  It must not be used to claim that policy-exception, contradiction or finance
  chain cases are solved by a real model; no 35% candidate comparison has run.

The target remains fully local/private model usage. OpenRouter-backed Qwen is a
temporary development/proxy validation path behind Model Gateway aliases; no
business component calls a provider directly.

## Requirements

- Docker Desktop or compatible Docker Compose runtime.
- Python 3.12 and `uv`.
- Node.js 22.14+ and `pnpm`.
- GNU Make when available. On Windows, use direct `uv`, `pnpm` or `docker compose` equivalents if `make` is unavailable.

## Quick Start

```bash
cp .env.example .env
make up
```

If `make` is unavailable, inspect [Makefile](Makefile) and run the equivalent
`docker compose` command directly.

With default local settings, sign in as `admin` / `admin`. These defaults are
development-only.

For Wikipedia import, place a Russian Wikipedia ZIM file under ignored
`zim/*.zim`, then run:

```bash
make import-zim-small WIKI_LIMIT=10000
```

## Local URLs

- UI: <http://localhost:5173>
- API health/readiness: <http://localhost:8000/health>, <http://localhost:8000/ready>
- Model Gateway: <http://localhost:8081>
- Mock provider: <http://localhost:8082>
- Kiwix: <http://localhost:8083>
- Metadata service: <http://localhost:8090>
- Xberg: <http://localhost:8091>
- Docling: <http://localhost:8092>
- MinIO console: <http://localhost:9001>
- OpenSearch: <http://localhost:9200>

## Validation Commands

Use the stable targets that match your change:

```bash
make lint
make format-check
make typecheck
make test-unit
make test-integration
make smoke
make smoke-models PROVIDER=mock
make verify-document-upload
make verify-document-corpus
make verify-cross-tenant-hardening
make deep-research-smoke
make deep-research-matrix
make deep-research-hard-gate
make eval-document-prepare EVAL_DOCUMENT_PREPARE_ARGS="--documents-dir RRNCB --output-suite rrncb-unit"
```

## RRNCB Benchmark Inputs

The 65 RRNCB PDF files are local evaluation inputs, not repository source.
Keep them in the ignored `RRNCB/` directory or another local path, then use an
explicit `--output-suite`, `--suite` and fresh `--run-id` for a new benchmark.
Do not reuse or overwrite the historical `rrncb-public-v1` artifacts.

UI checks:

```bash
cd services/ui
pnpm lint
pnpm typecheck
pnpm build
```

Docs-only changes do not require the full unit/integration/e2e suite unless
the active task or CI policy says otherwise.

## Documentation Map

- [docs/STATUS.md](docs/STATUS.md) - active goal, implemented snapshot, validation, blockers and next approved task.
- [docs/architecture.md](docs/architecture.md) - architecture overview and index.
- [docs/architecture/web.md](docs/architecture/web.md) - actual web UI screens, browser state, cookies, CSRF and SSE behavior.
- [docs/architecture/services.md](docs/architecture/services.md) - runtime component responsibilities.
- [docs/architecture/data-and-storage.md](docs/architecture/data-and-storage.md) - data ownership, source-of-truth and backup boundaries.
- [docs/architecture/flows.md](docs/architecture/flows.md) - login, upload, ingestion, retrieval, delete and readiness flows.
- [docs/architecture/security-and-tenancy.md](docs/architecture/security-and-tenancy.md) - auth, roles, tenancy and access controls.
- [docs/architecture/search-and-deep-research.md](docs/architecture/search-and-deep-research.md) - backend search, chat retrieval, Extended Search and Deep Research contract.
- [docs/architecture/deployment-and-operations.md](docs/architecture/deployment-and-operations.md) - local Compose and external deployment requirements.
- [AGENTS.md](AGENTS.md) - long-lived instructions for coding agents.
- [docs/exec-plans/35-development-roadmap.md](docs/exec-plans/35-development-roadmap.md) and [docs/exec-plans/36-deep-research-tool-loop.md](docs/exec-plans/36-deep-research-tool-loop.md) - current/recent implementation plans and their measured status.
- [docs/exec-plans/](docs/exec-plans/) - older implementation plans retained as historical records.
- [docs/history/STATUS-archive.md](docs/history/STATUS-archive.md) - compact archive of historical status details.
- [docs/decisions/](docs/decisions/) - ADR guidance and template.
