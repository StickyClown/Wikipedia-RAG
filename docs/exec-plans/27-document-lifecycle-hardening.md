# ExecPlan 27 - Document Lifecycle Hardening

Status: implemented
Updated: 2026-07-30

## Goal

Add the first production hardening slice for uploaded document retention, deletion, backup and restore semantics without adding multi-KB retrieval or document-level ACLs.

## Delivered

- Added forward-only lifecycle columns to `documents` and `document_versions`:
  - `lifecycle_state`
  - `deleted_at`
  - `purge_after`
  - `deleted_by_user_id`
  - `deletion_reason`
- Added `DOCUMENT_SOFT_DELETE_RETENTION_DAYS`, default `30`.
- Added `DELETE /api/v1/documents/{document_id}`:
  - requires KB `OWNER`;
  - marks document and versions as `deleting`;
  - marks DB chunks as `deleted`;
  - deletes OpenSearch chunks by `tenant_id + knowledge_base_id + document_id`;
  - schedules an idempotent `document_delete` cleanup job.
- Added worker support for due `document_delete` jobs:
  - skips jobs whose `purge_after` is in the future;
  - deletes MinIO artifact objects;
  - repeats OpenSearch chunk deletion;
  - deletes DB chunks and `document_artifacts` rows;
  - marks document/version lifecycle `deleted`;
  - records `purge_failed` with a safe error code on failure.
- Document public reads now expose only `active` documents.
- Backup/restore v1 contract: PostgreSQL plus object storage are authoritative; OpenSearch is rebuildable and not the source of truth.

## Validation

```text
uv run pytest tests\unit\test_document_deletion_lifecycle.py tests\unit\test_ingestion_publication_invariants.py tests\unit\test_auth_schema.py -q
-> exit 0, 11 passed

uv run ruff check src\wikipediarag\api_app.py src\wikipediarag\config.py src\wikipediarag\db.py src\wikipediarag\ingestion.py src\wikipediarag\repository.py src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\storage.py tests\unit\test_document_deletion_lifecycle.py
-> exit 0

uv run mypy src\wikipediarag\api_app.py src\wikipediarag\config.py src\wikipediarag\db.py src\wikipediarag\ingestion.py src\wikipediarag\repository.py src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\storage.py tests\unit\test_document_deletion_lifecycle.py
-> exit 0
```

## Remaining

- Runtime deletion smoke against Docker services.
- ACL mirroring runtime smoke.
- Parser sandboxing/autoscaling.
- Eval local-auth client and release-gate preflight tightening.
- Public multi-file batch upload and Multi-KB retrieval.
