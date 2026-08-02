from __future__ import annotations

from wikipediarag.db import SCHEMA_SQL
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
        "research_episodes",
        "research_questions",
        "research_evidence_records",
        "research_claim_records",
        "research_coverage_records",
        "research_reflections",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_SQL

    assert (
        "status text NOT NULL CHECK (status IN ('received','running','paused','completed','failed','cancelled'))"
        in SCHEMA_SQL
    )
    assert "context_policy jsonb NOT NULL DEFAULT '{}'" in SCHEMA_SQL
    assert "final_report jsonb NOT NULL DEFAULT '{}'" in SCHEMA_SQL
    assert "UNIQUE(research_run_id, chunk_id)" in SCHEMA_SQL
    assert "UNIQUE(research_run_id, question_id)" in SCHEMA_SQL
    assert "reflection_type text NOT NULL DEFAULT 'operational'" in SCHEMA_SQL
    assert ResearchRunStatus.paused == "paused"
