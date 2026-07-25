# Database contract baseline

PostgreSQL is the source of truth for control-plane state. Use UUID primary keys, UTC timestamps and explicit foreign keys.

## Implemented MVP tables

### `tenants`

- `id uuid primary key`
- `slug text unique not null`
- `name text not null`
- `created_at timestamptz not null`
- `updated_at timestamptz not null`

### `users`

- `id uuid primary key`
- `external_subject text unique null`
- `email citext unique not null`
- `display_name text null`
- `created_at`, `updated_at`

### `tenant_memberships`

- `tenant_id uuid references tenants`
- `user_id uuid references users`
- `role text check in ('owner','admin','editor','viewer')`
- composite primary key `(tenant_id, user_id)`

### `model_aliases`

- `id uuid primary key`
- `alias text unique not null`
- `provider text not null`
- `provider_model text not null`
- `operation text check in ('chat','embedding','rerank')`
- `config jsonb not null default '{}'`
- `is_enabled boolean not null`
- `capability_status jsonb not null default '{}'`
- timestamps

### `query_runs`

- `id uuid primary key`
- `tenant_id uuid references tenants not null`
- `user_id uuid references users null`
- `request_id uuid unique not null`
- `client_request_id text null`
- `mode text not null`
- `status text check in ('received','running','completed','failed','cancelled')`
- `input_text text not null` for MVP; encryption/redaction policy before production
- `model_alias text null`
- `provider_request_id text null`
- `error_code text null`
- `usage jsonb not null default '{}'`
- `trace_id text not null`
- `started_at`, `completed_at`, `created_at`

Required index: `(tenant_id, created_at desc)`.

### `knowledge_bases`

- `id uuid primary key`
- `tenant_id uuid references tenants not null`
- `name text not null`
- `active_index text not null default 'wiki-chunks-read'`
- timestamps

Required index: `(tenant_id, created_at desc)`.

### `documents`

- `id text primary key`
- `tenant_id uuid references tenants not null`
- `knowledge_base_id uuid references knowledge_bases not null`
- `source_type text not null`
- `title text not null`
- `source_uri text not null`
- `metadata jsonb not null default '{}'`
- timestamps

Wikipedia documents preserve `page_id`, `revision_id`, `timestamp`, `redirect_target` and namespace metadata.

### `ingestion_jobs`

- `id uuid primary key`
- `tenant_id uuid references tenants not null`
- `knowledge_base_id uuid references knowledge_bases not null`
- `kind text not null`
- `status text check in ('received','running','completed','failed','cancelled')`
- `config jsonb not null default '{}'`
- `progress jsonb not null default '{}'`
- `checkpoint jsonb not null default '{}'`
- `cancel_requested boolean not null default false`
- safe error fields and timestamps

Wikipedia checkpoints include index validation metadata and the last completed bzip2 stream offset.

### `chunks`

- `id text primary key`
- tenant and knowledge-base foreign keys
- `document_id text references documents`
- Wikipedia `page_id`, `revision_id`, `title`, `section_path`
- `content`, parent/prev/next chunk links, source URI/URL
- `embedding jsonb`, `content_hash`, metadata and `created_at`

Chunk IDs are deterministic for the snapshot, page/revision, section path, sequence and content hash.

### `retrieval_events`

- `id uuid primary key`
- `tenant_id uuid references tenants not null`
- optional `query_run_id`
- `trace_id`, `event_type`, `stage`, `payload jsonb`, `created_at`

Stages include BM25, dense, RRF, rerank and context.

### `agent_runs`

- `id uuid primary key`
- `tenant_id uuid references tenants not null`
- optional `query_run_id`
- `status`, `stop_reason`, `ledger jsonb`, timestamps

Used by the bounded Extended Search MVP for evidence ledger and stop reason.

## Deferred table families

- knowledge-base version publication tables beyond the active OpenSearch alias;
- parser templates and immutable parser versions;
- universal document parse artifacts and previews;
- citation-check history as a first-class table;
- evaluation datasets/cases/runs beyond the local JSON mini-eval command;
- feedback and audit log.

## Migration rules

- migrations are forward-only after commit;
- destructive changes use expand/migrate/contract stages;
- every migration has upgrade and safe downgrade where data loss is not implied;
- migration tests run against an empty DB and the previous tagged schema;
- tenant-scoped tables require an index beginning with `tenant_id` for main access paths.
