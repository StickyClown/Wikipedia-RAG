# ADR-002 — UI stack

Status: accepted for baseline

## Decision

Use React, Vite and TypeScript with `pnpm`. The first UI is functional rather than design-heavy and covers chat, sources, ingestion jobs and retrieval debugger.

## Consequences

- API remains usable without UI;
- generated API client should be based on OpenAPI when the contract stabilizes;
- accessibility and loading/error/empty states are acceptance requirements;
- no server-side UI framework in the baseline.
