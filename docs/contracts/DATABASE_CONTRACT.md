# Database contract baseline

PostgreSQL is the source of truth for control-plane state. Use UUID primary keys, UTC timestamps and explicit foreign keys.

## Phase 0 tables

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

## Later table families

- knowledge bases and versions;
- documents, versions and artifacts;
- ingestion jobs and checkpoints;
- parser templates and immutable versions;
- chunk manifests and index versions;
- retrieval runs/events;
- agent runs/steps/evidence ledger;
- citation checks;
- evaluation datasets/cases/runs;
- feedback and audit log.

## Migration rules

- migrations are forward-only after commit;
- destructive changes use expand/migrate/contract stages;
- every migration has upgrade and safe downgrade where data loss is not implied;
- migration tests run against an empty DB and the previous tagged schema;
- tenant-scoped tables require an index beginning with `tenant_id` for main access paths.
