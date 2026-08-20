from __future__ import annotations

from wikipediarag.db import FINAL_SCHEMA_VERSION, HISTORICAL_SCHEMA_VERSIONS, final_workspace_schema_sql


def test_clean_workspace_bootstrap_has_no_tenant_shape_and_includes_grants() -> None:
    schema = final_workspace_schema_sql()

    assert "tenant_id" not in schema
    assert "active_tenant_id" not in schema
    assert "CREATE TABLE IF NOT EXISTS tenants" not in schema
    assert "CREATE TABLE IF NOT EXISTS tenant_memberships" not in schema
    assert "CREATE TABLE IF NOT EXISTS knowledge_base_grants" not in schema
    assert "CREATE TABLE IF NOT EXISTS resource_grants" in schema
    assert "owner_user_id uuid NOT NULL REFERENCES users(id)" in schema
    assert "inherits_kb_access boolean NOT NULL DEFAULT true" in schema


def test_clean_workspace_marks_historical_schema_without_replaying_it() -> None:
    assert HISTORICAL_SCHEMA_VERSIONS[0] == "001_retrieval_correctness_v3"
    assert FINAL_SCHEMA_VERSION == "010_single_workspace_clean_reset_v1"
