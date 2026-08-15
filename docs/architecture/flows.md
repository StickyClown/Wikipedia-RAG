# Main Flows

These flows show ownership and failure boundaries. Public schemas are defined
in the API code; executable invariants are indexed in [contract-map.md](contract-map.md).

## Authentication and Scope

1. Login/OIDC callback creates a server-side session with hashed opaque tokens.
2. Each request resolves `ActorContext` from the session.
3. Tenant, KB role and document access are checked before the operation.
4. Client-supplied KB/document/group/filter identifiers are inputs to authorize,
   never authority by themselves.
5. State-changing cookie requests require CSRF validation.

## Upload and Publication

```text
authorize → create session → presigned upload → complete → durable job
          → validate → parse → normalize → chunk/embed → index → publish
```

- Object paths are generated after tenant/KB authorization.
- Large files are never ingested synchronously inside the HTTP request.
- Worker transitions are retryable and idempotent.
- PostgreSQL owns current publication. Failed/cancelled work remains
  non-searchable.
- Projection reconciliation repairs bounded OpenSearch drift from canonical DB
  state.

Wikipedia/ZIM and external connectors enter the same normalized document,
chunking and publication boundary after source-specific acquisition.

## Search and Answer

1. API authorizes the requested KB scope and retrieval filters.
2. It verifies the active index/retrieval contract.
3. BM25 and dense lanes retrieve tenant/KB-scoped candidates.
4. Fusion, rerank and optional parent expansion produce candidates.
5. PostgreSQL confirms current version, publication and ACL.
6. The answer stage receives only authorized evidence through Model Gateway.
7. Citations retain resolvable document/chunk/index provenance.

A missing/incompatible index fails safely with `KB_NOT_READY`. Stale derived
state may reduce recall but cannot broaden access.

## Chat SSE

The UI sends one authorized chat request and consumes ordered SSE events.
Progress/heartbeat events keep the connection observable; terminal success
contains answer/evidence/query-run identity and terminal failure contains a safe
error. Cancellation or disconnect does not grant a second hidden execution.

## Deep Research

```text
create scoped run → claim lease → plan → bounded tools → evaluate/verify
                  → persist evidence/claims → synthesize → ACL-trimmed report
```

- Scope is a server-owned snapshot of one to three same-tenant KBs.
- Lease and compare-and-set protect concurrent state transitions.
- Tool names and arguments are validated against a closed registry.
- Document content is evidence, not executable instruction.
- Pause, resume, cancel and stale-heartbeat recovery are durable.
- Evidence is re-authorized before model context and public report projection.

## Delete and Purge

Delete authorization immediately marks the document/version out of retrieval,
removes or invalidates derived search state and schedules deferred purge. The
worker removes object artifacts and durable derived rows after retention.
Failures end in a safe retryable/terminal lifecycle state rather than silently
restoring visibility.

## Model Call

```text
business operation → ModelClient alias → Gateway operation contract
                   → endpoint adapter → configured endpoint
```

Provider-specific request/response mapping ends at the Gateway adapter. Remote
and local endpoints use the same business boundary.
