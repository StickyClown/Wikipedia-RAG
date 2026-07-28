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
  display_name text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_memberships (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('owner','admin','editor','viewer')),
  PRIMARY KEY (tenant_id, user_id)
);

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

CREATE TABLE IF NOT EXISTS query_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
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
            INSERT INTO users(id, email, display_name)
            VALUES (:user_id, 'local@example.test', 'Local User')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"user_id": settings.default_user_id},
    )
    await conn.execute(
        text(
            """
            INSERT INTO tenant_memberships(tenant_id, user_id, role)
            VALUES (:tenant_id, :user_id, 'owner')
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
