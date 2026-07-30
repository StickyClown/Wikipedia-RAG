# WikipediaRag Architecture

Status: compact implementation authority
Last compacted: 2026-07-30

## Current Snapshot

WikipediaRag is a Docker-first local RAG platform for Russian Wikipedia plus default-tenant uploaded documents. The system is built around asynchronous ingestion, tenant-scoped retrieval, reproducible artifacts and a Model Gateway boundary for all model calls. XML multistream fallback remains supported for regression/local imports.

Current milestone is in `docs/STATUS.md`: ExecPlan 25.1+25.2 local async document ingestion is implemented without ExecPlan 24 production auth/onboarding. ExecPlan 21 reviewed Wikipedia gate remains complete; latest provider-backed gate is `passed=true`, `blocking_failures=0`.

Runtime services:

- API and worker: FastAPI/Python 3.12.
- UI: React + Vite + TypeScript.
- Control plane: PostgreSQL.
- Search representation: OpenSearch BM25 + vector fields.
- Artifacts: MinIO/S3-compatible storage.
- Jobs/cache: Redis/Valkey.
- Wikipedia source viewing: Kiwix serving local ZIM.
- Model access: Model Gateway for chat, embeddings and rerank aliases.
- Parser services: Xberg, Docling Serve CPU and metadata-service over HTTP.
- Observability: OpenTelemetry plus structured retrieval/eval events.

## Core Invariants

- API and workers are stateless where practical.
- Client input never directly controls `tenant_id`; tenant and KB scope are injected server-side.
- Every persistent entity and search document carries tenant scope where applicable.
- Original files, normalized documents and parser reports live in object storage; OpenSearch is rebuildable and is not source of truth.
- Ingestion and release-gate reports are idempotent, resumable and timestamped in UTC.
- Failed or cancelled ingestion jobs do not publish searchable chunks.
- Business code must call Model Gateway aliases only; it must not call OpenRouter or `llama-server` directly.
- Normal logs/reports must redact secrets, prompts, provider payloads, raw document text, parser stderr and storage object keys.

## Service Contours

API contour:

- owns chat/SSE, readiness, search debug, upload sessions, document metadata and ingestion job control;
- returns safe errors and safe public metadata only;
- persists query runs, retrieval events, usage summaries and safe failure metadata.

Worker contour:

- imports ZIM/XML Wikipedia sources with checkpoints;
- processes upload job items independently with bounded claiming;
- validates uploaded bytes before parser routing;
- stages chunks before OpenSearch writes and publishes only after validation succeeds.

Model Gateway contour:

- exposes `GET /health`, `GET /ready`, `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/embeddings`, `POST /v1/rerank`;
- normalizes provider errors, timeouts, usage and readiness;
- supports OpenRouter now and local `llama.cpp` later through the same alias contract.

Parser contour:

- Xberg is the fast/default parser for supported document formats.
- Docling is the high-quality fallback for low-quality, empty, scanned or layout-sensitive cases.
- metadata-service performs fast local language/date extraction.
- Parser services receive bytes/temp files over HTTP only, never MinIO credentials, raw object keys, arbitrary URLs, tenant authority, prompts or provider payloads.

## Data Contracts

PostgreSQL is the control-plane source of truth. Key tables include tenants, users, memberships, knowledge bases, knowledge sources, upload batches, upload sessions, document versions, document artifacts, ingestion jobs, ingestion job items, index versions, documents, chunks, query runs, retrieval events and agent runs.

Important contracts:

- `index_versions.metadata.index_contract_id` binds source snapshot, aliases, embedding provider/model/dimensions, vector field, chunking and retrieval-profile compatibility.
- Answer/eval rows use one root `run_contract_id` for the configured run and child retrieval/tool contract IDs for diagnostic substeps.
- `execution_path` and `path_selection_reason` record whether the row used normal, harness, retrieval-only, extended or fallback behavior.
- Uploaded documents use an app-owned `NormalizedDocument` schema with stable text/table blocks, source locators, hashes, parser report metadata, warnings and provenance at page/slide/sheet/cell/row/JSON Pointer granularity.
- `document_versions` carries universal metadata: upload/system timestamps, source dates, document-date candidates/source/confidence, detected language/confidence/alternatives, MIME/signature facts, parser route/version/options, hashes, warnings and safe public metadata.

Forward-only schema changes are made through `ensure_schema` for this MVP. After a migration is committed, destructive schema changes require expand/migrate/contract planning.

## Public API Contracts

Common public error shape:

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

Important endpoints:

- `GET /health` - liveness only after app startup.
- `GET /ready` - dependency readiness; API depends on Model Gateway `/ready`.
- `POST /api/v1/wikipedia/zim-imports` - async ZIM import.
- `POST /api/v1/wikipedia/imports` - async XML fallback import.
- `POST /api/v1/uploads/sessions` - create upload session and presigned object URL.
- `POST /api/v1/uploads/sessions/{id}:complete` - create document/version/job records after object durability.
- `GET /api/v1/documents/{document_id}` - safe document metadata.
- `GET /api/v1/documents/{document_id}/versions` - safe version metadata.
- `POST /api/v1/documents/{document_id}:reprocess` - enqueue reprocess for current version.
- `GET /api/v1/ingestion-jobs/{job_id}` and job control endpoints.
- `POST /api/v1/search:debug` - retrieval-only debug path.
- `POST /api/v1/chat` - SSE answer path.

Upload session response:

```json
{"upload_session_id": "...", "upload_url": "...", "expires_at": "...", "required_headers": {}}
```

Upload complete response:

```json
{"document_id": "...", "document_version_id": "...", "job_id": "...", "status": "received"}
```

Job progress exposes safe counters only: stage, bytes received, document totals, parser route, staged/published chunks, timings, terminal state and safe error code.

Chat SSE event types are `run.started`, `message.delta`, `usage.updated`, `run.completed` and `run.failed`. `run.failed` carries safe stage/code/retryability/attempt/trace metadata, and may include safe retrieval snapshots without document text.

## Ingestion Flows

Wikipedia ZIM flow:

ZIM/libzim + Kiwix is the primary local Wikipedia path.

```text
Kiwix serves full local ZIM
-> worker reads canonical non-redirect pages with libzim
-> redirects stored as provenance
-> chunks generated deterministically
-> OpenSearch index version written
-> alias published after validation
```

Document upload flow:

```text
create upload session
-> client PUT to MinIO presigned URL
-> complete session
-> worker validates bytes/signatures/safety
-> metadata-service extracts language/date from available text
-> route to local adapter, Xberg or Docling
-> normalize to app-owned contract
-> chunk with locators
-> embed
-> stage chunks
-> write OpenSearch
-> publish DB chunks and document version
```

Upload validation rejects zero-byte files, oversized objects, archives, renamed executables, extension/signature mismatches, encrypted PDF/Office, macro-enabled Office, deeply nested JSON and remote-resource HTML.

CSV, TSV, JSON and JSONL stay in local streaming adapters. XLSX/PDF/DOCX/PPTX use Xberg first unless the quality route requires Docling.

XML multistream fallback validates UTF-8 index rows and monotonic non-decreasing offsets without loading full multi-gigabyte dumps into memory.

## Retrieval And Answering

Normal retrieval:

```text
query normalization
-> BM25 and dense search
-> RRF
-> rerank
-> dedup/page quota/parent expansion
-> token-budget context
-> answerability
-> grounded generation
-> citation validation
```

Server-owned tenant/KB filters are applied to all BM25, vector, neighbor, debug and export paths. If an active KB has no compatible published index, search fails safely with `KB_NOT_READY`.

Extended Search is bounded and conditional. It starts only when profile policy allows it and answerability is `PARTIAL` or `UNANSWERABLE`.

## Evaluation And Release Gates

Evaluation is CLI-first and writes ignored artifacts under `artifacts/eval/`. Release gates run only against locked reviewed dev/test rows.

Important commands:

```bash
python -m wikipediarag.cli eval-smoke --count 10
python -m wikipediarag.cli eval-generate --count 150
python -m wikipediarag.cli eval-run --suite generated-wikipedia-v1 --batch-size 6
python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10
python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000
python -m wikipediarag.cli eval-release-gate-status --suite reviewed-wikipedia-smoke-v1 --json
python -m wikipediarag.cli verify-document-upload
python -m wikipediarag.cli verify-document-corpus --fixture-set standard
```

Long-running eval/generation/release-gate commands must expose live progress or inspectable status artifacts. When supported, use bounded `--batch-size` or `--concurrency`.

Release-gate report directories are immutable and dated:

```text
YYYYMMDDTHHMMSSZ-<suite>-release-gate-<short_id>
```

The gate blocks on mixed root contract IDs, test citation precision below threshold, unsupported claims, no-answer failures, false-positive evidence, material retrieval regressions and material p95 latency regressions. Dev findings are diagnostic unless explicitly promoted.

## Corpus Verification

`verify-document-corpus` is the ingestion-quality gate for varied data:

- `smoke`: small CSV/PDF plus one negative HTML case.
- `standard`: generated TXT/MD/HTML/CSV/TSV/JSON/JSONL/PDF plus unsafe negative fixtures.
- `full`: standard plus generated DOCX/PPTX/XLSX.
- `--include-external`: downloads pinned manifest entries into ignored `artifacts/corpora/document-corpus/`.

Tracked external corpus manifests contain URL, SHA256, license, source and expected assertions only. Large archives and raw external documents are not committed.

## Open Decisions

Before external deployment:

- auth provider, tenant onboarding, roles and ACL mirroring;
- retention, deletion, backup and restore policy;
- malware scanning policy and parser sandbox hardening;
- whether user document contents may be sent to external model providers;
- domain/TLS/reverse proxy and environment isolation;
- observability retention and on-call ownership.

Before local `llama.cpp`:

- GPU/CPU/RAM sizing and concurrency targets;
- model choices, licenses, checksums and quality thresholds;
- release-gate acceptance criteria for local aliases.

Before larger corpus expansion:

- legal corpus cadence and storage budget;
- which external corpora are CI, nightly or manual only;
- expected metadata/citation assertions per source family.
