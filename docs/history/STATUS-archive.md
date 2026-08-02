# Status Archive

This archive preserves compact historical status details that were removed from
the first-read [../STATUS.md](../STATUS.md). Detailed implementation history
belongs in [../exec-plans/](../exec-plans/) and immutable validation artifacts
belong under ignored `artifacts/` directories.

## 2026-07-30 Snapshot Before Documentation Restructure

The project status file previously mixed milestone, feature catalogue,
validation journal, local data notes, risks and next work. The compact current
state now lives in [../STATUS.md](../STATUS.md).

Historical implementation references:

- ExecPlan 22: Model Gateway readiness and provider-backed gate rerun.
- ExecPlan 23: Reviewed-gate quality and eval semantics closure.
- ExecPlan 24: Authentication, tenancy and KB access foundation.
- ExecPlan 26: Deterministic eval root-cause diagnostics.
- ExecPlan 27: Document lifecycle hardening and backup/restore contract.
- ExecPlan 28: KB-grant ACL mirroring metadata.
- ExecPlan 29: Eval HTTP client local-auth support.
- ExecPlan 30: Release-gate provider preflight.
- ExecPlan 31: Warm retrieval profiling.
- ExecPlan 32: Parser sandboxing and endpoint pools.
- ExecPlan 33: Public multi-file batch upload.
- ExecPlan 34: Multi-KB direct retrieval.

## Historical Validation Pointers

- Reviewed Wikipedia provider gate passed on 2026-07-30 with `passed=true` and `blocking_failures=0`.
  Report: `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260730T195822Z-reviewed-wikipedia-smoke-v1-release-gate-5b04e45f/report.json`.
- Document corpus verification reports retained from 2026-07-29:
  `artifacts/validation/document-corpus/20260729T205604Z`,
  `artifacts/validation/document-corpus/20260729T205923Z`,
  `artifacts/validation/document-corpus/20260729T210847Z`.
- Document upload verification retained from 2026-07-29:
  `artifacts/validation/document-upload/20260729T210047Z`.
- Cross-tenant hardening smoke command was added and unit/help validation was recorded in prior status history. Runtime hardening smoke remains part of the next approved smoke set.

## Historical Local Data State

The previous status recorded a prepared local data state:

- Real ZIM pages imported: `10,000` canonical non-redirect pages.
- Real ZIM chunks indexed: `14,281`.
- OpenSearch index: `wiki-chunks-387df2fb225f794d`.
- Redirect provenance persisted for the local ZIM snapshot.
- Document corpus reports and downloaded external bytes under ignored `artifacts/`.

This local state is not a project invariant and may differ on another machine.

## Historical Environment Notes

- GNU Make was not available in the Windows host PATH during one document-corpus verification attempt; direct `uv` commands were used successfully.
- Older status entries recorded expected Git LF-to-CRLF working-copy warnings on Windows during `git diff --check`.
- Previous full-project `ruff format --check .` failures were caused by pre-existing untracked `analyze_docs/` content; focused `src tests` checks passed after later increments.

## Superseded Or Corrected Notes

- Older auth-slice text said Multi-KB retrieval was unsupported. ExecPlan 34 superseded this for direct chat/debug retrieval.
- Eval root-cause diagnostics originally required `AUTH_DISABLED=true` for a live short run. ExecPlan 29 superseded this by adding a local-auth eval client.
- Redis/Valkey was described as jobs/cache in older first-read docs. Current source inspection found PostgreSQL-backed job claiming and no Redis client usage in `src/`.
