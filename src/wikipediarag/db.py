from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from wikipediarag.config import Settings, get_settings

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS tenants (
  id uuid PRIMARY KEY,
  slug text UNIQUE NOT NULL,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY,
  external_subject text UNIQUE NULL,
  email citext UNIQUE NOT NULL,
  username citext UNIQUE NULL,
  display_name text NULL,
  platform_role text NOT NULL DEFAULT 'USER',
  password_hash text NULL,
  password_change_required boolean NOT NULL DEFAULT false,
  is_disabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key;
ALTER TABLE users ADD COLUMN IF NOT EXISTS username citext UNIQUE NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS platform_role text NOT NULL DEFAULT 'USER';
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_change_required boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_disabled boolean NOT NULL DEFAULT false;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_platform_role_check;
ALTER TABLE users ADD CONSTRAINT users_platform_role_check CHECK (platform_role IN ('PLATFORM_ADMIN','USER'));

CREATE TABLE IF NOT EXISTS tenant_memberships (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('owner','admin','editor','viewer')),
  PRIMARY KEY (tenant_id, user_id)
);
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_role_check;
UPDATE tenant_memberships
SET role = CASE
  WHEN role IN ('owner','admin','TENANT_ADMIN') THEN 'TENANT_ADMIN'
  ELSE 'MEMBER'
END
WHERE role NOT IN ('TENANT_ADMIN','MEMBER');
ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_role_check CHECK (role IN ('TENANT_ADMIN','MEMBER'));

CREATE TABLE IF NOT EXISTS model_aliases (
  id uuid PRIMARY KEY,
  alias text UNIQUE NOT NULL,
  provider text NOT NULL,
  provider_model text NOT NULL,
  operation text NOT NULL CHECK (operation IN ('chat','embedding','rerank')),
  config jsonb NOT NULL DEFAULT '{}',
  is_enabled boolean NOT NULL DEFAULT true,
  capability_status jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL,
  active_index text NOT NULL DEFAULT 'wiki-chunks-read',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_knowledge_bases_tenant ON knowledge_bases(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS auth_identities (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id),
  issuer text NOT NULL,
  subject text NOT NULL,
  identity_key text UNIQUE NOT NULL,
  provider_type text NOT NULL CHECK (provider_type IN ('LOCAL','OIDC')),
  username citext NULL,
  email citext NULL,
  claims jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (issuer, subject)
);
CREATE INDEX IF NOT EXISTS ix_auth_identities_user ON auth_identities(user_id);

CREATE TABLE IF NOT EXISTS auth_sessions (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id),
  session_token_hash text UNIQUE NOT NULL,
  csrf_token_hash text NOT NULL,
  active_tenant_id uuid NULL REFERENCES tenants(id),
  authentication_method text NOT NULL CHECK (authentication_method IN ('local','oidc','test')),
  rotation_counter int NOT NULL DEFAULT 0,
  server_side_tokens jsonb NOT NULL DEFAULT '{}',
  expires_at timestamptz NOT NULL,
  idle_expires_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_active ON auth_sessions(expires_at, idle_expires_at)
  WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_oidc_flows (
  id uuid PRIMARY KEY,
  state_hash text UNIQUE NOT NULL,
  nonce_hash text NOT NULL,
  code_verifier_hash text NOT NULL,
  redirect_uri text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_auth_oidc_flows_expiry ON auth_oidc_flows(expires_at);

CREATE TABLE IF NOT EXISTS groups (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  group_type text NOT NULL CHECK (group_type IN ('LOCAL','OIDC')),
  name text NOT NULL,
  external_id text NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, group_type, name),
  UNIQUE (tenant_id, group_type, external_id)
);
CREATE INDEX IF NOT EXISTS ix_groups_tenant ON groups(tenant_id, group_type, name);

CREATE TABLE IF NOT EXISTS group_memberships (
  group_id uuid NOT NULL REFERENCES groups(id),
  user_id uuid NOT NULL REFERENCES users(id),
  membership_type text NOT NULL CHECK (membership_type IN ('LOCAL','OIDC')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (group_id, user_id, membership_type)
);
CREATE INDEX IF NOT EXISTS ix_group_memberships_user ON group_memberships(user_id, membership_type);

CREATE TABLE IF NOT EXISTS knowledge_base_grants (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  subject_type text NOT NULL CHECK (subject_type IN ('USER','GROUP')),
  subject_id text NOT NULL,
  role text NOT NULL CHECK (role IN ('VIEWER','EDITOR','MANAGER','OWNER')),
  created_by_user_id uuid NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, knowledge_base_id, subject_type, subject_id)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_base_grants_subject
  ON knowledge_base_grants(tenant_id, subject_type, subject_id);

CREATE TABLE IF NOT EXISTS audit_events (
  id uuid PRIMARY KEY,
  tenant_id uuid NULL REFERENCES tenants(id),
  actor_user_id uuid NULL REFERENCES users(id),
  actor_session_id uuid NULL REFERENCES auth_sessions(id),
  request_id uuid NOT NULL,
  trace_id text NOT NULL,
  action text NOT NULL,
  target_type text NOT NULL,
  target_id text NULL,
  outcome text NOT NULL CHECK (outcome IN ('success','failure')),
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_events_tenant ON audit_events(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_events_actor ON audit_events(actor_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS index_versions (
  id text PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  source_type text NOT NULL,
  snapshot_id text NOT NULL,
  retrieval_profile text NOT NULL,
  embedding_alias text NOT NULL,
  embedding_dimensions int NOT NULL,
  physical_index text NOT NULL,
  read_alias text NOT NULL,
  write_alias text NOT NULL,
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_index_versions_tenant ON index_versions(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS documents (
  id text PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  source_type text NOT NULL,
  title text NOT NULL,
  source_uri text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_documents_tenant ON documents(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_sources (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  kind text NOT NULL,
  name text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','failed')),
  config jsonb NOT NULL DEFAULT '{}',
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_knowledge_sources_tenant ON knowledge_sources(tenant_id, knowledge_base_id, kind);

CREATE TABLE IF NOT EXISTS upload_batches (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','running','completed','failed','cancelled')),
  total_items int NOT NULL DEFAULT 0,
  completed_items int NOT NULL DEFAULT 0,
  failed_items int NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_upload_batches_tenant ON upload_batches(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS upload_sessions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  batch_id uuid NULL REFERENCES upload_batches(id),
  status text NOT NULL DEFAULT 'created'
    CHECK (status IN ('created','uploaded','completed','expired','failed','cancelled')),
  filename text NOT NULL,
  content_type text NOT NULL,
  size_bytes bigint NOT NULL,
  checksum_sha256 text NOT NULL,
  object_key text NOT NULL,
  parser_profile text NOT NULL DEFAULT 'standard',
  metadata jsonb NOT NULL DEFAULT '{}',
  expires_at timestamptz NOT NULL,
  upload_completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_upload_sessions_tenant ON upload_sessions(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_versions (
  id text PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(id),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  version_ordinal int NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'received'
    CHECK (status IN ('received','validating','parsing','normalized','indexing','published','failed','cancelled')),
  content_hash text NOT NULL,
  normalized_hash text NULL,
  original_artifact_key text NOT NULL,
  normalized_artifact_key text NULL,
  parser_route text NULL,
  parser_name text NULL,
  parser_version text NULL,
  parser_options jsonb NOT NULL DEFAULT '{}',
  source_metadata jsonb NOT NULL DEFAULT '{}',
  extracted_metadata jsonb NOT NULL DEFAULT '{}',
  public_metadata jsonb NOT NULL DEFAULT '{}',
  validation jsonb NOT NULL DEFAULT '{}',
  warnings jsonb NOT NULL DEFAULT '[]',
  uploaded_at timestamptz NOT NULL DEFAULT now(),
  upload_completed_at timestamptz NULL,
  ingested_at timestamptz NULL,
  published_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_document_versions_tenant ON document_versions(tenant_id, knowledge_base_id, document_id);

CREATE TABLE IF NOT EXISTS document_artifacts (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  document_id text NOT NULL REFERENCES documents(id),
  document_version_id text NOT NULL REFERENCES document_versions(id),
  kind text NOT NULL CHECK (kind IN ('original','normalized','parser_report')),
  object_key text NOT NULL,
  content_type text NOT NULL,
  size_bytes bigint NOT NULL,
  checksum_sha256 text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_document_artifacts_tenant ON document_artifacts(tenant_id, document_version_id, kind);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  kind text NOT NULL,
  status text NOT NULL CHECK (status IN ('received','running','completed','failed','cancelled')),
  config jsonb NOT NULL DEFAULT '{}',
  progress jsonb NOT NULL DEFAULT '{}',
  checkpoint jsonb NOT NULL DEFAULT '{}',
  error_code text NULL,
  error_message text NULL,
  cancel_requested boolean NOT NULL DEFAULT false,
  started_at timestamptz NULL,
  completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ingestion_jobs_tenant ON ingestion_jobs(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ingestion_job_items (
  id uuid PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES ingestion_jobs(id),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  document_id text NULL REFERENCES documents(id),
  document_version_id text NULL REFERENCES document_versions(id),
  upload_session_id uuid NULL REFERENCES upload_sessions(id),
  status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','running','completed','failed','cancelled')),
  stage text NOT NULL DEFAULT 'received',
  attempts int NOT NULL DEFAULT 0,
  progress jsonb NOT NULL DEFAULT '{}',
  checkpoint jsonb NOT NULL DEFAULT '{}',
  error_code text NULL,
  error_message text NULL,
  claimed_at timestamptz NULL,
  completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(job_id, document_version_id)
);
CREATE INDEX IF NOT EXISTS ix_ingestion_job_items_claim
  ON ingestion_job_items(job_id, status, created_at);
CREATE INDEX IF NOT EXISTS ix_ingestion_job_items_tenant
  ON ingestion_job_items(tenant_id, knowledge_base_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chunks (
  id text PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  document_id text NOT NULL REFERENCES documents(id),
  page_id bigint NULL,
  revision_id bigint NULL,
  title text NOT NULL,
  section_path text[] NOT NULL DEFAULT ARRAY[]::text[],
  content text NOT NULL,
  parent_chunk_id text NULL,
  prev_chunk_id text NULL,
  next_chunk_id text NULL,
  source_uri text NOT NULL,
  source_url text NOT NULL,
  embedding jsonb NOT NULL DEFAULT '[]',
  content_hash text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_chunks_tenant ON chunks(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_chunks_page ON chunks(tenant_id, page_id);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS document_version_id text NULL REFERENCES document_versions(id);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS chunk_ordinal int NULL;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS locator jsonb NOT NULL DEFAULT '{}';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS publication_status text NOT NULL DEFAULT 'published';

CREATE TABLE IF NOT EXISTS query_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NULL REFERENCES knowledge_bases(id),
  user_id uuid NULL REFERENCES users(id),
  request_id uuid UNIQUE NOT NULL,
  client_request_id text NULL,
  mode text NOT NULL,
  status text NOT NULL CHECK (status IN ('received','running','completed','failed','cancelled')),
  input_text text NOT NULL,
  answer text NULL,
  model_alias text NULL,
  provider_request_id text NULL,
  error_code text NULL,
  usage jsonb NOT NULL DEFAULT '{}',
  trace_id text NOT NULL,
  started_at timestamptz NULL,
  completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_query_runs_tenant ON query_runs(tenant_id, created_at DESC);
ALTER TABLE query_runs ADD COLUMN IF NOT EXISTS knowledge_base_id uuid NULL REFERENCES knowledge_bases(id);

CREATE TABLE IF NOT EXISTS retrieval_events (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  query_run_id uuid NULL REFERENCES query_runs(id),
  trace_id text NOT NULL,
  event_type text NOT NULL,
  stage text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_retrieval_events_tenant ON retrieval_events(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  query_run_id uuid NULL REFERENCES query_runs(id),
  status text NOT NULL,
  stop_reason text NULL,
  ledger jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_tenant ON agent_runs(tenant_id, created_at DESC);
"""


_engine: AsyncEngine | None = None
_engine_url: str | None = None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _engine_url
    resolved = settings or get_settings()
    if _engine is None or _engine_url != resolved.database_url:
        _engine = create_async_engine(resolved.database_url, pool_pre_ping=True)
        _engine_url = resolved.database_url
    return _engine


@asynccontextmanager
async def connect(settings: Settings | None = None) -> AsyncIterator[AsyncConnection]:
    engine = get_engine(settings)
    async with engine.begin() as conn:
        yield conn


async def ensure_schema(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    engine = get_engine(resolved)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('wikipediarag_schema_v1'))"))
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        await seed_development_data(conn, resolved)


async def seed_development_data(conn: AsyncConnection, settings: Settings) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO tenants(id, slug, name)
            VALUES (:tenant_id, 'local', 'Local development tenant')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"tenant_id": settings.default_tenant_id},
    )
    await conn.execute(
        text(
            """
            INSERT INTO users(id, email, username, display_name, platform_role)
            VALUES (:user_id, 'local@example.test', 'local', 'Local User', 'USER')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"user_id": settings.default_user_id},
    )
    await conn.execute(
        text(
            """
            INSERT INTO tenant_memberships(tenant_id, user_id, role)
            VALUES (:tenant_id, :user_id, 'TENANT_ADMIN')
            ON CONFLICT (tenant_id, user_id) DO NOTHING
            """
        ),
        {"tenant_id": settings.default_tenant_id, "user_id": settings.default_user_id},
    )
    await conn.execute(
        text(
            """
            INSERT INTO knowledge_bases(id, tenant_id, name)
            VALUES (:kb_id, :tenant_id, 'Russian Wikipedia')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"kb_id": settings.default_kb_id, "tenant_id": settings.default_tenant_id},
    )
    await conn.execute(
        text(
            """
            INSERT INTO auth_identities(id, user_id, issuer, subject, identity_key, provider_type, username, email)
            VALUES (
              '66666666-6666-4666-8666-666666666666',
              :user_id,
              'local',
              :subject,
              :identity_key,
              'LOCAL',
              'local',
              'local@example.test'
            )
            ON CONFLICT (issuer, subject) DO NOTHING
            """
        ),
        {
            "user_id": settings.default_user_id,
            "subject": str(settings.default_user_id),
            "identity_key": f"local:{settings.default_user_id}",
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO knowledge_base_grants(
              id, tenant_id, knowledge_base_id, subject_type, subject_id, role, created_by_user_id
            )
            VALUES (
              '55555555-5555-4555-8555-555555555555',
              :tenant_id,
              :kb_id,
              'USER',
              :subject_id,
              'OWNER',
              :user_id
            )
            ON CONFLICT (tenant_id, knowledge_base_id, subject_type, subject_id) DO NOTHING
            """
        ),
        {
            "tenant_id": settings.default_tenant_id,
            "kb_id": settings.default_kb_id,
            "user_id": settings.default_user_id,
            "subject_id": str(settings.default_user_id),
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO knowledge_sources(id, tenant_id, knowledge_base_id, kind, name, config, metadata)
            VALUES (
              '44444444-4444-4444-8444-444444444444',
              :tenant_id,
              :kb_id,
              'kiwix_zim',
              'Kiwix Russian Wikipedia',
              CAST(:config AS jsonb),
              CAST(:metadata AS jsonb)
            )
            ON CONFLICT (id) DO UPDATE
            SET config = EXCLUDED.config,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """
        ),
        {
            "tenant_id": settings.default_tenant_id,
            "kb_id": settings.default_kb_id,
            "config": json_dumps(
                {
                    "zim_dir_alias": "default_zim_mount",
                    "kiwix_public_base_url": settings.kiwix_public_base_url,
                    "kiwix_book_name": settings.kiwix_book_name,
                }
            ),
            "metadata": json_dumps({"source_contract": "knowledge_source_v1"}),
        },
    )
