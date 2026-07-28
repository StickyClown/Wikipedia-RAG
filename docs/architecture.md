# WikipediaRag Architecture

Status: compact implementation authority
Last compacted: 2026-07-28

## Current Snapshot

WikipediaRag is a Docker-first local RAG platform for Russian Wikipedia and future user knowledge bases. The current real demo source is ZIM/libzim + Kiwix. Wikimedia XML `pages-articles` multistream remains a supported regression/local fallback.

Active milestone state is in `docs/STATUS.md`: ExecPlan 22 deterministic implementation passed validation, but provider-backed reviewed release-gate execution is blocked by OpenRouter `403 Forbidden`.

Runtime stack:

- FastAPI API, worker and Model Gateway on Python 3.12.
- React + Vite + TypeScript UI.
- PostgreSQL for control-plane state.
- OpenSearch for online BM25/vector search representation.
- MinIO/S3 contour for original/normalized artifacts.
- Redis/Valkey for queue/cache primitives.
- Kiwix for local ZIM article viewing.
- OpenTelemetry and structured retrieval events.

## Product Goals

- Answer questions using local Wikipedia and future uploaded documents.
- Attach verifiable citations to supplied evidence.
- Preserve retrieval trace and timing data for debugging and evaluation.
- Keep ingestion asynchronous, resumable and safe.
- Keep model access behind Model Gateway aliases so OpenRouter can later be replaced by local `llama.cpp` servers without business-code changes.

Explicit non-goals before measured evidence: full GraphRAG, multi-agent swarm, always-on query rewrite, learned sparse as mandatory index, ColBERT, proposition-level chunking for all documents and synchronous large-file indexing in HTTP requests.

## Service Contours

Application/API contour:

- owns chat, SSE, search debug, ingestion job APIs, readiness and future auth/tenant workflows;
- does not trust client-supplied tenant filters;
- persists `query_runs`, usage summaries and safe error metadata.

Ingestion contour:

- reads ZIM through libzim or XML multistream fallback;
- skips assets/service entries and stores redirects as provenance;
- chunks canonical articles deterministically;
- writes DB/object-storage/search state durably before checkpoints advance;
- publishes versioned OpenSearch indices through aliases only after validation.

Retrieval contour:

- applies server-owned tenant and knowledge-base filters to BM25 and vector queries;
- runs BM25 + dense retrieval, RRF, optional rerank, dedup/page quota, selective parent expansion and token-budget context packing;
- validates active knowledge-base aliases against compatible `index_versions` before search;
- fails safely with `KB_NOT_READY` instead of using silent fallback retrieval.

Model contour:

- exposes logical aliases for chat, embeddings and rerank;
- supports OpenRouter now and `llama-server` targets later;
- normalizes provider errors, timeout/retry policy, usage, readiness and telemetry.

## Model Gateway Contract

Business code must call only Model Gateway aliases. It must not import OpenRouter SDKs or call `llama-server` directly.

Gateway endpoints:

```text
GET  /health
GET  /ready
GET  /v1/models
POST /v1/chat/completions
POST /v1/embeddings
POST /v1/rerank
```

`/health` is liveness only. `/ready` reports `ok|degraded` and safe provider/capability checks. Provider diagnostics must not include API keys, prompts, document contents, raw provider bodies or stack traces.

`MODEL_GATEWAY_STARTUP_SMOKE` modes:

- `required`: startup smoke failure is fatal.
- `warn`: process stays inspectable, `/ready` is degraded and affected aliases are unhealthy.
- `off`: explicit mock/local debug only; not valid release-gate proof.

`sota_mvp` cannot silently fall back to mock aliases or hash embeddings. Mock aliases are explicit and restricted to tests/local demo profiles.

## Data Contracts

PostgreSQL is the control-plane source of truth. Key MVP tables include tenants, users, memberships, knowledge bases, model aliases, query runs, index versions, documents, ingestion jobs, chunks, retrieval events and agent runs.

Core requirements:

- tenant-scoped tables need tenant-aware access paths;
- IDs are opaque UUIDs or deterministic content/version IDs as appropriate;
- timestamps are UTC;
- migrations are forward-only after commit;
- destructive schema changes use expand/migrate/contract stages;
- embedding alias or dimensions changes create a new index version and require reindex.

`index_versions.metadata.index_contract_id` and `metadata.index_contract` bind source snapshot, physical/read alias, embedding alias/provider/model/dimensions, vector field, chunking and retrieval profile compatibility. Online search and evaluation results preserve `index_contract_id` and `run_contract_id`.

OpenSearch stores online search representation only. It is rebuildable from persisted artifacts and metadata, not the source of truth.

## API Contract

Common API errors use a safe envelope:

```json
{
  "error": {
    "code": "machine_readable_code",
    "message": "safe user-facing message",
    "request_id": "uuid",
    "details": {}
  }
}
```

Public errors must not include provider payloads, stack traces, secrets or document content.

Important endpoints:

- `GET /health` - liveness only.
- `GET /ready` - dependency readiness; API uses Model Gateway `/ready`.
- `POST /api/v1/wikipedia/zim-imports` - asynchronous ZIM import.
- `POST /api/v1/wikipedia/imports` - asynchronous XML fallback import.
- `GET /api/v1/ingestion-jobs/{job_id}` and job event/control endpoints.
- `POST /api/v1/chat` - SSE chat path.
- `POST /api/v1/search:debug` - retrieval-only debug path.

Required chat SSE event types:

```text
run.started
message.delta
usage.updated
run.completed
run.failed
```

Every SSE event includes request/run identity, sequence and timestamp. `usage.updated` carries evidence, retrieval events, answerability, citation/claim verification state, provider usage and safe `timings_ms`.

Answerability statuses are `ANSWERABLE`, `PARTIAL`, `UNANSWERABLE` and `CONFLICTING`. `UNANSWERABLE` and `CONFLICTING` produce local refusal/caveat behavior when Extended Search cannot improve coverage. Citation validation is strict by default; `warn` and `off` are explicit policy states and visible in telemetry.

## Retrieval and Answering

Normal retrieval:

```text
query normalization
-> BM25 and dense search in parallel
-> RRF fusion
-> cross-encoder rerank
-> dedup/page quota/parent expansion
-> token-budget context
-> answerability gate
-> grounded answer with citation validation
```

`POST /api/v1/search:debug` runs retrieval without generation and is used for candidate/rank/timing analysis. Conditional Extended Search starts only from `PARTIAL` or `UNANSWERABLE` answerability when enabled by profile. It may issue bounded multi-query searches and tenant-scoped neighbor lookups; final answer generation receives only the final evidence bundle.

Negative-title suppression may remove candidates only from final context, only when the query marks a quoted title as negative/distractor evidence. Raw retrieval events may still show candidates for debugging.

## Ingestion

ZIM/libzim + Kiwix is the primary local Wikipedia path:

- Kiwix serves the full ZIM mounted read-only from `./zim`;
- worker imports a bounded canonical article subset from the same archive;
- redirects do not count toward `WIKI_LIMIT` and are not chunked;
- source URLs are built from `KIWIX_PUBLIC_BASE_URL`, the Kiwix book identifier and exact `zim_entry_path`;
- checkpoints advance only after durable DB/object-storage/OpenSearch writes.

XML multistream fallback remains supported. It validates bzip2 signatures, UTF-8 index rows, `offset:page_id:title` format and monotonic non-decreasing offsets. Workers must not load full multi-gigabyte dumps into memory.

Failed jobs must not publish partial content. Reprocessing from saved canonical artifacts should not repeat expensive parsing/OCR unless explicitly requested.

## Evaluation and Release Gates

Evaluation is CLI-first and stores artifacts under ignored `artifacts/eval/`. PostgreSQL may be read for corpus/gold construction and enrichment; evaluated answer paths use the public API boundary.

Important commands:

```bash
python -m wikipediarag.cli eval-smoke --count 10
python -m wikipediarag.cli eval-generate --count 150
python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10
python -m wikipediarag.cli eval-trusted-catalog
python -m wikipediarag.cli eval-trusted-generate --count 300 --rejection-budget 30
python -m wikipediarag.cli eval-review-candidates --input <candidate_jsonl> --output-suite <suite>
python -m wikipediarag.cli eval-freeze-reviewed --suite <suite> --dev-count 20 --test-count 20
python -m wikipediarag.cli eval-release-gate --suite <suite> --api http://localhost:8000
python -m wikipediarag.cli eval-release-gate-status --suite <suite> --json
```

Long-running eval/generation/release-gate commands must expose live progress or inspectable status artifacts. Progress must use safe task IDs and counters, not full prompts, provider bodies or raw evidence packets.

Reviewed release gates run answer and retrieval evaluation only on locked reviewed dev/test rows. Test findings are blocking; dev findings are diagnostic. Gates must fail on mixed contract IDs, citation precision below threshold, unsupported claims, no-answer failures, retrieval false-positive evidence, material retrieval regressions and material p95 latency regressions.

Do not rerun provider-backed release gates while API readiness is degraded or strict OpenRouter smoke fails.

## Security and Tenancy

- Client input never directly selects `tenant_id` for authorization.
- BM25, vector search, neighbor expansion, traces, reports and caches must include server-owned tenant/access scope.
- Query/debug/export paths must not expose other tenants.
- Prompts, document text, provider bodies and secrets are redacted from normal logs.
- Uploaded files require content-based MIME detection, size/nesting/path traversal limits and isolated parsers before production use.
- Production infrastructure must not expose PostgreSQL, OpenSearch, MinIO, Redis or model servers publicly.
- Lockfiles are committed; production Docker images are pinned; model/parser artifact licenses and checksums are recorded.
- Extended Search has server-side step, wall-time, subquery and cost budgets.

## Baseline Decisions

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, asyncpg, `uv`.
- UI: React + Vite + TypeScript, `pnpm`.
- Search: OpenSearch BM25 + HNSW vectors, service-side RRF and cross-encoder rerank.
- Jobs: Redis/Valkey-backed worker model for MVP.
- Models: OpenRouter now, local `llama.cpp` target through Model Gateway aliases.
- Wikipedia: ZIM/libzim + Kiwix primary demo source; XML multistream fallback retained.
- Agents: bounded Extended Search only, not a default multi-agent system.
- Observability: retrieval-specific events plus OpenTelemetry.

## Open Owner Decisions

Before external deployment:

- deployment target, domain/TLS and reverse proxy;
- authentication provider, tenant onboarding and role matrix;
- data retention, deletion and backup requirements;
- whether user document contents may be sent to OpenRouter;
- region/residency and compliance requirements.

Before local llama.cpp:

- OS/container runtime, GPU model/count/VRAM, RAM/CPU;
- model disk footprint, licenses, checksums and quality thresholds;
- target concurrent users and query rate.

Before production release:

- RPO/RTO and restore drill requirements;
- malware scanning policy;
- observability retention/access;
- human review ownership for evaluation and citation failures;
- error budget and on-call ownership.
