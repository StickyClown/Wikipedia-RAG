# Repository Engineering Guide

Read [README.md](README.md), the current goal in [docs/STATUS.md](docs/STATUS.md),
and only the architecture contract relevant to the task before editing.

## Working Method

- Make the smallest complete change that satisfies the approved task.
- Trace architecture changes as `code → contract → owner → boundary → executable test`.
- Preserve public contracts or document and test an intentional compatibility change.
- Add deterministic coverage for the changed success path and important failures.
- Run the narrow stable checks for the changed surface; never report an unrun or failed check as passing.
- Review the diff for authorization, data loss, migrations, errors, observability and accidental scope growth.
- Update `docs/STATUS.md` when the current goal, delivered behavior, validation or blockers change.

## Backend Contracts

- Derive tenant, user, role, group, filter and object authority on the server; client identifiers are never authority.
- Keep API schemas and safe error semantics typed and compatible. Use additive migrations; do not rewrite committed migrations.
- Large ingestion and research work stays asynchronous, idempotent, resumable and bounded in retries/concurrency.
- Failed or cancelled jobs never publish searchable data. PostgreSQL/object storage remain authoritative; search and caches are derived.
- Business model calls use provider-neutral Model Gateway aliases. Any healthy configured endpoint is valid when it implements the required chat, embedding, rerank or token-counting operation.
- Keep secrets, credentials, prompts, provider payloads, raw document contents and storage keys out of normal logs and public responses.

## UI Contracts

- Keep UI request/response types aligned with the API and preserve cookie, CSRF, SSE and cancellation behavior.
- Handle loading, empty, success, forbidden, degraded and failed states explicitly with safe user-facing errors.
- Verify visible changes with typecheck/build and the smallest relevant Vitest or Playwright path.
- UI guards improve usability but never replace backend authorization.

## Safety

- Preserve unrelated user changes in a dirty worktree.
- Do not run destructive Git, database, index, storage or Docker-volume operations without explicit approval.
- Do not commit secrets, credentials, uploads, model files, source archives, generated indices or large eval artifacts.
- Long-running commands must expose progress and bounded execution.

Use stable Make targets when available. On Windows, run the equivalent `uv`, `pnpm` or `docker compose` command and record the exact command and exit code.
