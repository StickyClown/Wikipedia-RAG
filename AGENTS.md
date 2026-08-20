# Repository Engineering Guide

Before editing:

1. Read [README.md](README.md) for the repository map.
2. Read the current goal and known state in [docs/STATUS.md](docs/STATUS.md).
3. Load only the architecture contract and documentation relevant to the task.

Do not explore unrelated parts of the repository without evidence that the task crosses those boundaries.

## Working Method

* Make the smallest complete change that satisfies the requested task.
* Start from task-provided files, symbols, endpoints, tests and failure evidence before widening the search.
* For architectural changes, trace the affected path as `code → contract → owner → boundary → executable test`.
* Keep changes internally consistent across implementations, contracts, consumers, tests and documentation.
* Add deterministic coverage for the changed success path and important failure paths.
* Run the narrowest stable checks that exercise the changed surface.
* Never report an unrun, skipped or failed check as passing.
* Do not weaken, delete or bypass meaningful regression coverage merely to obtain a green test run.
* Review the final diff for authorization, data loss, migrations, error handling, observability and accidental scope growth.
* Update `docs/STATUS.md` when the current goal, delivered behavior, validation state or blockers materially change.

## Compatibility

This project is pre-release.

* Do not add backward-compatibility shims, legacy aliases or parallel obsolete paths unless explicitly requested.
* Prefer removing obsolete paths when all current owners and consumers can move atomically.
* When intentionally changing a public contract, update its implementations, consumers, tests and relevant documentation in the same change.
* Preserve compatibility only where the task or an external contract explicitly requires it.

## Backend Contracts

* Derive tenant, user, role, group, filter and object authority on the server. Client-provided identifiers are never authorization authority.
* Keep API schemas, typed contracts and safe error semantics aligned across producers and consumers.
* Use additive database migrations. Never rewrite a committed migration.
* Large ingestion and research workflows must remain asynchronous, idempotent, resumable and bounded in retries and concurrency.
* Failed or cancelled jobs must never publish searchable data.
* PostgreSQL and object storage are authoritative. Search indexes and caches are derived state.
* Business model calls must use provider-neutral Model Gateway aliases.
* A configured model endpoint is acceptable only when it implements the operation required by the caller, such as chat, embedding, rerank or token counting.
* Keep secrets, credentials, prompts, provider payloads, raw document contents and storage keys out of normal logs and public responses.

## UI Contracts

* Keep UI request and response types aligned with the API.
* Preserve cookie, CSRF, SSE and cancellation semantics when changing related flows.
* Handle loading, empty, success, forbidden, degraded and failed states explicitly.
* User-facing errors must be safe and must not expose sensitive backend details.
* Verify visible behavior with typecheck/build and the smallest relevant Vitest or Playwright path.
* UI guards improve usability but never replace backend authorization.

## Safety

* Preserve unrelated user changes in a dirty worktree.
* Do not discard, overwrite, reset or reformat unrelated changes.
* Do not run destructive Git, database, index, storage or Docker-volume operations without explicit approval.
* Do not commit secrets, credentials, uploads, model files, source archives, generated indexes or large evaluation artifacts.
* Long-running operations must have bounded execution and observable progress.

## Context Discipline

Treat model context as a limited resource.

* Search before reading.
* Prefer `rg` and `rg --files` for repository discovery.
* Never use unrestricted recursive `find`, `grep -R` or `ls -R`.
* Exclude generated or irrelevant trees such as `node_modules`, `dist`, `build`, `coverage` and `.git`.
* Do not recursively inspect logs, databases, binaries, generated artifacts or Codex session/history files unless directly relevant.
* Read only relevant file sections when the whole file is unnecessary.
* Do not rediscover architecture already established by task evidence or repository documentation unless conflicting evidence appears.

### Command Output

* Never dump large files or unbounded command output into context.
* Prefer quiet test and build reporters.
* Prefer targeted filters for noisy commands.
* When output may be large, redirect the complete output to a temporary file and inspect only relevant failures, summaries, head/tail sections or matched ranges.
* Preserve and report the original command's exit code when redirecting or filtering output.
* Do not rely on truncation alone when it could hide the actual error.

### Diffs

* Inspect changed paths or diff statistics before reading a large full diff.
* Prefer task-relevant file diffs during implementation.
* Review the complete relevant diff before completion.

### Tests

* Run targeted tests first.
* Expand validation only as required by the affected boundary.
* Run the full test suite only when the change warrants it or before completion when repository policy requires it.
* Prefer executable evidence over conclusions inferred only from source inspection.

## Continuation State

If the working context becomes long or the task must continue across sessions, record a concise durable state in `docs/STATUS.md` or the task's existing plan.

Record only what is needed to resume:

* goal;
* important decisions;
* changed files or surfaces;
* checks already run and their results;
* blockers;
* remaining work.

Do not use chat history as the only source of truth for unfinished work.

## Command Conventions

* Use stable repository Make targets when available.
* Otherwise use the repository's established `uv`, `pnpm`, `docker compose` or equivalent commands.
* On Windows, use the equivalent supported command when a Make target is unavailable.
* When reporting validation, include the exact command and its exit code.
