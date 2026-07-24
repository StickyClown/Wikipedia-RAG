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

## Foundation endpoints

### `GET /health`

Liveness only. Must not depend on external providers.

### `GET /ready`

Checks required internal dependencies for the active profile and returns per-component status.

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

Foundation does not perform retrieval. It records that retrieval was skipped because the capability is not implemented.

## Later public endpoints

```text
POST /auth/login
GET  /knowledge-bases
POST /knowledge-bases
GET  /knowledge-bases/{id}
POST /uploads
POST /uploads/{id}/complete
GET  /jobs/{id}
POST /documents/{id}/reprocess
GET  /documents/{id}/preview
POST /chat
GET  /query-runs/{id}
GET  /query-runs/{id}/retrieval
GET  /agent-runs/{id}
```

## Cross-cutting rules

- tenant identity comes from authenticated server context;
- pagination is cursor-based for growing collections;
- create/complete operations accept an idempotency key;
- all IDs are opaque UUIDs;
- timestamps are UTC ISO-8601;
- API version changes require contract tests;
- OpenAPI becomes source of truth once Phase 0 generates it.
