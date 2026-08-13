from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from wikipediarag.config import Settings, get_settings

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

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
ALTER TABLE knowledge_base_grants ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}';
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
ALTER TABLE index_versions ADD COLUMN IF NOT EXISTS identity_scope text NOT NULL DEFAULT 'legacy';
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
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_document_id text NULL;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS identity_scope text NOT NULL DEFAULT 'legacy';
CREATE INDEX IF NOT EXISTS ix_documents_tenant ON documents(tenant_id, created_at DESC);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS lifecycle_state text NOT NULL DEFAULT 'active';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS purge_after timestamptz NULL;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_by_user_id uuid NULL REFERENCES users(id);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS deletion_reason text NULL;
CREATE INDEX IF NOT EXISTS ix_documents_purge
  ON documents(tenant_id, purge_after)
  WHERE lifecycle_state IN ('deleting','purge_failed');

CREATE TABLE IF NOT EXISTS legacy_id_mappings (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  entity_kind text NOT NULL CHECK (entity_kind IN ('document','chunk','index_version')),
  legacy_id text NOT NULL,
  scoped_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, knowledge_base_id, entity_kind, legacy_id),
  UNIQUE (tenant_id, knowledge_base_id, entity_kind, scoped_id)
);
CREATE INDEX IF NOT EXISTS ix_legacy_id_mappings_scoped
  ON legacy_id_mappings(tenant_id, knowledge_base_id, entity_kind, scoped_id);

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
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS encrypted_credentials jsonb NOT NULL DEFAULT '{}';
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS refresh_interval_seconds int NULL;
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS sync_cursor jsonb NOT NULL DEFAULT '{}';
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS last_sync_run_id uuid NULL;
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS last_sync_status text NULL;
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS last_synced_at timestamptz NULL;
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS next_sync_at timestamptz NULL;
CREATE INDEX IF NOT EXISTS ix_knowledge_sources_due
  ON knowledge_sources(tenant_id, next_sync_at)
  WHERE status = 'active' AND refresh_interval_seconds IS NOT NULL;

CREATE TABLE IF NOT EXISTS source_sync_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  source_id uuid NOT NULL REFERENCES knowledge_sources(id),
  job_id uuid NULL,
  mode text NOT NULL CHECK (mode IN ('full','incremental','healthcheck')),
  status text NOT NULL CHECK (status IN ('received','running','completed','failed','cancelled')),
  cursor_before jsonb NOT NULL DEFAULT '{}',
  cursor_after jsonb NOT NULL DEFAULT '{}',
  checkpoint jsonb NOT NULL DEFAULT '{}',
  stats jsonb NOT NULL DEFAULT '{}',
  error_code text NULL,
  error_message text NULL,
  started_at timestamptz NULL,
  completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_source_sync_runs_source
  ON source_sync_runs(tenant_id, knowledge_base_id, source_id, created_at DESC);

CREATE TABLE IF NOT EXISTS source_document_states (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  source_id uuid NOT NULL REFERENCES knowledge_sources(id),
  external_id text NOT NULL,
  source_uri text NOT NULL,
  source_url text NOT NULL,
  title text NOT NULL,
  source_version text NOT NULL,
  content_hash text NOT NULL,
  document_id text NULL REFERENCES documents(id),
  document_version_id text NULL,
  last_sync_run_id uuid NULL REFERENCES source_sync_runs(id),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
  metadata jsonb NOT NULL DEFAULT '{}',
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz NULL,
  tombstone_version text NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, knowledge_base_id, source_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_source_document_states_document
  ON source_document_states(tenant_id, knowledge_base_id, document_id);

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
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS lifecycle_state text NOT NULL DEFAULT 'active';
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL;
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS purge_after timestamptz NULL;
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS deleted_by_user_id uuid NULL REFERENCES users(id);
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS deletion_reason text NULL;

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
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS source_chunk_id text NULL;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_unit_id text NULL;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS identity_scope text NOT NULL DEFAULT 'legacy';
CREATE INDEX IF NOT EXISTS ix_chunks_tenant ON chunks(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_chunks_page ON chunks(tenant_id, page_id);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS document_version_id text NULL REFERENCES document_versions(id);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS chunk_ordinal int NULL;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS locator jsonb NOT NULL DEFAULT '{}';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS publication_status text NOT NULL DEFAULT 'published';

CREATE TABLE IF NOT EXISTS document_sections (
  section_id text NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  document_id text NOT NULL REFERENCES documents(id),
  document_version_id text NULL REFERENCES document_versions(id),
  parent_section_id text NULL,
  title text NOT NULL,
  level int NOT NULL DEFAULT 1,
  path text[] NOT NULL DEFAULT ARRAY[]::text[],
  ordinal int NOT NULL DEFAULT 1,
  locator jsonb NOT NULL DEFAULT '{}',
  first_chunk_id text NULL REFERENCES chunks(id),
  last_chunk_id text NULL REFERENCES chunks(id),
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, knowledge_base_id, document_id, section_id)
);
CREATE INDEX IF NOT EXISTS ix_document_sections_document
  ON document_sections(tenant_id, knowledge_base_id, document_id, ordinal);
CREATE INDEX IF NOT EXISTS ix_document_sections_version
  ON document_sections(tenant_id, document_version_id, ordinal);

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

CREATE SEQUENCE IF NOT EXISTS retrieval_events_sequence;

CREATE TABLE IF NOT EXISTS retrieval_events (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  query_run_id uuid NULL REFERENCES query_runs(id),
  trace_id text NOT NULL,
  event_type text NOT NULL,
  stage text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  sequence bigint NOT NULL DEFAULT nextval('retrieval_events_sequence'),
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE retrieval_events ADD COLUMN IF NOT EXISTS sequence bigint;
UPDATE retrieval_events SET sequence = nextval('retrieval_events_sequence') WHERE sequence IS NULL;
ALTER TABLE retrieval_events ALTER COLUMN sequence SET DEFAULT nextval('retrieval_events_sequence');
ALTER TABLE retrieval_events ALTER COLUMN sequence SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_retrieval_events_tenant ON retrieval_events(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_retrieval_events_run_sequence ON retrieval_events(query_run_id, sequence);

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

CREATE TABLE IF NOT EXISTS research_plans (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  user_id uuid NULL REFERENCES users(id),
  topic text NOT NULL,
  knowledge_base_ids jsonb NOT NULL DEFAULT '[]',
  retrieval_profile text NOT NULL,
  tool_mode text NOT NULL DEFAULT 'all_local_tools'
    CHECK (tool_mode IN ('all_local_tools','extended_search_only','search_plus_document_tools')),
  retrieval_overrides jsonb NOT NULL DEFAULT '{}',
  context_policy jsonb NOT NULL DEFAULT '{}',
  notes text NOT NULL DEFAULT '',
  questions jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','archived')),
  approved_run_id uuid NULL,
  approved_at timestamptz NULL,
  approved_by_user_id uuid NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_research_plans_tenant ON research_plans(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_plans_kb ON research_plans(tenant_id, knowledge_base_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  user_id uuid NULL REFERENCES users(id),
  active_job_id uuid NULL REFERENCES ingestion_jobs(id),
  topic text NOT NULL,
  retrieval_profile text NOT NULL,
  tool_mode text NOT NULL DEFAULT 'all_local_tools'
    CHECK (tool_mode IN ('all_local_tools','extended_search_only','search_plus_document_tools')),
  retrieval_overrides jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL CHECK (status IN ('received','running','paused','completed','failed','cancelled')),
  progress jsonb NOT NULL DEFAULT '{}',
  checkpoint jsonb NOT NULL DEFAULT '{}',
  context_policy jsonb NOT NULL DEFAULT '{}',
  final_report jsonb NOT NULL DEFAULT '{}',
  stop_reason text NULL,
  error_code text NULL,
  error_message text NULL,
  pause_requested boolean NOT NULL DEFAULT false,
  cancel_requested boolean NOT NULL DEFAULT false,
  started_at timestamptz NULL,
  completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_research_runs_tenant ON research_runs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_runs_kb ON research_runs(tenant_id, knowledge_base_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_run_scopes (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  ordinal int NOT NULL,
  access_snapshot jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_run_id, knowledge_base_id),
  UNIQUE(research_run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_research_run_scopes_tenant
  ON research_run_scopes(tenant_id, research_run_id, ordinal);

CREATE TABLE IF NOT EXISTS research_episodes (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  query_run_id uuid NULL REFERENCES query_runs(id),
  episode_index int NOT NULL,
  question_id uuid NULL,
  status text NOT NULL CHECK (status IN ('received','running','completed','failed','cancelled')),
  stage text NOT NULL DEFAULT 'received',
  context_summary jsonb NOT NULL DEFAULT '{}',
  metrics jsonb NOT NULL DEFAULT '{}',
  error_code text NULL,
  error_message text NULL,
  started_at timestamptz NULL,
  completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_run_id, episode_index)
);
CREATE INDEX IF NOT EXISTS ix_research_episodes_run ON research_episodes(tenant_id, research_run_id, episode_index);

CREATE TABLE IF NOT EXISTS research_questions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  question text NOT NULL,
  ordinal int NOT NULL,
  kind text NOT NULL DEFAULT 'primary',
  status text NOT NULL CHECK (
    status IN ('open','running','covered','partial','missing','conflicting','exhausted','failed')
  ),
  execution_state text NOT NULL DEFAULT 'pending'
    CHECK (execution_state IN ('pending','running','done')),
  outcome text NULL CHECK (outcome IN ('covered','partial','exhausted','failed')),
  CHECK ((execution_state = 'done') = (outcome IS NOT NULL)),
  attempt_count int NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  rewrite_count int NOT NULL DEFAULT 0 CHECK (rewrite_count >= 0),
  depth int NOT NULL DEFAULT 0 CHECK (depth >= 0),
  budget jsonb NOT NULL DEFAULT '{}',
  acceptance jsonb NOT NULL DEFAULT '{}',
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_research_questions_run ON research_questions(tenant_id, research_run_id, ordinal);

CREATE TABLE IF NOT EXISTS research_tool_calls (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  episode_id uuid NULL REFERENCES research_episodes(id),
  question_id uuid NULL REFERENCES research_questions(id),
  query_run_id uuid NULL REFERENCES query_runs(id),
  tool_name text NOT NULL CHECK (tool_name IN ('extended_search')),
  tool_query_hash text NOT NULL,
  status text NOT NULL CHECK (status IN ('running','completed','failed','cancelled')),
  result_summary jsonb NOT NULL DEFAULT '{}',
  safe_metadata jsonb NOT NULL DEFAULT '{}',
  error_code text NULL,
  error_message text NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_research_tool_calls_run
  ON research_tool_calls(tenant_id, research_run_id, created_at);
CREATE INDEX IF NOT EXISTS ix_research_tool_calls_episode
  ON research_tool_calls(tenant_id, episode_id, created_at);

CREATE TABLE IF NOT EXISTS research_evidence_records (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  question_id uuid NULL REFERENCES research_questions(id),
  chunk_id text NOT NULL REFERENCES chunks(id),
  document_id text NULL REFERENCES documents(id),
  document_version_id text NULL REFERENCES document_versions(id),
  knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
  evidence_ref text NOT NULL,
  title text NOT NULL,
  source_url text NOT NULL,
  section_path text[] NOT NULL DEFAULT ARRAY[]::text[],
  content_abstract text NOT NULL,
  evidence_fingerprint text NULL,
  support_status text NOT NULL CHECK (support_status IN ('supports','partial','contradicts','unknown')),
  score double precision NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_run_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_research_evidence_run
  ON research_evidence_records(tenant_id, research_run_id, created_at);
CREATE INDEX IF NOT EXISTS ix_research_evidence_chunk
  ON research_evidence_records(tenant_id, chunk_id);

CREATE TABLE IF NOT EXISTS research_claim_records (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  question_id uuid NULL REFERENCES research_questions(id),
  claim_text text NOT NULL,
  support_status text NOT NULL CHECK (support_status IN ('supported','partial','unsupported','conflicting')),
  evidence_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_research_claims_run ON research_claim_records(tenant_id, research_run_id, created_at);

CREATE TABLE IF NOT EXISTS research_coverage_records (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  question_id uuid NOT NULL REFERENCES research_questions(id),
  status text NOT NULL CHECK (status IN ('missing','partial','covered','conflicting')),
  required_evidence_count int NOT NULL DEFAULT 1,
  linked_evidence_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
  reason text NOT NULL,
  metrics jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_run_id, question_id)
);
CREATE INDEX IF NOT EXISTS ix_research_coverage_run
  ON research_coverage_records(tenant_id, research_run_id, status);

CREATE TABLE IF NOT EXISTS research_reflections (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  episode_id uuid NULL REFERENCES research_episodes(id),
  reflection_type text NOT NULL DEFAULT 'operational',
  body text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_research_reflections_run
  ON research_reflections(tenant_id, research_run_id, created_at DESC);

ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS last_heartbeat_at timestamptz NULL;
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS controller_lease_id text NULL;
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS controller_lease_expires_at timestamptz NULL;
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS controller_lease_epoch bigint NOT NULL DEFAULT 0;
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS research_plan_id uuid NULL REFERENCES research_plans(id);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS tool_mode text NOT NULL DEFAULT 'all_local_tools';
ALTER TABLE research_runs DROP CONSTRAINT IF EXISTS research_runs_tool_mode_check;
ALTER TABLE research_runs ADD CONSTRAINT research_runs_tool_mode_check
  CHECK (tool_mode IN ('all_local_tools','extended_search_only','search_plus_document_tools'));
ALTER TABLE research_plans DROP CONSTRAINT IF EXISTS research_plans_tool_mode_check;
ALTER TABLE research_plans ADD CONSTRAINT research_plans_tool_mode_check
  CHECK (tool_mode IN ('all_local_tools','extended_search_only','search_plus_document_tools'));
ALTER TABLE research_episodes ADD COLUMN IF NOT EXISTS step_index int NOT NULL DEFAULT 1;
ALTER TABLE research_questions DROP CONSTRAINT IF EXISTS research_questions_status_check;
ALTER TABLE research_questions ADD CONSTRAINT research_questions_status_check
  CHECK (status IN ('open','running','covered','partial','missing','conflicting','exhausted','failed'));
ALTER TABLE research_questions ADD COLUMN IF NOT EXISTS execution_state text NOT NULL DEFAULT 'pending';
ALTER TABLE research_questions ADD COLUMN IF NOT EXISTS outcome text NULL;
ALTER TABLE research_questions ADD COLUMN IF NOT EXISTS attempt_count int NOT NULL DEFAULT 0;
ALTER TABLE research_questions ADD COLUMN IF NOT EXISTS rewrite_count int NOT NULL DEFAULT 0;
ALTER TABLE research_questions ADD COLUMN IF NOT EXISTS depth int NOT NULL DEFAULT 0;
ALTER TABLE research_questions ADD COLUMN IF NOT EXISTS budget jsonb NOT NULL DEFAULT '{}';
ALTER TABLE research_questions DROP CONSTRAINT IF EXISTS research_questions_execution_state_check;
ALTER TABLE research_questions ADD CONSTRAINT research_questions_execution_state_check
  CHECK (execution_state IN ('pending','running','done'));
ALTER TABLE research_questions DROP CONSTRAINT IF EXISTS research_questions_outcome_check;
ALTER TABLE research_questions ADD CONSTRAINT research_questions_outcome_check
  CHECK (outcome IN ('covered','partial','exhausted','failed') OR outcome IS NULL);
ALTER TABLE research_questions DROP CONSTRAINT IF EXISTS research_questions_lifecycle_check;
ALTER TABLE research_questions ADD CONSTRAINT research_questions_lifecycle_check
  CHECK ((execution_state = 'done') = (outcome IS NOT NULL));
UPDATE research_questions
SET execution_state = CASE
      WHEN status IN ('covered','partial','exhausted','failed') THEN 'done'
      ELSE execution_state
    END,
    outcome = CASE
      WHEN status = 'covered' THEN 'covered'
      WHEN status = 'partial' THEN 'partial'
      WHEN status = 'exhausted' THEN 'exhausted'
      WHEN status = 'failed' THEN 'failed'
      ELSE outcome
    END
WHERE execution_state = 'pending' AND status IN ('covered','partial','exhausted','failed');
ALTER TABLE research_evidence_records ADD COLUMN IF NOT EXISTS evidence_fingerprint text NULL;
ALTER TABLE research_tool_calls ADD COLUMN IF NOT EXISTS tool_args_hash text NULL;
ALTER TABLE research_tool_calls ADD COLUMN IF NOT EXISTS last_heartbeat_at timestamptz NULL;
ALTER TABLE research_tool_calls ADD COLUMN IF NOT EXISTS validated_args jsonb NOT NULL DEFAULT '{}';
ALTER TABLE research_tool_calls ADD COLUMN IF NOT EXISTS execution_attempts int NOT NULL DEFAULT 0;
ALTER TABLE research_tool_calls DROP CONSTRAINT IF EXISTS research_tool_calls_tool_name_check;
ALTER TABLE research_tool_calls ADD CONSTRAINT research_tool_calls_tool_name_check
  CHECK (tool_name IN (
    'extended_search', 'document_section_lookup', 'search_within_document', 'table_csv_lookup', 'metadata_lookup'
  ));
ALTER TABLE research_tool_calls DROP CONSTRAINT IF EXISTS research_tool_calls_status_check;
ALTER TABLE research_tool_calls ADD CONSTRAINT research_tool_calls_status_check
  CHECK (status IN ('running','completed','failed','cancelled','stalled'));
ALTER TABLE research_claim_records ADD COLUMN IF NOT EXISTS verification_input_hash text NULL;

CREATE TABLE IF NOT EXISTS research_decisions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  episode_id uuid NULL REFERENCES research_episodes(id),
  question_id uuid NULL REFERENCES research_questions(id),
  decision_type text NOT NULL,
  selected_strategy text NOT NULL,
  reason text NOT NULL,
  evidence_gain int NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_research_decisions_run
  ON research_decisions(tenant_id, research_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_claim_relations (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  research_run_id uuid NOT NULL REFERENCES research_runs(id),
  source_claim_id uuid NOT NULL REFERENCES research_claim_records(id),
  target_claim_id uuid NOT NULL REFERENCES research_claim_records(id),
  relation text NOT NULL CHECK (relation IN ('supports','contradicts','depends_on')),
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_run_id, source_claim_id, target_claim_id, relation)
);
CREATE INDEX IF NOT EXISTS ix_research_claim_relations_run
  ON research_claim_relations(tenant_id, research_run_id, created_at);
"""

# Additive migrations are deliberately kept separate from the bootstrap schema.
# This lets an already-running installation converge without rewriting the
# historical schema marker or losing existing rows.
ADDITIVE_MIGRATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "002_research_evidence_refs_and_job_leases",
        (
            "ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS worker_lease_id text NULL",
            "ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS worker_lease_expires_at timestamptz NULL",
            "ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS worker_last_heartbeat_at timestamptz NULL",
            "CREATE INDEX IF NOT EXISTS ix_ingestion_jobs_claim_lease "
            "ON ingestion_jobs(kind, status, worker_lease_expires_at, created_at)",
            "UPDATE research_evidence_records SET evidence_ref = 'E-' || replace(id::text, '-', '') "
            "WHERE evidence_ref IS DISTINCT FROM ('E-' || replace(id::text, '-', ''))",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_research_evidence_run_ref "
            "ON research_evidence_records(research_run_id, evidence_ref)",
        ),
    ),
    (
        "003_reliability_idempotency_records",
        (
            """
            CREATE TABLE IF NOT EXISTS idempotency_records (
              id uuid PRIMARY KEY,
              tenant_id uuid NOT NULL REFERENCES tenants(id),
              actor_user_id uuid NOT NULL REFERENCES users(id),
              route text NOT NULL,
              idempotency_key text NOT NULL,
              request_hash text NOT NULL,
              status text NOT NULL CHECK (status IN ('in_progress','completed','failed')),
              resource_id text NULL,
              response_status int NULL,
              safe_response jsonb NOT NULL DEFAULT '{}',
              expires_at timestamptz NOT NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now(),
              UNIQUE(tenant_id, actor_user_id, route, idempotency_key)
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_idempotency_records_expiry ON idempotency_records(expires_at)",
            """
            CREATE TABLE IF NOT EXISTS worker_instances (
              id text PRIMARY KEY,
              lane text NOT NULL,
              last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
              started_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_worker_instances_heartbeat ON worker_instances(last_heartbeat_at DESC)",
        ),
    ),
    (
        "004_reliability_ingestion_item_retry",
        (
            "ALTER TABLE ingestion_job_items ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NULL",
            "CREATE INDEX IF NOT EXISTS ix_ingestion_job_items_retry "
            "ON ingestion_job_items(job_id, status, next_attempt_at)",
        ),
    ),
    (
        "005_model_control_plane",
        (
            """
            CREATE TABLE IF NOT EXISTS model_provider_connections (
              id uuid PRIMARY KEY,
              name text UNIQUE NOT NULL,
              driver text NOT NULL CHECK (
                driver IN ('openrouter','vllm','llamacpp','textgen_webui','openai_compatible','mock')
              ),
              base_url text NOT NULL,
              endpoint_paths jsonb NOT NULL DEFAULT '{}',
              safe_headers jsonb NOT NULL DEFAULT '{}',
              tls_verify boolean NOT NULL DEFAULT true,
              enabled boolean NOT NULL DEFAULT true,
              row_version bigint NOT NULL DEFAULT 1,
              last_status jsonb NOT NULL DEFAULT '{}',
              last_checked_at timestamptz NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_model_provider_connections_enabled "
            "ON model_provider_connections(enabled, name)",
            """
            CREATE TABLE IF NOT EXISTS model_connection_credentials (
              id uuid PRIMARY KEY,
              connection_id uuid NOT NULL REFERENCES model_provider_connections(id) ON DELETE CASCADE,
              version bigint NOT NULL,
              encrypted_payload text NOT NULL,
              state text NOT NULL CHECK (state IN ('pending','active','retired')),
              source text NOT NULL DEFAULT 'database' CHECK (source IN ('database','env','secret_file')),
              created_at timestamptz NOT NULL DEFAULT now(),
              activated_at timestamptz NULL,
              UNIQUE(connection_id, version)
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_connection_credentials_active "
            "ON model_connection_credentials(connection_id) WHERE state = 'active'",
            """
            ALTER TABLE model_aliases
              ADD COLUMN IF NOT EXISTS connection_id uuid NULL REFERENCES model_provider_connections(id),
              ADD COLUMN IF NOT EXISTS input_modalities jsonb NOT NULL DEFAULT '["text"]',
              ADD COLUMN IF NOT EXISTS capabilities jsonb NOT NULL DEFAULT '{}',
              ADD COLUMN IF NOT EXISTS context_window_tokens int NULL,
              ADD COLUMN IF NOT EXISTS max_output_tokens int NULL,
              ADD COLUMN IF NOT EXISTS dimensions int NULL,
              ADD COLUMN IF NOT EXISTS tokenizer_contract jsonb NOT NULL DEFAULT '{}',
              ADD COLUMN IF NOT EXISTS model_defaults jsonb NOT NULL DEFAULT '{}',
              ADD COLUMN IF NOT EXISTS thinking_capabilities jsonb NOT NULL DEFAULT '{}',
              ADD COLUMN IF NOT EXISTS catalog_snapshot jsonb NOT NULL DEFAULT '{}',
              ADD COLUMN IF NOT EXISTS canary_status jsonb NOT NULL DEFAULT '{}',
              ADD COLUMN IF NOT EXISTS row_version bigint NOT NULL DEFAULT 1
            """,
            "CREATE INDEX IF NOT EXISTS ix_model_aliases_connection ON model_aliases(connection_id, is_enabled)",
            """
            CREATE TABLE IF NOT EXISTS model_configuration_revisions (
              id uuid PRIMARY KEY,
              revision bigint NOT NULL,
              status text NOT NULL CHECK (status IN ('draft','validated','active','archived')),
              config_hash text NOT NULL,
              resolved_snapshot jsonb NOT NULL DEFAULT '{}',
              row_version bigint NOT NULL DEFAULT 1,
              created_by uuid NULL REFERENCES users(id),
              validation_report jsonb NOT NULL DEFAULT '{}',
              validated_at timestamptz NULL,
              activated_at timestamptz NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now(),
              UNIQUE(revision)
            )
            """,
            "ALTER TABLE model_configuration_revisions ADD COLUMN IF NOT EXISTS row_version bigint NOT NULL DEFAULT 1",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_configuration_active "
            "ON model_configuration_revisions(status) WHERE status = 'active'",
            """
            CREATE TABLE IF NOT EXISTS model_stage_bindings (
              id uuid PRIMARY KEY,
              revision_id uuid NOT NULL REFERENCES model_configuration_revisions(id) ON DELETE CASCADE,
              stage_key text NOT NULL,
              model_alias text NOT NULL REFERENCES model_aliases(alias),
              parameter_overrides jsonb NOT NULL DEFAULT '{}',
              token_policy jsonb NOT NULL DEFAULT '{}',
              thinking_policy jsonb NOT NULL DEFAULT '{}',
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now(),
              UNIQUE(revision_id, stage_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS model_validation_runs (
              id uuid PRIMARY KEY,
              target_type text NOT NULL CHECK (target_type IN ('connection','model','stage','revision')),
              target_id text NOT NULL,
              config_hash text NOT NULL,
              status text NOT NULL CHECK (status IN ('passed','failed','skipped')),
              safe_error_code text NULL,
              measurements jsonb NOT NULL DEFAULT '{}',
              created_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_model_validation_runs_target "
            "ON model_validation_runs(target_type, target_id, created_at DESC)",
            "ALTER TABLE query_runs ADD COLUMN IF NOT EXISTS model_config_revision_id "
            "uuid NULL REFERENCES model_configuration_revisions(id)",
            "ALTER TABLE query_runs ADD COLUMN IF NOT EXISTS model_config_hash text NULL",
            "ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS model_config_revision_id "
            "uuid NULL REFERENCES model_configuration_revisions(id)",
            "ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS model_config_hash text NULL",
            "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS model_config_revision_id "
            "uuid NULL REFERENCES model_configuration_revisions(id)",
            "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS model_config_hash text NULL",
        ),
    ),
    (
        "006_model_gateway_request_adapter",
        (
            "ALTER TABLE model_provider_connections ADD COLUMN IF NOT EXISTS request_adapter "
            "jsonb NOT NULL DEFAULT '{}'",
            "ALTER TABLE model_provider_connections ADD COLUMN IF NOT EXISTS request_defaults "
            "jsonb NOT NULL DEFAULT '{}'",
            "ALTER TABLE model_aliases ADD COLUMN IF NOT EXISTS startup_canary jsonb NOT NULL DEFAULT '{}'",
        ),
    ),
    (
        "007_search_projection_events",
        (
            """
            CREATE TABLE IF NOT EXISTS search_projection_events (
              id uuid PRIMARY KEY,
              tenant_id uuid NOT NULL REFERENCES tenants(id),
              knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
              document_id text NOT NULL REFERENCES documents(id),
              event_kind text NOT NULL CHECK (event_kind IN ('document_access','document_publication')),
              dedupe_key text NOT NULL UNIQUE,
              payload jsonb NOT NULL DEFAULT '{}',
              status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','running','completed','failed')),
              attempts int NOT NULL DEFAULT 0,
              next_attempt_at timestamptz NOT NULL DEFAULT now(),
              worker_lease_id text NULL,
              worker_lease_expires_at timestamptz NULL,
              error_code text NULL,
              error_message text NULL,
              completed_at timestamptz NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_search_projection_events_claim "
            "ON search_projection_events(status, next_attempt_at, worker_lease_expires_at, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_search_projection_events_tenant "
            "ON search_projection_events(tenant_id, knowledge_base_id, created_at DESC)",
        ),
    ),
    (
        "008_search_projection_reconciliation",
        (
            "ALTER TABLE search_projection_events DROP CONSTRAINT IF EXISTS search_projection_events_document_id_fkey",
            "ALTER TABLE search_projection_events ADD CONSTRAINT search_projection_events_document_id_fkey "
            "FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE",
            """
            CREATE TABLE IF NOT EXISTS search_projection_reconciliation (
              document_id text PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
              tenant_id uuid NOT NULL REFERENCES tenants(id),
              knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
              status text NOT NULL DEFAULT 'due' CHECK (status IN ('due','running','ok','degraded')),
              attempts int NOT NULL DEFAULT 0,
              next_check_at timestamptz NOT NULL DEFAULT now(),
              last_checked_at timestamptz NULL,
              last_success_at timestamptz NULL,
              worker_lease_id text NULL,
              worker_lease_expires_at timestamptz NULL,
              expected_document_version_id text NULL,
              expected_projection_hash text NULL,
              observed_projection_hash text NULL,
              last_error_code text NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_search_projection_reconciliation_claim "
            "ON search_projection_reconciliation(status, next_check_at, worker_lease_expires_at, updated_at)",
            "CREATE INDEX IF NOT EXISTS ix_search_projection_reconciliation_scope "
            "ON search_projection_reconciliation(tenant_id, knowledge_base_id, updated_at DESC)",
        ),
    ),
    (
        "009_search_projection_historical_reconciliation",
        (
            "ALTER TABLE search_projection_reconciliation "
            "ADD COLUMN IF NOT EXISTS reconciliation_generation integer NOT NULL DEFAULT 1",
            "ALTER TABLE search_projection_reconciliation "
            "ADD CONSTRAINT search_projection_reconciliation_generation_check "
            "CHECK (reconciliation_generation >= 1)",
            """
            CREATE TABLE IF NOT EXISTS search_projection_reconciliation_scan_state (
              generation integer PRIMARY KEY CHECK (generation >= 1),
              cursor_document_id text NULL,
              completed_at timestamptz NULL,
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            """
            INSERT INTO search_projection_reconciliation_scan_state(generation)
            VALUES (1)
            ON CONFLICT (generation) DO NOTHING
            """,
        ),
    ),
)


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


@asynccontextmanager
async def connect_autocommit(settings: Settings | None = None) -> AsyncIterator[AsyncConnection]:
    """Open a connection without holding a transaction across external work.

    Deep Research retrieval can call the Model Gateway and perform several
    bounded searches.  Those calls must not keep a PostgreSQL transaction open
    while they wait.  Individual statements still run through SQLAlchemy, but
    each statement is committed independently and a failed later stage can be
    resumed from the durable ledger.
    """
    engine = get_engine(settings)
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        yield conn


async def ensure_schema(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    engine = get_engine(resolved)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('wikipediarag_schema_v1'))"))
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        await conn.execute(
            text(
                """
                INSERT INTO schema_migrations(version)
                VALUES ('001_retrieval_correctness_v3')
                ON CONFLICT (version) DO NOTHING
                """
            )
        )
        for version, statements in ADDITIVE_MIGRATIONS:
            already_applied = await conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :version"),
                {"version": version},
            )
            if already_applied.first() is not None:
                continue
            for statement in statements:
                await conn.execute(text(statement))
            await conn.execute(
                text(
                    """
                    INSERT INTO schema_migrations(version)
                    VALUES (:version)
                    ON CONFLICT (version) DO NOTHING
                    """
                ),
                {"version": version},
            )
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
    await seed_model_control_plane(conn, settings)


async def seed_model_control_plane(conn: AsyncConnection, settings: Settings) -> None:
    """Import legacy YAML exactly once, without importing any secret fields."""
    existing = await conn.execute(text("SELECT EXISTS (SELECT 1 FROM model_aliases)"))
    if bool(existing.scalar_one()):
        return
    try:
        with settings.models_config_path.open("r", encoding="utf-8") as file:
            models_payload = yaml.safe_load(file) or {}
        with settings.retrieval_config_path.open("r", encoding="utf-8") as file:
            retrieval_payload = yaml.safe_load(file) or {}
    except (OSError, yaml.YAMLError):
        return
    connections: dict[str, str] = {}
    for provider in sorted({str(value.get("provider")) for value in (models_payload.get("models") or {}).values()}):
        if provider not in {"openrouter", "mock"}:
            continue
        connection_id = uuid4()
        name = f"bootstrap-{provider}"
        base_url = settings.openrouter_base_url if provider == "openrouter" else settings.mock_provider_url
        await conn.execute(
            text(
                "INSERT INTO model_provider_connections(id,name,driver,base_url) "
                "VALUES (:id,:name,:driver,:base_url) ON CONFLICT (name) DO NOTHING"
            ),
            {"id": str(connection_id), "name": name, "driver": provider, "base_url": str(base_url).rstrip("/")},
        )
        row = await conn.execute(text("SELECT id FROM model_provider_connections WHERE name=:name"), {"name": name})
        connections[provider] = str(row.scalar_one())
    alias_ids: dict[str, str] = {}
    for alias, value in (models_payload.get("models") or {}).items():
        if not isinstance(value, dict) or value.get("operation") not in {"chat", "embedding", "rerank"}:
            continue
        alias_id = uuid4()
        alias_ids[str(alias)] = str(alias_id)
        capability = {str(value["operation"]): True}
        if value.get("provider_preferences", {}).get("require_parameters"):
            capability["structured_output"] = True
        await conn.execute(
            text(
                "INSERT INTO model_aliases(id,alias,provider,provider_model,operation,connection_id,"
                "capabilities,input_modalities,context_window_tokens,dimensions,"
                "tokenizer_contract,model_defaults,thinking_capabilities) "
                "VALUES (:id,:alias,:provider,:provider_model,:operation,:connection_id,CAST(:capabilities AS jsonb),"
                "CAST(:modalities AS jsonb),:context,:dimensions,"
                "CAST(:tokenizer AS jsonb),CAST(:defaults AS jsonb),"
                "CAST(:thinking AS jsonb))"
            ),
            {
                "id": str(alias_id),
                "alias": str(alias),
                "provider": str(value["provider"]),
                "provider_model": str(value["model"]),
                "operation": str(value["operation"]),
                "connection_id": connections.get(str(value["provider"])),
                "capabilities": json_dumps(capability),
                "modalities": json_dumps(["text"]),
                "context": value.get("context_window_tokens"),
                "dimensions": value.get("dimensions"),
                "tokenizer": json_dumps({"name": value.get("tokenizer")} if value.get("tokenizer") else {}),
                "defaults": json_dumps({"provider": value.get("provider_preferences", {})}),
                "thinking": json_dumps({"reasoning_control": str(value["provider"]) in {"openrouter", "mock"}}),
            },
        )
    profile = (retrieval_payload.get("profiles") or {}).get("sota_mvp") or {}
    aliases = profile.get("model_aliases") or {}
    deep_research = profile.get("deep_research") or {}
    stages: dict[str, Any] = {
        "ingestion.embedding": {
            "model_alias": aliases.get("embed"),
            "thinking_policy": {"mode": "off", "effort": "none"},
        },
        "retrieval.query_embedding": {
            "model_alias": aliases.get("embed"),
            "thinking_policy": {"mode": "off", "effort": "none"},
        },
        "retrieval.rerank": {
            "model_alias": aliases.get("rerank"),
            "thinking_policy": {"mode": "off", "effort": "none"},
        },
        "chat.answer": {
            "model_alias": aliases.get("generator_main"),
            "thinking_policy": {"mode": "off", "effort": "none"},
        },
        "chat.claim_verification": {
            "model_alias": aliases.get("verifier"),
            "thinking_policy": {"mode": "off", "effort": "none"},
        },
    }
    for stage_name, key in (
        ("planner", "deep_research.planner"),
        ("verifier", "deep_research.verifier"),
        ("synthesis", "deep_research.synthesis"),
    ):
        stage = deep_research.get(stage_name) or {}
        stages[key] = {"model_alias": stage.get("model_alias"), "thinking_policy": {"mode": "off", "effort": "none"}}
    legacy_bundle = hashlib.sha256(
        settings.models_config_path.read_bytes() + settings.retrieval_config_path.read_bytes()
    ).hexdigest()
    snapshot = {
        "stages": stages,
        "source": "yaml-bootstrap-v2",
        "pre_control_plane_hash": legacy_bundle,
    }
    encoded = json_dumps(snapshot).encode("utf-8")
    await conn.execute(
        text(
            "INSERT INTO model_configuration_revisions(id,revision,status,config_hash,resolved_snapshot) "
            "VALUES (:id,1,'draft',:hash,CAST(:snapshot AS jsonb)) ON CONFLICT (revision) DO NOTHING"
        ),
        {"id": str(uuid4()), "hash": hashlib.sha256(encoded).hexdigest(), "snapshot": encoded.decode("utf-8")},
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
