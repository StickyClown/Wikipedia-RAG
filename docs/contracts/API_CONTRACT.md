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

### `POST /api/v1/wikipedia/zim-imports`

Creates an asynchronous ZIM/libzim import job. The same real ZIM mounted into Kiwix is read by the worker from `/zim`.

```json
{
  "limit": 10000,
  "zim_path": null,
  "zim_filename": null
}
```

Rules:

- imports exactly `limit` canonical non-redirect article pages or fails;
- skips assets, metadata, service entries, empty/short pages and unsupported mimetypes;
- redirects are persisted as provenance/aliases but do not count toward `limit`;
- checkpoints include last completed entry index/path, scanned entry count, accepted article count, redirect count, archive id, index version, embedding alias and embedding dimensions;
- source URLs are built only from `KIWIX_PUBLIC_BASE_URL`, resolved ZIM book name and exact `zim_entry_path`.

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
  "retrieval_profile": "sota_mvp",
  "retrieval_overrides": {
    "retrieval": {"bm25": true, "dense": true, "fusion": "rrf"},
    "postprocess": {"parent_expansion": "selective"}
  },
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

The MVP performs tenant-filtered retrieval before answer generation. `usage.updated` includes retrieval evidence, stage events, citation validation and additive `timings_ms`.
`usage.updated.data.retrieval` and persisted query usage include `index_contract_id` and `run_contract_id` so a chat answer can be tied to the exact compatible index/run contract.
`usage.updated.data.retrieval.answerability` is an additive deterministic gate decision with `status`, `confidence`, `reason`, covered/missing query parts and compact safe signals. Status values are `ANSWERABLE`, `PARTIAL`, `UNANSWERABLE` and `CONFLICTING`.

Generation behavior follows the gate:

- `ANSWERABLE`: normal grounded generation.
- `PARTIAL`: generation is allowed, but the prompt instructs the model to answer only covered parts and explicitly state the coverage gap.
- `UNANSWERABLE`: when Extended Search is unavailable or still cannot cover the query, chat returns a local refusal without a generator provider call.
- `CONFLICTING`: v1 returns a safe local conflict caveat/refusal without attempting conflict resolution.

`usage.updated.data.timings_ms` is a flat numeric millisecond summary. Normal runs may include `retrieval_total`, `bm25`, `dense_total`, `dense_embedding`, `dense_search`, `fusion`, `rerank`, `context`, `generation_total`, `model_chat`, `answer_parse` and `citation_validation`. Extended Search runs may additionally include `extended_search_total` and `extended_tool_search_total`. Timing payloads must not include prompts, raw provider bodies, document text, secrets or exception details.

### `GET /api/v1/query-runs/{id}/retrieval`

Returns persisted retrieval events for a tenant-scoped query run.

### `POST /api/v1/search:debug`

Runs retrieval without answer generation and returns candidate evidence plus stage summaries. The request accepts `message` or `query`, `retrieval_profile` and validated `retrieval_overrides`.

```json
{
  "query": "Россия",
  "top_k": 5,
  "retrieval_profile": "test_mock",
  "retrieval_overrides": {
    "retrieval": {"bm25": true, "dense": false, "fusion": "none", "rerank": false},
    "postprocess": {"parent_expansion": "off"}
  }
}
```

Retrieval stage events keep the legacy `context.latency_ms` total retrieval duration and add per-stage `latency_ms` where applicable. A final `{"stage": "timings", "timings_ms": {...}}` event carries the same safe numeric retrieval timing summary for evaluation/reporting consumers.

The response includes top-level `index_contract_id` and `run_contract_id`. The `profile` retrieval event repeats those IDs with `index_version` and `read_alias`.
The response also includes top-level `answerability`. Retrieval events include a final `answerability` stage whose `decision` payload matches the top-level decision.

Conditional Extended Search is triggered only when the direct retrieval answerability status is `PARTIAL` or `UNANSWERABLE` and the active profile has `postprocess.extended_search` set to `conditional` or `always`. It is not triggered for `ANSWERABLE` or `CONFLICTING`.

If the active knowledge-base alias has no compatible `index_versions` record, search fails with HTTP `409` and safe error code `KB_NOT_READY`; it must not search a fallback alias.

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
