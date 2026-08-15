# Web Architecture

The React/Vite UI is a single-screen application. It presents application
state; the API remains authoritative for identity, access and lifecycle.

## Main Surfaces

- Login/session and tenant context.
- Knowledge-base creation/selection and role-aware controls.
- Wikipedia import and multi-file document upload with progress.
- Search, filters, pagination and document viewer.
- Chat answer, citations and retrieval debugger.
- Deep Research scope, progress, evidence, findings and lifecycle actions.
- Platform-admin model configuration when authorized.

## Session and Scope

The UI loads the current session and available KBs from the API. Active tenant
is server-owned. A selected KB or research scope is still authorized on every
backend request. Logout clears browser state and invalidates the server session.

## API Client Contract

- Send cookies with application API requests.
- Add `X-CSRF-Token` to state-changing requests when the session supplies it.
- Treat presigned object-storage PUT as a separate bounded upload; never attach
  application cookies or expose object keys.
- Parse typed safe error envelopes and retain request/query-run IDs for support.
- Abort obsolete searches/uploads where supported and prevent stale responses
  from replacing newer state.

## Streaming Chat

Chat consumes ordered SSE events from `fetch(...).body`. The client handles
partial frames, progress, heartbeat, evidence, answer and one terminal event.
On terminal failure it preserves safe diagnostics and any already-public
query-run identity. Retry is an explicit new request.

## Upload

The UI creates upload sessions/batches, uploads selected files to presigned
URLs, completes each session and polls durable item/job state. Per-file progress
and failure remain visible. Reload may discard local `File` objects but does
not discard durable server state.

## Search and Viewer

Search uses the authorized KB scope and returns grouped results, facets and a
bounded cursor. Opening a result fetches authorized document context by public
document/chunk identity; the UI never treats citation labels as authority.

## Deep Research

The UI creates a run over one to three selected KBs, then polls durable detail
and exposes pause/resume/cancel. Evidence and reports are already ACL-trimmed by
the API. Client-side scope limits do not replace backend validation.

## Required UI States

Every user-visible operation handles:

- initial/loading;
- empty;
- success;
- forbidden;
- dependency-degraded;
- terminal failure;
- cancelled/aborted where supported.

Errors are localized for users while preserving safe technical code and request
ID. The browser never receives credentials, storage keys, provider payloads,
raw planner prompts or cross-tenant records.

## Verification

- Type/API changes: `pnpm typecheck` and affected Vitest protocol tests.
- Rendering-only changes: lint, typecheck, build and the smallest Playwright path.
- Auth, upload, search, chat or research changes: verify the visible browser
  path plus the corresponding public API boundary.
