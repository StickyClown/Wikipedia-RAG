# Project Status

Last updated: 2026-08-17

Detailed history through this date is archived in
[STATUS-2026-08-15.md](history/STATUS-2026-08-15.md).

## Current Goal

ExecPlan 37 is in progress. It replaces tenant/KB-role/document-metadata ACL
authorization with workspace resource grants and OIDC-managed groups through a
clean workspace reset; legacy data conversion is no longer supported. The
workspace-only bootstrap schema now records `010_single_workspace_clean_reset_v1`,
refuses a legacy schema with `WORKSPACE_RESET_REQUIRED`, and seeds the local
user as `PLATFORM_ADMIN`. The destructive reset remains pending removal of the
remaining technical tenant arguments from repository, ingestion and worker
paths. The retained implementation backlog is now Slice 6.5 of ExecPlan 37:
remove every live `tenant_id` argument/predicate/identity (current static
inventory: 1,140 Python occurrences), delete transitional compatibility shims,
then run the guarded reset rehearsal and actual approved local reset.

## Current State

- ExecPlan 37 Slice 0 inventory found tenant/role/document-ACL authority in the
  API handlers, auth service, repository, retrieval/research paths and single
  screen UI; Docker Compose has no running services. Slice 1 now has the
  isolated `workspace_access` policy resolver and deterministic matrix tests.
- The legacy workspace-migration transform/preflight/apply CLI was removed.
  `workspace-reset` now provides a dry-run inventory by default and permits an
  all-data reset only when both `WORKSPACE_RESET_ENABLED=true` and the explicit
  `--apply --all-data-confirmed` command flags are supplied.
- The OpenSearch workspace projection no longer stores or filters `tenant_id`:
  index names/IDs derive from KB scope, while PostgreSQL remains the live
  authorization confirmation boundary. Repository/bootstrap schema removal of
  the remaining tenant columns is still in progress; a reset must not be
  applied until that cutover is complete.
- Workspace grant repository and the new KB/document `access-grants` boundary
  are implemented. The OIDC callback uses strict workspace claim sync before
  session return when `OIDC_GROUP_SYNC_ENABLED=true`; JIT remains opt-in.
- Existing legacy tenant rows are intentionally not transformed. The clean
  deployment starts with workspace ownership, grants and global groups only.
- Group CRUD now has a global workspace boundary: only platform admins may
  list/create/update/delete groups, only local groups can have manual
  membership changes, and deleting a group referenced by a resource grant
  returns bounded `GROUP_IN_USE` rather than silently revoking access.
- KB collection, KB creation and KB opening now resolve current PostgreSQL
  ownership/grants without active-tenant authority. Direct-document readers
  receive only a `partial` KB shell, while full KB readers receive server-owned
  `write_access` and `share_access` capability booleans.
- Direct upload sessions and batches now record the initiating user. Document
  creation persists that session owner as `owner_user_id` and sets
  `inherits_kb_access=true`; the upload completion unit path asserts this
  ownership transfer.
- Public document read, delete and reprocess boundaries now use live workspace
  document authorization. Owner/KB-owner/admin delete access and direct
  document `WRITE` reprocessing are enforced without active tenant authority;
  internal tenant fields remain only for the still-legacy job/index calls.
- Multipart, presigned-session and batch upload entry points now authorize KB
  `WRITE` through the workspace resolver. Their technical legacy tenant value
  is loaded from the target KB after authorization and is never derived from
  the request actor's active tenant.
- Document versions, structure, context and in-document search now all enter
  through the current workspace document resolver before using legacy
  tenant-keyed section/chunk repository calls. The old viewer-scope tests were
  updated to assert this resolver boundary rather than role/metadata ACLs.
- Search now performs a second, batch PostgreSQL authorization pass over every
  cached or OpenSearch-derived result document using current owner/grant/group
  state. This is independent of old indexed ACL metadata and prevents stale
  cache/index candidates from broadening visible results.
- A focused search test proves a revoked document is removed by that batch
  confirmation while an authorized sibling remains; the test asserts the
  candidates are supplied in one batch.
- Search cache fingerprints now also include the actor, current workspace group
  set and monotonic authorization revision. Grant replacement and local/OIDC
  membership changes bump that revision; PostgreSQL reauthorization remains the
  final safety check for cached candidates.
- Persisted Deep Research evidence is batch-reauthorized before report details
  are rendered, so revoked evidence and the claims derived from it are omitted.
- The public router no longer exposes the tenant session selector, tenant admin
  CRUD, legacy KB role-grant CRUD, document metadata ACL PATCH or source ACL
  PATCH. The remaining group selector is authorized by workspace `share`, and
  the UI no longer sends or renders tenant/document/source ACL compatibility
  payloads. Document structure responses expose only workspace capability and
  inheritance fields, and the viewer now includes a grant editor backed by the
  atomic document `access-grants` replacement API.
- The platform-admin Knowledge tab now lists, creates, edits and deletes local
  groups with description and initial membership; OIDC groups remain clearly
  identified as externally managed and do not expose manual-edit controls.
- Source list/create/read/update/healthcheck/sync now derive only an internal
  storage tenant key after workspace KB `READ`/`WRITE` authorization. Source
  creation no longer accepts or writes a document ACL default.
- The obsolete document/source metadata ACL handlers and schemas have been
  removed. Source ingestion no longer projects ACL metadata into chunks or
  updates OpenSearch ACL fields; it strips connector ACL metadata and creates
  external-source documents with the KB owner and `inherits_kb_access=true`.
- OpenSearch no longer reads, fingerprints, filters or mutates
  `metadata.document_access`; stale candidate safety is supplied solely by the
  workspace batch PostgreSQL authorization pass before response.
- Public search now authorizes requested full KB scopes through the workspace
  grant repository before retrieval and no longer constructs legacy document ACL
  scopes. The legacy tenant is only an internal single-scope retrieval key until
  the remaining derived-store migration is complete.
- Search-projection workers now accept only publication events and do not
  compare or repair ACL metadata; access grants and group changes require no
  index mutation.
- The now-unroutable tenant-session and tenant-admin handlers, along with their
  request schemas, have been removed. There is no longer a server endpoint that
  can select or create a tenant at runtime.
- The public authentication-session response no longer exposes an active tenant
  or tenant role. Search no longer interprets, fingerprints, or sends legacy
  `document_access` metadata, and its cache key/trace identity no longer
  includes a tenant; the workspace PostgreSQL batch confirmation is the sole
  candidate-access check in that boundary.
- `ActorContext` now has only workspace identity and request-correlation
  fields and rejects legacy tenant arguments. The unreachable KB-role grant
  handlers, their DTOs, the tenant session selector, and the legacy ACL
  mirroring helper have been removed; remaining tenant-keyed storage calls are
  still being cut over separately.
- The editor-only debug search path now requires workspace KB `WRITE` for each
  requested KB and no longer sends metadata document ACL scopes into retrieval.
  Its unused legacy filter-authorization helpers and tests were removed.
- Repository code no longer has source/document ACL metadata mutation helpers
  or ACL projection-event enqueueing, and chunk retrieval no longer copies
  `document_access` into result metadata. Viewer tests now model only the live
  workspace document resolver.
- Query-run retrieval, feedback and evaluation now each require live workspace
  KB `WRITE` across the stored scope, replacing their legacy editor-role gate.
- Retrieval-profile catalog, KB rename, source-sync inspection, Wikipedia/ZIM
  imports and ingestion job control now use workspace `READ`/`WRITE` checks;
  their focused authorization/API regression set reports 23 passing tests.
- The old `TenantRole`/`KnowledgeBaseRole` runtime, role-composition SQL and
  `document_access.py` module have been removed. Remaining `document_access`
  text is limited to one-time migration conversion, safe stripping of legacy
  input, and the new document grant route names.

- API, worker, PostgreSQL, OpenSearch, MinIO, parser services and Model Gateway
  returned `ok`; the active Gateway revision resolves the required operations.
- Direct upload accepts the typed `source_ref_v1` identity contract. Server-owned
  `(tenant, KB, namespace, external_id)` identity controls document reuse and
  version creation.
- `source_provenance_v1` is projected through document, search, answer and
  Extended/Deep Research evidence paths. User attributes cannot override
  identity, ACL, storage, checksum or filename fields.
- The signed runtime-binding v2 builder rejects stale, tampered, unresolved and
  ambiguous evidence. Creating a real binding requires
  `WIKIPEDIARAG_EVAL_BINDING_SIGNING_KEY`.
- The P0.1 quality workflow is complete: prepare, review, freeze, ingest,
  resumable dev/test runs, compatibility grouping and safe reports are covered.
  Its 220-row synthetic fixture validates workflow execution, not production
  retrieval quality.
- The controlled RRNCB suite is frozen as `p0-search-quality-v2` at dataset hash
  `40b9cf9b6b4c359d3f8cd1b95006be48f05741f419b2406b1ba28d15399f34b4`:
  65 pinned PDFs, 200 base questions and 1,000 `ru/en/uk/de/ko` retrieval tasks
  split into 200 dev and 800 test tasks.
- Retrieval-only RRNCB execution now emits overall, per-language and paired-to-
  Russian Recall@10, MRR@10, nDCG@10 and latency metrics under one immutable
  dev/test run contract. The execution and acceptance contract is documented in
  [p0-search-quality-v2.md](p0-search-quality-v2.md).

## Active Execution

The two historical degraded reconciliation rows belonged to one disposable
upload-verification knowledge base whose active index version no longer had an
authoritative database record. Reconciliation now persists the safe terminal
code `SEARCH_PROJECTION_INDEX_UNAVAILABLE`; the disposable KB was removed via
the public lifecycle API. Reconciliation is clean: zero pending and zero
degraded rows.

RRNCB ingestion run `p0-search-quality-v2-ingest-real2-40b9cf9b6b4c` completed
with the real `sota_mvp`/`upload_sota_mvp` embedding contract: 65/65 documents,
14,396 chunks, failed=0. All documents used the Xberg parser route; the
average document time was 46.4 s (maximum 332.9 s) under concurrency=2. The
first run used a stale `upload_mock` worker container and is isolated as an
invalid attempt; its results are not used. Readiness remains clean with zero
pending/degraded search-projection rows.

The dev and test retrieval slices are complete (200/200 and 800/800,
failed=0). The full 1,000-task report is compatible with one immutable index
contract: Recall@10=0.949, MRR@10=0.877, nDCG@10=0.895, latency p50=2,345 ms
and p95=9,441 ms. Per-language Recall@10 is RU 0.985, EN/UK 0.965, DE 0.935
and KO 0.895; paired deltas versus RU are EN -0.020, UK -0.020, DE -0.050
and KO -0.090. The reference report is stored under
`artifacts/eval/rrncb-public/p0-search-quality-v2/runs/p0-search-quality-v2-retrieval-real-40b9cf9b6b4c/retrieval-report.json`.

RRNCB has `evaluation_granularity=document`: its source rows do not provide
`gold_section_ids` or `gold_chunk_ids`. Therefore chunk/section Recall, chunk
MRR/nDCG and chunk-based root-cause diagnostics are **N/A** for this run, not
zero-valued quality results or retrieval failures. The baseline does not claim
answer correctness, citation quality or exact evidence-chunk retrieval.

P0.1 is closed. Human translation sign-off, translation cleanup and any
evidence-level RRNCB benchmark are future work; they do not block this closure.

## Latest Relevant Validation

- Clean workspace bootstrap: an isolated PostgreSQL 16.4 instance on
  `127.0.0.1:55433` initialized successfully with `ensure_schema`; inspection
  reported `legacy_columns=0`, ledger `010_single_workspace_clean_reset_v1=1`
  and `bootstrap_role=PLATFORM_ADMIN`. A second `ensure_schema` call was
  idempotent. The disposable container was then removed; no project volume or
  configured store was reset.

- Clean-reset/derived-search boundary: `uv run pytest
  tests/unit/test_retrieval_current_state.py tests/unit/test_workspace_reset.py
  tests/unit/test_document_deletion_lifecycle.py tests/unit/test_external_sources.py
  -q -x` — 21 passed (exit 0). Focused Ruff, format and Mypy checks passed.
  `workspace-reset --apply` correctly exits 2 with
  `WORKSPACE_RESET_CONFIRMATION_REQUIRED` before connecting to any store.

- `uv run pytest tests/unit/test_workspace_access.py -q` — 4 passed (exit 0).
- `uv run pytest tests/unit/test_workspace_access.py tests/unit/test_workspace_migration.py -q`
  — 6 passed (exit 0).
- `uv run mypy src/wikipediarag/workspace_access.py src/wikipediarag/workspace_migration.py`
  — passed (exit 0).
- Isolated PostgreSQL bootstrap and `workspace-migration-preflight --json`
  against `127.0.0.1:55432` — passed (exit 0); preflight digest
  `1d9ee3d2480870aa9e3e75d69eb529501d8a820521a2ebd1d50e023d252a6f19`.
- Isolated `workspace-migration-apply --apply --backup-confirmed` rehearsal
  against `127.0.0.1:55432` — passed (exit 0); the seeded KB owner was
  backfilled successfully.
- `uv run ruff check src/wikipediarag/workspace_access.py tests/unit/test_workspace_access.py`
  and `uv run ruff format --check src/wikipediarag/workspace_access.py
  tests/unit/test_workspace_access.py` — passed (exit 0).
- `uv run pytest tests/unit/test_workspace_research_access.py tests/unit/test_deep_research.py -q`
  — 57 passed (exit 0).
- `uv run pytest tests/unit/test_search_service.py -k "workspace_cache_marker or workspace_candidate" -q`
  — 2 passed, 7 deselected (exit 0); `ruff`, `mypy` and `git diff --check` for
  the changed workspace/search/OIDC modules also passed (exit 0).
- `WIKIPEDIARAG_INTEGRATION_DATABASE_URL=postgresql+asyncpg://rag:…@127.0.0.1:55432/rag`
  and `WIKIPEDIARAG_INTEGRATION_OPENSEARCH_URL=http://127.0.0.1:9200`
  `uv run pytest tests/integration/test_deep_research_persistence.py
  tests/integration/test_search_projection_reconciliation.py -q -x` — 5 passed
  (exit 0) against disposable Compose services.
- `uv run pytest tests/e2e/test_local_contract.py -q` — 1 passed (exit 0).
- `cd services/ui && pnpm typecheck` — passed (exit 0); focused
  `pnpm vitest run src/App.protocol.test.ts --reporter=dot` — 3 passed (exit
  0). The unfiltered Vitest command did not return a final status within the
  bounded 30-second execution window, so it is intentionally not recorded as
  passing.
- After legacy route/UI removal: `uv run pytest tests/unit/test_architecture_boundaries.py
  tests/unit/test_auth_schema.py tests/unit/test_workspace_grants.py -q -x` —
  10 passed (exit 0); router `ruff`/`mypy`, UI `pnpm typecheck`, and focused
  `pnpm vitest run src/App.protocol.test.ts --reporter=dot` — 3 passed (exit
  0).
- Document grant-editor cutover: `uv run pytest tests/unit/test_document_viewer.py
  tests/unit/test_workspace_grants.py tests/unit/test_workspace_access.py -q` —
  15 passed (exit 0); `mypy`, Python `ruff`, UI typecheck and focused UI tests
  passed. OpenAPI inspection confirms the five removed legacy route paths are
  absent.
- Source boundary cutover: `uv run pytest tests/unit/test_external_sources.py
  tests/unit/test_architecture_boundaries.py -q -x` — 14 passed (exit 0), with
  Python `ruff`, `mypy` and `git diff --check` also passing.
- ACL-projection removal: `uv run pytest tests/unit/test_external_sources.py
  tests/unit/test_document_ingestion.py tests/unit/test_ingestion_publication_invariants.py -q -x`
  — 22 passed (exit 0); `ruff` and `mypy` for ingestion/repository passed.
- Index ACL removal: `uv run pytest tests/unit/test_search_projection_worker.py
  tests/unit/test_retrieval_current_state.py -q -x` — 10 passed (exit 0), and
  focused workspace search checks — 2 passed (exit 0). The broad search-service
  command did not return within the bounded window and is not recorded passing.
- Worker ACL-event removal: `uv run pytest tests/unit/test_search_projection_worker.py
  tests/unit/test_retrieval_current_state.py -q -x` — 10 passed (exit 0), with
  `ruff`, `mypy` and diff check passing.
- Session/search ACL cleanup: `uv run pytest tests/unit/test_auth_service.py
  tests/unit/test_api_routing.py tests/unit/test_auth_schema.py -q -x` —
  20 passed (exit 0). `ruff` and `mypy` passed for the changed session schema
  and handlers. `uv run pytest tests/unit/test_search_service.py -k
  "workspace_candidate_confirmation or workspace_cache_marker or filter_ast"
  -q -x` — 3 passed, 6 deselected (exit 0); `test_search_endpoint.py` —
  4 passed (exit 0). The full search-service command again did not complete
  within its bounded 30-second window and is intentionally not recorded as
  passing.
- Current workspace authorization regression check: `uv run pytest
  tests/unit/test_workspace_access.py tests/unit/test_workspace_grants.py
  tests/unit/test_workspace_oidc_groups.py tests/unit/test_workspace_research_access.py
  -q -x` — 11 passed (exit 0); `uv run pytest tests/e2e/test_local_contract.py
  -q -x` — 1 passed (exit 0).
- Group-administration UI completion: `cd services/ui && pnpm vitest run
  src/App.protocol.test.ts --reporter=dot` — 4 passed (exit 0); it verifies
  local edit/delete controls and OIDC read-only rendering. `pnpm typecheck`
  and `pnpm build` also passed (exit 0); `pnpm lint` exited 0 with one existing
  React hook dependency warning. `uv run pytest tests/unit/test_workspace_access.py
  tests/unit/test_workspace_grants.py tests/unit/test_workspace_oidc_groups.py
  tests/unit/test_workspace_research_access.py tests/unit/test_document_viewer.py
  -q -x` — 19 passed (exit 0). `uv run ruff check src tests`, `uv run ruff
  format --check src tests`, and `uv run mypy src tests` all passed (exit 0).
  The disposable Compose PostgreSQL/OpenSearch integration suite passed 19 tests
  (exit 0) with its locally resolved test connection.
- Retrieval/chat ACL cutover: the base retrieval pipeline no longer evaluates
  `document_access` metadata. Chat requires a full workspace KB scope before
  resolving its transitional durable-storage partition; partial shells cannot
  be used for chat. `uv run pytest tests/unit/test_api_contract_abstention.py
  tests/unit/test_api_routing.py tests/unit/test_gateway_app.py
  tests/unit/test_search_endpoint.py -q -x` — 36 passed (exit 0; two existing
  FastAPI lifespan deprecation warnings). The disposable PostgreSQL/OpenSearch
  integration command passed 5 tests (exit 0) using the Compose credentials.
- Deep Research/Extended Search now use workspace batch document authorization
  for durable evidence and document-bounded neighbors; their planner/tool paths
  no longer inspect legacy document ACL metadata. `uv run pytest
  tests/unit/test_deep_research.py tests/unit/test_workspace_research_access.py
  tests/unit/test_extended.py -q -x` — 66 passed (exit 0). `ActorContext` and
  session loading no longer retain active-tenant or tenant-role authority;
  `uv run pytest tests/unit/test_auth_service.py tests/unit/test_auth_policy.py
  tests/unit/test_workspace_oidc_groups.py tests/unit/test_api_routing.py -q
  -x` — 28 passed (exit 0).
- Research-run read/control authorization now resolves the run KB through the
  workspace grant repository. There are no remaining production references to
  `actor.active_tenant_id`, `actor.tenant_role`, or legacy
  `document_access_scope(s)` in retrieval, Extended Search, Deep Research,
  research tools, or public search. `uv run pytest tests/unit -q -x` —
  551 passed (exit 0; two existing FastAPI lifespan deprecation warnings).
- Architecture contract invariants for XML/ZIM/retrieval were restored;
  `uv run pytest tests/integration -q -x` against the disposable Compose
  PostgreSQL/OpenSearch services — 19 passed (exit 0).
- Isolated workspace migration rehearsal repeated successfully: preflight and
  guarded `workspace-migration-apply --apply --backup-confirmed` accepted the
  current digest `1d9ee3d2480870aa9e3e75d69eb529501d8a820521a2ebd1d50e023d252a6f19`
  against Compose PostgreSQL (exit 0). No production database was touched.
- The prior focused legacy authorization/retrieval/research test command was
  started, but its integration component could not complete while Compose was
  stopped; it is not recorded as passing.
- Disposable Compose PostgreSQL could not start because port `5432` is occupied
  by an unrelated `pub-postgres` container. A separate `wikipediarag-plan37`
  Compose project now runs only a new PostgreSQL container on port `55432`; no
  external container or database was modified. Its bootstrapped-schema preflight
  completed and reported 38 tenant-scoped tables. Full data apply/reindex
  rehearsal remains pending the completed transform.

- Provenance/upload/chunker/eval-binding and quality tests: 29 passed.
- Search service tests: 7 passed; document viewer tests: 10 passed.
- Full unit suite: 539 passed with 2 deprecation warnings.
- Full-worktree Ruff check and format check passed; Mypy `src tests` passed.
- Functional retrieval equivalent on Windows: 1 passed.
- Synthetic P0.1 run completed 44 dev and 176 test questions with no execution
  errors. Results correctly stayed separated across five incompatible index
  revisions; they are not a production quality baseline.
- Documentation cleanup: relative Markdown links passed across 13 active/archive
  files and `git diff --check` passed.
- RRNCB reconciliation, multilingual freeze, retrieval reporting and readiness
  coverage: 23 focused tests passed; Ruff and Mypy passed for the changed CLI
  and benchmark modules.
- Translation generation completed 800/800 records. Full-matrix script QC found
  and repaired two Korean-script violations before freeze.
- Real RRNCB retrieval report: 1,000/1,000 completed with five languages and
  200 paired RU comparisons per non-RU language; `compatible=true` and
  document-level Recall@10/MRR@10/nDCG@10 of 0.949/0.877/0.895.
- RRNCB metric coverage was reviewed: its task manifest has no section or chunk
  evidence anchors, so those metric families are intentionally N/A.

The focused retrieval-runner checks remain 31 passed; Ruff, Mypy and
`git diff --check` pass for the changed execution and status files.

## Deferred Follow-up

1. Create an evidence-level RRNCB subset only after adding reviewed section and
   chunk anchors; then measure exact context recall and chunk ranking.
2. Review the 17/200 Korean questions containing mixed Han characters and the
   source questions that are too broad to identify one gold document.
3. Revisit the regression gate when a future retrieval task defines accepted
   metric scope and thresholds.
