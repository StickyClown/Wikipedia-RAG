from __future__ import annotations

from wikipediarag.db import ADDITIVE_MIGRATIONS, SCHEMA_SQL
from wikipediarag.schemas import ResearchRunStatus


def test_auth_schema_tables_are_forward_only_ensure_schema_expansions() -> None:
    for table in (
        "auth_identities",
        "auth_sessions",
        "auth_oidc_flows",
        "groups",
        "group_memberships",
        "knowledge_base_grants",
        "audit_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_SQL


def test_user_email_is_profile_attribute_and_identity_key_is_issuer_subject() -> None:
    assert "ALTER TABLE users ALTER COLUMN email DROP NOT NULL" in SCHEMA_SQL
    assert "UNIQUE (issuer, subject)" in SCHEMA_SQL
    assert "identity_key text UNIQUE NOT NULL" in SCHEMA_SQL


def test_default_tenant_role_backfill_uses_new_role_vocabulary() -> None:
    assert "WHEN role IN ('owner','admin','TENANT_ADMIN') THEN 'TENANT_ADMIN'" in SCHEMA_SQL
    assert "CHECK (role IN ('TENANT_ADMIN','MEMBER'))" in SCHEMA_SQL


def test_query_runs_and_audit_events_carry_scope_and_request_identity() -> None:
    assert "knowledge_base_id uuid NULL REFERENCES knowledge_bases(id)" in SCHEMA_SQL
    assert "request_id uuid NOT NULL" in SCHEMA_SQL
    assert "trace_id text NOT NULL" in SCHEMA_SQL


def test_deep_research_schema_tracks_durable_lifecycle_and_typed_memory() -> None:
    for table in (
        "research_runs",
        "research_run_scopes",
        "research_episodes",
        "research_questions",
        "research_tool_calls",
        "research_evidence_records",
        "research_claim_records",
        "research_claim_relations",
        "research_coverage_records",
        "research_reflections",
        "research_decisions",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_SQL

    assert (
        "status text NOT NULL CHECK (status IN ('received','running','paused','completed','failed','cancelled'))"
        in SCHEMA_SQL
    )
    assert "context_policy jsonb NOT NULL DEFAULT '{}'" in SCHEMA_SQL
    assert "final_report jsonb NOT NULL DEFAULT '{}'" in SCHEMA_SQL
    assert "tool_query_hash text NOT NULL" in SCHEMA_SQL
    assert "ALTER TABLE research_tool_calls ADD COLUMN IF NOT EXISTS tool_args_hash text NULL" in SCHEMA_SQL
    assert "last_heartbeat_at timestamptz NULL" in SCHEMA_SQL
    assert "UNIQUE(research_run_id, chunk_id)" in SCHEMA_SQL
    assert "UNIQUE(research_run_id, question_id)" in SCHEMA_SQL
    assert "UNIQUE(research_run_id, knowledge_base_id)" in SCHEMA_SQL
    assert "reflection_type text NOT NULL DEFAULT 'operational'" in SCHEMA_SQL
    assert ResearchRunStatus.paused == "paused"


def test_reliability_migrations_add_durable_idempotency_and_ingestion_retry() -> None:
    migrations = {name: statements for name, statements in ADDITIVE_MIGRATIONS}

    assert "003_reliability_idempotency_records" in migrations
    assert any(
        "CREATE TABLE IF NOT EXISTS idempotency_records" in statement
        for statement in migrations["003_reliability_idempotency_records"]
    )
    assert "004_reliability_ingestion_item_retry" in migrations
    assert any("next_attempt_at" in statement for statement in migrations["004_reliability_ingestion_item_retry"])


def test_model_control_plane_migration_is_additive_and_secret_safe() -> None:
    migrations = {name: statements for name, statements in ADDITIVE_MIGRATIONS}
    statements = migrations["005_model_control_plane"]
    joined = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS model_provider_connections" in joined
    assert "CREATE TABLE IF NOT EXISTS model_connection_credentials" in joined
    assert "CREATE TABLE IF NOT EXISTS model_configuration_revisions" in joined
    assert "CREATE TABLE IF NOT EXISTS model_stage_bindings" in joined
    assert "CREATE TABLE IF NOT EXISTS model_validation_runs" in joined
    assert "model_config_revision_id" in joined
    assert "encrypted_payload" in joined
    assert "UNIQUE INDEX IF NOT EXISTS uq_model_configuration_active" in joined
