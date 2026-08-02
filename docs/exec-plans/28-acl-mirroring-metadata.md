# ExecPlan 28 - ACL Mirroring Metadata

Status: implemented
Updated: 2026-07-30

## Goal

Add safe ACL mirroring metadata to KB grants without adding document-level ACLs or external connector policy.

## Delivered

- Added forward-only `knowledge_base_grants.metadata jsonb`.
- Grant list responses now include `metadata`.
- Grant create/update writes deterministic safe metadata:
  - `acl_snapshot` with scope, subject type/id, role and source table;
  - `acl_sync` with source, `in_sync` status and timestamp;
  - OIDC group grants include `external_group_path`.
- OIDC group membership sync remains scoped to `membership_type='OIDC'` and does not remove local memberships.
- Effective access still comes from `ActorContext`, tenant role, direct user grants, local groups and OIDC groups; no document-level ACLs were introduced.

## Validation

```text
uv run pytest tests\unit\test_acl_mirroring_metadata.py tests\unit\test_auth_schema.py -q
-> exit 0, 7 passed

uv run ruff format --check src\wikipediarag\eval\diagnostics.py src\wikipediarag\api_app.py src\wikipediarag\db.py tests\unit\test_acl_mirroring_metadata.py
-> exit 0

uv run ruff check src\wikipediarag\api_app.py tests\unit\test_acl_mirroring_metadata.py
-> exit 0

uv run mypy src\wikipediarag\api_app.py src\wikipediarag\db.py tests\unit\test_acl_mirroring_metadata.py
-> exit 0
```

## Remaining

- Runtime smoke proving user access appears/disappears through an OIDC group grant without KB reindex.
- External connector ACL import policy remains undecided.
