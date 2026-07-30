# WikipediaRag

WikipediaRag is a Docker-first local RAG platform for Russian Wikipedia and default-tenant document knowledge bases. It is currently a local MVP, not a production multi-tenant release.

First-read source of truth for an LLM or engineer:

- `AGENTS.md` - work protocol, safety rules and implementation constraints.
- `README.md` - project map, runbook and current capabilities.
- `docs/architecture.md` - architecture contracts, service boundaries and invariants.
- `docs/STATUS.md` - latest milestone, validation evidence, blockers and next work.

## Current State

Active milestone: ExecPlan 25.1+25.2 is implemented for local async document ingestion on the seeded `default_tenant_id/default_kb_id` model. ExecPlan 21 remains complete for the reviewed Wikipedia smoke gate; the latest provider-backed reviewed gate is `completed`, `passed=true`, `blocking_failures=0`.

Implemented MVP capabilities:

- FastAPI API, background worker and Model Gateway on Python 3.12.
- React + Vite + TypeScript UI.
- PostgreSQL control plane, OpenSearch search representation, MinIO artifacts, Redis/Valkey jobs/cache, Kiwix ZIM viewing and OpenTelemetry.
- Model Gateway aliases for chat, embeddings and rerank; business code does not call OpenRouter or `llama-server` directly.
- ZIM/libzim Wikipedia ingestion with Kiwix source URLs, checkpoints, redirects as provenance and deterministic chunks.
- XML multistream Wikipedia fallback for regression/local development.
- Async document upload sessions with presigned MinIO upload, completion, background validation/parsing/chunking/embedding and published-only retrieval.
- Isolated parser containers: Xberg default parser, Docling Serve CPU fallback/high-quality parser and local metadata-service for fast language/date extraction.
- Universal document metadata: upload/system timestamps, document-date candidates, language/confidence, MIME/signature facts, parser route/report, hashes, warnings and safe public metadata.
- Hybrid retrieval: BM25, dense vectors, RRF, rerank, dedup/page quota, parent expansion, answerability and citation validation.
- Bounded Extended Search MVP for multi-query follow-up retrieval when evidence is partial or insufficient.
- Local evaluation, trusted/reviewed dataset workflow and dated release-gate reports with root run contracts and step events.
- Document corpus verification with generated fixtures plus optional pinned URL/SHA external samples.

## Runtime

Requirements:

- Docker Desktop or compatible Docker Compose runtime.
- Python 3.12 and `uv`.
- Node.js 22.14+ and `pnpm`.
- GNU Make when available; this Windows host may require direct `uv`, `pnpm` or `docker compose` equivalents.

Local URLs:

- UI: `http://localhost:5173`
- API health/readiness: `http://localhost:8000/health`, `http://localhost:8000/ready`
- Model Gateway: `http://localhost:8081`
- Mock provider: `http://localhost:8082`
- Kiwix: `http://localhost:8083`
- Metadata service: `http://localhost:8090`
- Xberg: `http://localhost:8091`
- Docling: `http://localhost:8092`
- MinIO console: `http://localhost:9001`
- OpenSearch: `http://localhost:9200`

Start:

```bash
cp .env.example .env
make up
```

If `make` is unavailable, inspect `Makefile` and run the equivalent command directly.

## Data And Ingestion

Place one real Russian Wikipedia ZIM file under ignored `zim/*.zim`. Kiwix serves the full archive; the worker imports a bounded canonical subset from the same file. Optional XML fallback dumps live under ignored `zip/`.

Small ZIM import:

```bash
make import-zim-small WIKI_LIMIT=10000
```

Document upload is asynchronous and default-tenant scoped:

```text
POST /api/v1/uploads/sessions
PUT  <upload_url>
POST /api/v1/uploads/sessions/{upload_session_id}:complete
GET  /api/v1/ingestion-jobs/{job_id}
GET  /api/v1/documents/{document_id}
GET  /api/v1/documents/{document_id}/versions
POST /api/v1/documents/{document_id}:reprocess
```

Parser routing:

- CSV, TSV, JSON and JSONL use app-owned local streaming adapters.
- Xberg handles supported document formats first.
- Docling is used for parser failure, low-quality/empty text, scanned PDF signals, layout/table/formula/read-order warnings or explicit `high_quality`.

Parser containers receive bytes/temp files over HTTP only. They never receive MinIO credentials, raw object keys, arbitrary URLs, tenant authority, prompts or provider payloads.

## Retrieval And Evaluation

Normal answer path:

```text
query
-> BM25 + dense
-> RRF
-> rerank
-> dedup/page quota/parent expansion
-> token-budget context
-> answerability
-> grounded answer with citations
```

Debug retrieval without generation:

```bash
curl -X POST http://localhost:8000/api/v1/search:debug
```

Provider-backed release gates require healthy API readiness and strict OpenRouter smoke. Do not rerun them while `/ready` is degraded.

Release-gate reports are immutable dated directories under:

```text
artifacts/eval/release-gates/<suite>/<YYYYMMDDTHHMMSSZ-suite-release-gate-short_id>/
```

## Validation Commands

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
make verify-document-upload
make verify-document-corpus
```

Windows/direct equivalents:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration
uv run python -m wikipediarag.cli smoke-models --provider mock
uv run python -m wikipediarag.cli verify-document-upload
uv run python -m wikipediarag.cli verify-document-corpus --fixture-set standard
```

UI:

```bash
cd services/ui
pnpm lint
pnpm typecheck
pnpm build
```

Document corpus verification:

```bash
uv run python -m wikipediarag.cli verify-document-corpus --fixture-set standard
uv run python -m wikipediarag.cli verify-document-corpus --fixture-set full
uv run python -m wikipediarag.cli verify-document-corpus --fixture-set smoke --include-external --skip-compose
```

External corpus bytes are downloaded to ignored `artifacts/corpora/document-corpus/`. Only URL, SHA256, license and expected assertions are tracked.

## Configuration Notes

Real OpenRouter runs use local `.env` only:

```env
MODEL_PROVIDER=openrouter
RETRIEVAL_PROFILE=sota_mvp
OPENROUTER_API_KEY=...
ZIM_DIR=/zim
KIWIX_PUBLIC_BASE_URL=http://localhost:8083
```

`sota_mvp` must not silently fall back to mock aliases or hash embeddings. Model Gateway `/health` is liveness after startup; `/ready` reports dependency degradation. During slow startup smoke, both endpoints may be temporarily unreachable until application startup completes.

Local `llama.cpp` remains an optional future target behind Model Gateway aliases:

```bash
docker compose -f compose.yaml -f compose.llamacpp.yaml --profile llamacpp up -d
make smoke-models PROVIDER=llamacpp
```

## Main Risks And Next Work

- Production auth, tenant onboarding, role model and cross-tenant acceptance are not implemented.
- Document ingestion is local/default-tenant only and still needs malware scanning, retention/deletion policy, ACL mirroring, parser autoscaling and external deployment hardening.
- Public multi-file batch creation is not exposed yet; the DB/job framework already supports independent job items.
- Fast language/date extraction is deterministic and local but heuristic.
- Warm retrieval p95 needs profiling and SLO work.
- OpenRouter-backed gates depend on provider quota, credits, latency and model behavior.
- Large/legal corpus expansion should stay manifest-driven and run outside ordinary CI unless explicitly approved.
