# Runtime Services

This document records responsibility and failure boundaries. Deployment details
are in [deployment-and-operations.md](deployment-and-operations.md).

| Component | Owns | Main dependencies | Failure contract |
| --- | --- | --- | --- |
| Web UI | Browser state and rendered interaction | API, presigned object upload | Shows safe loading, forbidden, degraded and failed states; never grants authority |
| API | Public HTTP/SSE schemas, sessions, `ActorContext`, authorization and lifecycle commands | PostgreSQL, object storage, search, Model Gateway | Returns typed safe errors; `/ready` reports required dependency failures |
| Worker | Ingestion, publication, reconciliation, purge and Deep Research transitions | PostgreSQL, object storage, search, parsers, Model Gateway | Persists safe terminal/retry state; failed/cancelled work is not published |
| PostgreSQL | Control plane, ACL, lifecycle, chunks, query events and research state | Durable database | API/worker operations stop or degrade; no alternate authority |
| Object storage | Original uploads, normalized documents and parser artifacts | S3-compatible service | Upload/reprocess/purge fails without losing DB authority |
| OpenSearch | BM25/vector candidate projection | Published PostgreSQL state | Retrieval may fail or omit results; stale candidates are DB-confirmed before exposure |
| Redis/Valkey | Short-lived search windows and facets | Rebuildable cache | Falls back to uncached retrieval |
| Kiwix | Read-only ZIM access and source provenance | Operator-supplied ZIM | Wikipedia import/viewing is unavailable; other sources remain usable |
| Parser services | Parsing and metadata extraction | Bytes/text supplied by worker | Worker uses configured fallback or records a safe item failure |
| Model Gateway | Chat, embedding, rerank and token-counting operations; aliases, adapters and safe errors | Active model revision and configured endpoints | Unhealthy required aliases degrade readiness and fail calls explicitly |
| Model endpoints | Operation execution | Remote or local compatible API | Endpoint-specific failure terminates at the Gateway boundary |
| OpenTelemetry collector | Optional telemetry export | OTLP configuration | Observability degrades without becoming application authority |

## Service Rules

- API and UI never call model providers directly; business code uses
  `ModelClient` aliases through Model Gateway.
- Any endpoint implementation is acceptable when it satisfies the configured
  operation contract and readiness check. No local runtime is mandatory.
- Parsers receive only bounded content needed for parsing, never object-storage
  credentials, tenant authority, prompts or provider payloads.
- Worker claims and retries are bounded. Concurrent transitions use database
  locking, leases or compare-and-set.
- OpenSearch, Redis and browser state are projections, not sources of truth.
