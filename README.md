# WikipediaRag

Local Docker-first RAG MVP for Russian Wikipedia. The current real demo path uses one local Russian Wikipedia ZIM file: Kiwix serves the full archive, while the worker indexes a bounded canonical article subset through `python-libzim`. Wikimedia XML `pages-articles` multistream remains a supported regression/local fallback.

For Codex and engineer handoff, use this repository-level source set:

- `AGENTS.md` - working protocol, scope control and safety rules.
- `README.md` - current runbook and project handoff.
- `docs/architecture.md` - compact architecture, contracts, invariants and decisions.
- `docs/STATUS.md` - current state, latest validation evidence and blockers.

## Current State

Active milestone: ExecPlan 22 behavior is implemented and deterministic validation passed. Provider-backed reviewed release-gate execution is blocked because OpenRouter access currently returns `403 Forbidden`; the latest reviewed gate status remains completed but failed on quality metrics from the prior run.

Implemented local MVP capabilities:

- FastAPI API, worker and Model Gateway on Python 3.12.
- React + Vite + TypeScript UI.
- PostgreSQL, OpenSearch, MinIO, Redis/Valkey, Kiwix and OpenTelemetry Collector via Docker Compose.
- Model Gateway aliases for OpenRouter-backed `sota_mvp`; mock aliases are explicit and only for tests/local demo.
- ZIM/libzim ingestion with checkpoints, redirects as provenance, deterministic chunks and Kiwix source URLs.
- XML multistream fallback ingestion for regression/development.
- Hybrid retrieval: BM25, dense search, service-side RRF, rerank, dedup/page quota, parent expansion and token-budget context packing.
- Chat SSE with evidence IDs, source links, retrieval trace, answerability gate, citation validation and safe timings.
- Retrieval debugger via `POST /api/v1/search:debug`.
- Bounded Extended Search MVP with multi-query search and tenant-scoped neighbor expansion.
- Local JSONL evaluation, trusted dataset generation, reviewed freeze workflow and release-gate runner.

## Requirements

- Docker Desktop or compatible Docker Compose runtime.
- Python 3.12 and `uv`.
- Node.js 22.14+ and `pnpm`.
- GNU Make when available. In this Windows Codex environment `make` may be absent, so equivalent `uv`/`pnpm` commands are acceptable and must be recorded exactly.

Do not commit `.env`, API keys, ZIM snapshots, model files, generated indices, uploads or evaluation artifacts.

## Data

Place one real Russian Wikipedia ZIM file under:

```text
zim/*.zim
```

The same directory is mounted read-only into:

- Kiwix as `/data`;
- API/worker as `/zim`.

Kiwix serves the full archive on `http://localhost:8083`. The RAG import indexes only the requested number of canonical non-redirect article pages. Redirects are persisted as provenance/aliases and do not count toward `WIKI_LIMIT`.

Optional XML fallback data is local-only and ignored by Git:

```text
zip/ruwiki-20260701-pages-articles-multistream.xml.bz2
zip/ruwiki-20260701-pages-articles-multistream-index.txt.bz2
```

## Run

```bash
cp .env.example .env
make up
```

Useful local URLs:

- Web UI: `http://localhost:5173`
- API health: `http://localhost:8000/health`
- API readiness: `http://localhost:8000/ready`
- Model Gateway: `http://localhost:8081`
- Mock provider: `http://localhost:8082`
- Kiwix: `http://localhost:8083`
- MinIO console: `http://localhost:9001`
- OpenSearch: `http://localhost:9200`

If GNU Make is unavailable, inspect `Makefile` and run the equivalent `uv` or `docker compose` command directly.

## Import

Small ZIM demo import:

```bash
make import-zim-small WIKI_LIMIT=10000
```

XML fallback import:

```bash
make import-wiki-small WIKI_LIMIT=10000
```

Job status and controls:

```bash
curl http://localhost:8000/api/v1/ingestion-jobs/<job_id>
curl -X POST http://localhost:8000/api/v1/ingestion-jobs/<job_id>:cancel
curl -X POST http://localhost:8000/api/v1/ingestion-jobs/<job_id>:resume
```

Checkpoints advance only after durable DB/object-storage/OpenSearch writes. Failed jobs must not publish partial content.

## Validation

Preferred stable commands:

```bash
make lint
make format-check
make typecheck
make test-unit
make test-integration
make test-e2e
make smoke
make eval
make smoke-models PROVIDER=mock
```

Direct Python equivalents commonly used in this environment:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
uv run python -m wikipediarag.cli smoke-models --provider mock
```

UI checks:

```bash
cd services/ui
pnpm lint
pnpm typecheck
pnpm format:check
pnpm build
```

Provider-backed validation:

```bash
uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://localhost:8081
uv run python -m wikipediarag.cli eval-release-gate-status --suite reviewed-wikipedia-smoke-v1 --json
uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000
```

Do not start a provider-backed release gate while API readiness is degraded.

## OpenRouter

For real `sota_mvp` runs, configure only local `.env`:

```env
MODEL_PROVIDER=openrouter
RETRIEVAL_PROFILE=sota_mvp
OPENROUTER_API_KEY=...
ZIM_DIR=/zim
KIWIX_PUBLIC_BASE_URL=http://localhost:8083
```

`sota_mvp` must not silently fall back to mock aliases or hash embeddings. Model Gateway `/health` is liveness only; `/ready` reports provider/capability degradation. In local `warn` smoke mode, a failed OpenRouter startup smoke keeps the gateway inspectable but unhealthy for OpenRouter aliases.

## llama.cpp

The local model target is three internal `llama-server` roles behind Model Gateway: chat, embeddings and rerank. The optional compose profile exists, but real use requires owner decisions on hardware, VRAM, model licenses, checksums and quality gates.

```bash
docker compose -f compose.yaml -f compose.llamacpp.yaml --profile llamacpp up -d
make smoke-models PROVIDER=llamacpp
```

Application code must use Model Gateway aliases and must not call OpenRouter or `llama-server` directly.

## Demo

After import, open `http://localhost:5173` and ask:

```text
Что такое Россия?
```

Expected behavior: Russian answer with citation IDs such as `[S1]`, clickable Kiwix source links and retrieval debugger stages for profile/query, BM25, dense, RRF, rerank, policy/context and optional Extended Search.

## Main Risks

- OpenRouter access, model churn, external provider exposure and cost.
- Current local MVP uses a seeded/default tenant; production auth/tenancy remains future work.
- Universal PDF/Office/image ingestion is not production-ready.
- Warm retrieval p95 in real eval has exceeded the target SLO and needs profiling.
- Reviewed release gate is blocked until OpenRouter access and remaining quality findings are resolved.
