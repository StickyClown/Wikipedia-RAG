# Runtime Services

Each component below is a runtime responsibility boundary. Some are packaged as
containers in Compose; some are external provider profiles.

## Web UI

- Responsibility: Browser UI for login, KB selection, import, upload, search,
  Deep Research, chat and retrieval debugging.
- Owns: In-memory React state only.
- Inputs: User actions, session responses, KB lists, upload files, SSE events.
- Outputs: API JSON calls, presigned upload `PUT`, rendered answers and progress.
- Dependencies: Browser, API, presigned MinIO URL.
- Must not: Store secrets, provider tokens, object keys or cross-tenant data.
- Failure behavior: Shows readiness, auth and upload errors; chat failure rendering is limited.
- Scaling or concurrency model: Static Vite app; browser-local concurrency for file hashing/upload loops.

## API

- Responsibility: Auth, tenancy, admin APIs, KBs, upload sessions, document
  lifecycle, Deep Research lifecycle, chat SSE, debug search, readiness and
  safe errors.
- Owns: Public API contracts, server-owned `ActorContext`, application sessions and authorization checks.
- Inputs: Browser/API client requests, cookies, CSRF token, OIDC callbacks.
- Outputs: JSON responses, SSE events, presigned upload URLs, durable DB writes.
- Dependencies: PostgreSQL, MinIO, OpenSearch, Model Gateway, optional OIDC provider.
- Must not: Trust client tenant filters, expose secrets/object keys/provider payloads, or call model providers directly.
- Failure behavior: Safe error envelopes for HTTP and validation errors; `/ready` reports degraded when PostgreSQL or Model Gateway readiness fails.
- Scaling or concurrency model: Stateless where practical; session and control-plane state in PostgreSQL.

## Worker

- Responsibility: Claim and process ingestion jobs, import Wikipedia, process
  document uploads, run bounded Deep Research episodes, reprocess documents and
  purge deleted documents.
- Owns: Background state transitions and publication ordering.
- Inputs: PostgreSQL ingestion jobs/items plus durable Deep Research run state.
- Outputs: PostgreSQL lifecycle/job/chunk updates, research episodes/evidence/
  claims/coverage/tool metadata, MinIO artifacts and OpenSearch documents.
- Dependencies: PostgreSQL, MinIO, OpenSearch, Kiwix, Xberg, Docling, metadata-service, Model Gateway.
- Must not: Publish failed/cancelled/parser-error jobs; pass tenant authority,
  object keys, prompts or provider payloads to parsers; or persist raw planner
  queries, prompts, chunks or provider payloads in the public research ledger.
- Failure behavior: Records failed job/item state with safe error codes; worker loop logs exceptions and continues.
- Scaling or concurrency model: Jobs and items are claimed with PostgreSQL `FOR UPDATE SKIP LOCKED`; document item concurrency and parser semaphores are bounded.

## PostgreSQL

- Responsibility: Control-plane source of truth and durable job state.
- Owns: Tenants, users, identities, sessions, groups, grants, audit events,
  KBs, sources, uploads, documents, versions, artifacts metadata, jobs, chunks,
  index versions, query runs, retrieval events and durable Deep Research state.
- Inputs: API and worker writes.
- Outputs: Control-plane reads, job claims, authorization lookups and recovery data.
- Dependencies: Persistent volume or external PostgreSQL service.
- Must not: Store plaintext passwords, plaintext session/CSRF tokens or unredacted provider secrets.
- Failure behavior: API readiness degrades; API and worker cannot operate normally.
- Scaling or concurrency model: Async SQLAlchemy/asyncpg; job claiming uses row locks.

## MinIO

- Responsibility: S3-compatible object storage for original uploads and derived artifacts.
- Owns: Uploaded original bytes, normalized JSON and parser reports.
- Inputs: Browser presigned `PUT`, worker artifact writes.
- Outputs: Worker reads and purge deletes.
- Dependencies: Persistent volume or external S3-compatible storage.
- Must not: Be exposed with production secrets from local fixtures.
- Failure behavior: Upload and worker artifact steps fail; API `/ready` does not currently check MinIO.
- Scaling or concurrency model: Single local container in Compose.

## OpenSearch

- Responsibility: Derived BM25 and vector search representation.
- Owns: Search documents with tenant and KB filters.
- Inputs: Worker bulk indexing and delete-by-query.
- Outputs: API retrieval candidates.
- Dependencies: OpenSearch data volume.
- Must not: Be treated as source of truth or queried without server-owned tenant/KB filters.
- Failure behavior: Retrieval and publication fail; API `/ready` reports the
  dependency as degraded.
- Scaling or concurrency model: Single-node local Compose setup; versioned physical indices and read/write aliases.

## Redis or Valkey

- Responsibility: Tenant-scoped search-window cache for public-search pagination.
- Owns: Rebuildable result windows and facet snapshots with a short TTL; never
  authoritative data.
- Inputs: Server-generated search fingerprints and filtered retrieval results.
- Outputs: Cached windows used to serve later cursor pages; cache failures fall
  back to uncached retrieval.
- Dependencies: Compose `valkey/valkey:8.0.2-alpine`.
- Must not: Be treated as durable storage.
- Failure behavior: Redis/Valkey errors are swallowed at the cache boundary;
  API readiness remains degraded-only because the search path is still usable.
- Scaling or concurrency model: Single local container in Compose.

## Kiwix

- Responsibility: Serve local ZIM archives for Wikipedia viewing and import provenance.
- Owns: Nothing mutable; reads ignored local `zim/*.zim` files.
- Inputs: Worker/API source URL construction and Kiwix HTTP reads.
- Outputs: ZIM pages and source URLs.
- Dependencies: Local read-only `zim/` mount.
- Must not: Be treated as mutable application storage.
- Failure behavior: ZIM import and source viewing degrade.
- Scaling or concurrency model: Single local container.

## Model Gateway

- Responsibility: Boundary for chat, embedding, rerank and Deep Research
  planner/verifier aliases.
- Owns: Alias resolution, provider readiness and provider error normalization.
- Inputs: `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`, `/v1/models`.
- Outputs: Provider-compatible results with alias metadata.
- Dependencies: `config/models.yaml`, `config/retrieval.yaml`, mock provider,
  OpenRouter proxy validation or future local providers.
- Must not: Hide provider-backed readiness failures for release gates.
- Failure behavior: `/ready` returns degraded with safe reasons; API readiness degrades.
- Scaling or concurrency model: Stateless FastAPI service.

## Model Provider Profiles

- Responsibility: Select model aliases for retrieval profiles.
- Owns: Alias-to-provider/model mapping in `config/models.yaml` and retrieval profile mapping in `config/retrieval.yaml`.
- Inputs: Effective `RETRIEVAL_PROFILE` and `MODEL_PROVIDER`.
- Outputs: Alias requirements for embedding, chat, verifier and rerank.
- Dependencies: Model Gateway.
- Must not: Let `sota_mvp` silently fall back to mock aliases.
- Failure behavior: Gateway readiness or smoke checks fail.
- Scaling or concurrency model: Configuration-driven, not a separate service.

The product target is fully local/private model usage. OpenRouter-backed Qwen
aliases are a development/proxy validation path only; they remain behind this
gateway and do not create a direct provider dependency in business code.

## Xberg

- Responsibility: Default parser for supported document formats.
- Owns: No application data.
- Inputs: Bytes/temp files over HTTP.
- Outputs: Parsed text/metadata for normalization.
- Dependencies: Worker HTTP calls and local cache volume.
- Must not: Receive MinIO credentials, raw object keys, arbitrary URLs, tenant authority, prompts or provider payloads.
- Failure behavior: Worker can route to Docling fallback when policy allows; job item fails with safe parser error otherwise.
- Scaling or concurrency model: Compose supports explicit replicas and worker endpoint pools; separate Xberg semaphore.

## Docling

- Responsibility: CPU high-quality fallback parser for low-quality, empty, scanned or layout-sensitive documents.
- Owns: No application data.
- Inputs: Bytes/temp files over HTTP.
- Outputs: Parsed text/metadata for normalization.
- Dependencies: Worker HTTP calls and local cache volume.
- Must not: Receive MinIO credentials, raw object keys, arbitrary URLs, tenant authority, prompts or provider payloads.
- Failure behavior: Job item fails with safe parser error when fallback cannot parse.
- Scaling or concurrency model: Compose supports endpoint pools; separate Docling semaphore and lower default concurrency.

## Metadata Service

- Responsibility: Fast local language/date extraction.
- Owns: No durable data.
- Inputs: Text snippets from worker normalization path.
- Outputs: Language and document-date candidates.
- Dependencies: Python service in Compose.
- Must not: Receive secrets or provider payloads.
- Failure behavior: Worker falls back to in-process deterministic extraction.
- Scaling or concurrency model: Stateless HTTP service.

## OpenTelemetry Collector

- Responsibility: Local OTLP receiver/exporter in Compose.
- Owns: Local debug export pipeline.
- Inputs: OTLP gRPC/HTTP when application instrumentation sends it.
- Outputs: Debug trace/metric export.
- Dependencies: `infra/otel/collector.yaml`.
- Must not: Persist secrets or raw document contents.
- Failure behavior: Observability is degraded; application readiness does not depend on it.
- Scaling or concurrency model: Single local collector.
