from __future__ import annotations

from wikipediarag.db import SCHEMA_SQL


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
