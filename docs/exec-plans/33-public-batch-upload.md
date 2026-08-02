# ExecPlan 33 - Public Multi-File Batch Upload

Status: implemented 2026-07-30

## Scope

Expose the existing upload batch/job-item model as a public API and update the UI to upload several files through the asynchronous object-storage-first ingestion flow.

## Implemented

- `POST /api/v1/uploads/batches` creates one tenant/KB-scoped batch plus up to 25 upload sessions.
- `GET /api/v1/uploads/batches/{batch_id}` returns a safe aggregate read model from upload sessions and ingestion job items.
- Single-file `/api/v1/uploads/sessions` remains compatible.
- Completing a session that already belongs to a batch reuses that batch instead of creating a new one-item batch.
- UI upload accepts multiple files, shows aggregate and per-file state, and can retry failed ingestion jobs through the existing resume endpoint.

## Security Notes

- Storage object keys are server-owned and are not returned by batch status.
- Batch create/status require active tenant plus KB `EDITOR`.
- Duplicate filename/checksum entries in one batch are rejected to avoid competing ingestion jobs for the same document version.

## Validation

```text
uv run pytest tests\unit\test_upload_batches.py -q
-> exit 0, 2 passed
```
