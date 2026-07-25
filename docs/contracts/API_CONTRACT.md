# API contract baseline

Prefix: `/api/v1`  
Format: JSON except uploads and SSE streams.  
All errors use one envelope.

## Common error envelope

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

Do not place provider payloads, stack traces, secrets or document content in public errors.

## Implemented endpoints

### `GET /health`

Liveness only. Must not depend on external providers.

### `GET /ready`

Checks required internal dependencies for the active profile and returns per-component status.

### `GET /api/v1/knowledge-bases`

Returns tenant-scoped knowledge base metadata for the seeded local development tenant.

### `POST /api/v1/knowledge-bases`

Creates a tenant-scoped knowledge base.

### `POST /api/v1/wikipedia/imports`

Creates an asynchronous Wikimedia XML `pages-articles` import job.

```json
{
  "limit": 10000,
  "xml_path": null,
  "index_path": null,
  "snapshot_id": null,
  "reset": false
}
```

Default paths point to mounted local data assets:

```text
/data/ruwiki-20260701-pages-articles-multistream.xml.bz2
/data/ruwiki-20260701-pages-articles-multistream-index.txt.bz2
```

The worker validates bzip2 signatures, UTF-8 index rows, `offset:page_id:title`, monotonic non-decreasing offsets and sampled unique bzip2 stream signatures before import.

### `GET /api/v1/ingestion-jobs/{job_id}`

Returns status, config, progress, checkpoint and safe error metadata.

### `GET /api/v1/ingestion-jobs/{job_id}/events`

Streams `job.progress` SSE events until the job reaches `completed`, `failed` or `cancelled`.

### `POST /api/v1/ingestion-jobs/{job_id}:cancel`

Requests cooperative cancellation. Workers persist checkpoint state before stopping.

### `POST /api/v1/ingestion-jobs/{job_id}:resume`

Moves a failed/cancelled job back to resumable `received` state without deleting checkpoints.

### `POST /api/v1/uploads`

Uploads a small UTF-8 text document in local MVP mode and indexes deterministic chunks. Binary Office/PDF parsing remains out of local MVP scope.

### `POST /api/v1/chat`

Initial request:

```json
{
  "message": "Explain reciprocal rank fusion",
  "conversation_id": null,
  "knowledge_base_ids": [],
  "mode": "normal",
  "stream": true,
  "client_request_id": "optional-idempotency-correlation-id"
}
```

Initial response is `text/event-stream` when `stream=true`.

Required SSE event types:

```text
run.started
message.delta
usage.updated
run.completed
run.failed
```

Every event includes `request_id`, `query_run_id`, `sequence` and `timestamp`.

The MVP performs tenant-filtered retrieval before answer generation. `usage.updated` includes retrieval evidence, stage events and citation validation.

### `GET /api/v1/query-runs/{id}/retrieval`

Returns persisted retrieval events for a tenant-scoped query run.

### `POST /api/v1/search:debug`

Runs retrieval without answer generation and returns candidate evidence plus stage summaries. The request accepts `message` or `query`.

```json
{
  "query": "Россия",
  "top_k": 5
}
```

## Deferred public endpoints

- production auth/OIDC and role-management APIs;
- universal document parse/preview/reprocess APIs for PDF, Office and images;
- full agent-run inspection APIs beyond persisted local MVP ledger;
- admin model-alias management APIs.

## Cross-cutting rules

- tenant identity comes from authenticated server context;
- pagination is cursor-based for growing collections;
- create/complete operations accept an idempotency key;
- all IDs are opaque UUIDs;
- timestamps are UTC ISO-8601;
- API version changes require contract tests;
- OpenAPI and contract tests must be updated when an endpoint changes.
