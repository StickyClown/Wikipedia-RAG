# ExecPlan 05 — Retrieval debugger and minimal UI

## Outcome

A user can chat, open sources and inspect query stages, candidate ranks, filters, scores, citations and trace timing in a minimal React UI.

## In scope

- React/Vite app shell;
- chat streaming UI;
- source drawer;
- retrieval-run API;
- candidate/stage tables and timing waterfall;
- loading, empty, error and permission states;
- basic accessibility and browser E2E tests.

## Out of scope

Visual template editor and advanced design system.

## Acceptance criteria

- UI never exposes raw secrets/provider payloads;
- unauthorized query-run IDs return no cross-tenant metadata;
- source links resolve to the exact section/chunk preview;
- browser E2E covers successful stream, failure and debugger flow.

## Validation

```bash
make lint
make typecheck
make test-e2e TEST=ui-chat-debugger
```

## Progress

- [x] Plan refined for local MVP.
- [x] Implemented for local MVP.
- [x] Reviewed with local checks.

## MVP status

- Implemented React/Vite UI for chat, Wikipedia import, upload and retrieval debugger.
- Verified UI HTTP 200 and `pnpm lint`, `pnpm typecheck`, `pnpm format:check`, `pnpm build`.
- Verified debugger API with `{"query":"Россия","top_k":5}`.

## Remaining production work

- Browser-level E2E for visual source drawer states and timing waterfall remains future hardening.
