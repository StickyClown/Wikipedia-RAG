# Project Status

Last updated: 2026-08-02

## Current milestone

Deep Research V1 has been implemented on top of the Search Quality V2 and
Extended Search baseline. It is a durable single-KB lifecycle with worker
episodes, typed evidence memory, coverage records, operational reflections,
pause/resume/cancel and an ACL-trimmed report surface.

The latest increment ran the mock-provider Deep Research runtime smoke and
offline quality/context matrix. Runtime default packing remains 45% productive
target until a true runtime policy override or local-Qwen experiment confirms a
safe improvement.

## Implemented now

- Local production-shaped RAG MVP with FastAPI API, worker, Model Gateway, React/Vite UI and Docker Compose dependencies.
- Local auth and OIDC foundation: Argon2id local passwords, opaque HttpOnly session cookie, CSRF, OIDC Authorization Code + PKCE, server-side encrypted provider tokens and `ActorContext`.
- Tenant and KB authorization foundation: platform, tenant and KB roles; local/OIDC groups; KB grants; route-level server-owned tenant/KB enforcement; safe error envelopes.
- Wikipedia ingestion from local ZIM/libzim with Kiwix source URLs, deterministic chunks, redirects as provenance and versioned OpenSearch publication; XML multistream fallback remains available.
- Async document upload and batch upload through presigned MinIO URLs, background validation/parsing/chunking/embedding and published-only retrieval.
- Document lifecycle hardening: KB-owner soft delete, immediate retrieval removal and deferred idempotent purge after the configured retention window.
- Parser runtime hardening in Compose: Xberg, Docling and metadata-service with sandbox settings, separate concurrency limiters and safe parser progress metadata.
- Retrieval: BM25, dense vectors, RRF, rerank, dedup/page quota, parent expansion, answerability, citation validation and experimental safe diagnostics.
- Direct Multi-KB chat/debug retrieval with all-KB role validation and all-KB readiness preflight. Extended Search remains single-KB.
- Ordinary user search: document-visibility-scoped `POST /api/v1/search` now
  runs through the hybrid retrieval pipeline with OpenSearch BM25/vector search, RRF,
  optional rerank, typed filter expressions, cursor pagination, optional
  highlights, facets and document grouping while preserving safe
  `KB_NOT_READY` handling and exact document-viewer open action by `chunk_id`.
- Document navigation: `document_sections` persisted from published chunks,
  section-aware uploaded-document chunking, deterministic Wikipedia/ZIM chunk
  ordinals/locators, viewer-safe document structure/context/in-document search
  APIs and an inline React text viewer with TOC, local document search and
  neighboring chunk context.
- External source connection foundations: `knowledge_sources` now carry
  encrypted connector credentials, refresh metadata and sync cursors; source
  document states and sync-run journals track source versions, content hashes,
  current document versions and tombstones; APIs manage sources, healthchecks
  and sync jobs; worker scheduling enqueues due incremental source syncs.
- Stage 4 connectors: Confluence Data Center, Jira Data Center, GitLab
  Self-Managed, local folder, internal crawler, Kiwix/ZIM source-version
  tracking and deterministic Sunduk/DocSmart mocks share a connector contract
  that feeds the existing document ingestion pipeline.
- React/Vite source management UI now exposes the Stage 4 source kinds for the
  selected KB with safe create/list, healthcheck, incremental/full sync,
  disable/enable and sync-run status polling without returning stored
  credentials to the browser.
- Upload parity: `POST /api/v1/knowledge-bases/{kb_id}/documents` accepts
  multipart uploads through the API and reuses the same document upload records
  and async worker ingestion path as UI drag-and-drop uploads.
- Query-run observability V1: chat/debug queries persist protected query runs and retrieval events with stable stage names, query transforms, candidate rank movement, decision reasons, answerability reason codes, Model Gateway safe metadata, feedback/evaluation event endpoints and an updated Retrieval Debugger. PostgreSQL remains the source of truth; OpenTelemetry export is optional/no-op when packages are absent.
- Retrieval observability now carries explicit per-run `transform_id`/`subquery_id` query context through direct, extended and Multi-KB stage events, candidate debug payloads, extended harness search yield metrics and answer context source summaries.
- Search Quality V2 ACL/security trimming: documents are KB-visible by default,
  can be tenant-visible for all active-tenant actors, or restricted through
  trusted `metadata.document_access`; platform admins, tenant admins and KB
  managers/owners bypass document trimming, while restricted documents also
  support matching user/group allowlists. KB managers can update document access
  and source document access defaults through API/UI, and source defaults can be
  applied to existing synced documents. The same scope is applied to ordinary
  search, chat/debug retrieval, Multi-KB retrieval, Extended Search neighbor
  expansion, DB dense fallback and document viewer paths.
- Durable Deep Research V1: `research_runs`, episodes, questions, typed
  evidence records, minimal claim records, coverage records and operational
  reflections are persisted in PostgreSQL; API endpoints create/list/read runs,
  expose compact events and request pause/resume/cancel; worker jobs with
  `kind='deep_research'` process one bounded single-KB episode at a time,
  checkpoint after every episode, link episode retrieval to `query_runs` for
  debugger compatibility and synthesize the public report only from current
  ACL-visible evidence. Context budgets are ratios of the retrieval profile's
  declared max context: 45% productive target, 55% soft limit, 70% hard input
  limit, 15% output reserve and 15% safety reserve.
- Eval and validation tooling: local-auth eval client, release-gate provider preflight, warm retrieval profiling, MIRACL-RU adapter, document corpus verification, cross-tenant hardening smoke command and Deep Research complex fixture/evaluator/runtime smoke harness.

## In progress

- Deep Research policy tuning beyond V1: add a runtime policy override and run
  optional local Qwen/qwen3.6-27b validation before changing the default 45%
  productive context target.
- Browser UI OIDC through Dockerized API after the public/internal Keycloak URL strategy is decided.
- External deployment hardening remains planned, not supported production operation.

## Latest validation

- Deep Research runtime smoke and policy matrix validation on 2026-08-02:
  `make` was unavailable in the Windows shell, so the Makefile-equivalent `uv`
  commands were run directly. `uv run python -m wikipediarag.cli
  deep-research-smoke` -> exit 0, passed=true, 10/10 fixtures passed with
  compose rebuild/start, API readiness, platform-admin login, seeded viewer,
  upload-backed ingestion, ACL trimming and pause/resume/cancel lifecycle.
  Runtime report:
  `artifacts/validation/deep-research/20260802T152743Z/report.json`.
  The smoke exposed and fixed four harness/runtime issues during the run:
  host-side DB seeding now rewrites compose hostname `postgres` to `localhost`
  when needed, tenant-membership seeding no longer assumes an `updated_at`
  column, smoke auth refreshes CSRF after `/auth/session`, and
  `create_research_run` now inserts the `ingestion_jobs` row before the
  `research_runs.active_job_id` FK reference. It also exposed a pre-claim
  lifecycle bug: pause/cancel now marks received Deep Research jobs cancelled
  and moves the run to `paused`/`cancelled` instead of leaving
  `received/cancel_requested=true`. `uv run python -m wikipediarag.cli
  deep-research-matrix` -> exit 0, passed=true, 27 policy aggregates and 270
  fixture-policy rows passed; offline recommended policy:
  `target_35_abstracts_only_none`; report:
  `artifacts/validation/deep-research-matrix/20260802T153246Z/report.json`.
  This recommendation is offline/synthetic, so the runtime 45% default remains
  unchanged until measured through runtime policy override or local-Qwen
  validation. Final stable checks after fixes:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 128 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 128 source files;
  `uv run pytest tests\unit -q` -> exit 0, 266 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
- Deep Research testing harness validation on 2026-08-02:
  Added `tests/fixtures/deep_research/research_tasks.json` with 10 complex
  synthetic tasks, `wikipediarag.deep_research_eval` fixture validation and
  offline scoring, `deep-research-smoke` CLI/Make target with report/JUnit
  artifact output and architecture/operation docs. Deterministic validation:
  `uv run ruff format src tests` -> exit 0, 128 files left unchanged;
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 128 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 128 source files;
  `uv run pytest tests\unit -q` -> exit 0, 260 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `uv run python -m wikipediarag.cli deep-research-smoke --help` -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
  Runtime `make deep-research-smoke` was not run in this deterministic pass.
- Durable Deep Research V1 validation on 2026-08-02:
  Added durable single-KB research lifecycle tables, repository operations,
  `POST/GET /api/v1/research-runs`, detail/events and pause/resume/cancel
  endpoints, worker execution via `ingestion_jobs.kind='deep_research'`,
  context budget/packing policy, ACL-trimmed report rebuild, minimal React UI
  panel and architecture/status docs. Validation:
  `uv run ruff format src tests` -> exit 0, 126 files left unchanged;
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 126 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 126 source files;
  `uv run pytest tests\unit -q` -> exit 0, 253 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `pnpm lint` in `services/ui` -> exit 0;
  `pnpm typecheck` in `services/ui` -> exit 0;
  `pnpm build` in `services/ui` -> exit 0;
  `pnpm format:check` in `services/ui` -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
- Search Quality V2 ACL/security trimming validation on 2026-08-02:
  Extended trusted `metadata.document_access` trimming to `kb`, `tenant` and
  `restricted` policies with admin/manager bypass across public search,
  chat/direct retrieval, Multi-KB retrieval, Extended Search neighbor
  expansion, debug search and document viewer paths. KB managers can update
  document access and source document access defaults through API/UI; source
  defaults can be applied to existing synced documents and inherited by future
  source syncs. Direct client upload metadata is normalized back to KB-visible
  access unless it comes from trusted source sync. Validation:
  `uv run ruff format src tests` -> exit 0, 7 files reformatted;
  `uv run ruff format --check src tests` -> exit 0, 123 files already
  formatted;
  `uv run ruff check .` -> exit 0;
  `uv run mypy src tests` -> exit 0, no issues found in 123 source files;
  `uv run pytest tests\unit -q` -> exit 0, 245 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `pnpm lint` in `services/ui` -> exit 0;
  `pnpm typecheck` in `services/ui` -> exit 0;
  `pnpm build` in `services/ui` -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
- Backend search and Deep Research documentation validation on 2026-08-02:
  Added `docs/architecture/search-and-deep-research.md` as the canonical
  backend/product contract for ordinary search, debug search, chat retrieval,
  current single-KB Extended Search and planned durable Deep Research.
  Documentation indexes were updated in `README.md` and
  `docs/architecture.md`. Claims were checked against
  `src/wikipediarag/search_service.py`, `src/wikipediarag/retrieval.py`,
  `src/wikipediarag/extended.py`, `src/wikipediarag/api/handlers.py`,
  `src/wikipediarag/retrieval_contract.py`, `src/wikipediarag/schemas.py` and
  `config/retrieval.yaml`. Validation:
  `rg -n "search-and-deep-research|Detailed Architecture|Documentation Map" README.md docs`
  -> exit 0.
- Backend API refactoring validation on 2026-08-02:
  API routes were split into domain `APIRouter` modules under
  `src/wikipediarag/api/`, while `wikipediarag.api_app:app` remains the public
  service entrypoint. Chat and debug-search orchestration now have explicit
  service entry functions, and endpoint/large-helper docstrings were added
  without changing public routes, schemas, auth/CSRF checks, SSE event names or
  retrieval behavior. Validation:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 122 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 122 source files;
  `uv run pytest tests\unit -q` -> exit 0, 229 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `docker compose config --quiet` -> exit 0.
- Search/deep-research runtime validation on 2026-08-02:
  `docker compose up -d --build api worker ui` -> exit 0;
  authenticated API smoke created KB `82d4f568-cb38-408b-bcea-dfb3f7e9ff2a`,
  uploaded document `doc:04ce3e7844c7fb96b1469765`, observed ingestion job
  `8382d53e-18d5-4e05-80fa-02373c2666b9` complete with
  `chunks_published=1`, verified `POST /api/v1/search` without explicit
  `ranking_profile` returned HTTP 200 with 1 result, facets, groups,
  highlights, `chunk_id`, source metadata and score/ranks, verified document
  structure/context APIs, then verified extended chat completed with query run
  `b1e18d2d-52ea-4c59-a678-850567813acc` and retrieval events including
  extended harness stages. Artifact:
  `artifacts/validation/runtime-smokes/20260802T085033Z-search-v2-deep-research/report.json`.
  The runtime check exposed that upload search needs a compatible default
  profile when clients omit `ranking_profile`; public search now infers
  `upload_sota_mvp`/`upload_mock` from active upload index metadata for
  homogeneous upload scopes. Browser connector validation against
  `http://localhost:5173` with local auth `admin` selected the same KB,
  narrowed retrieval scope to that KB, searched
  `SEARCH_V2_DEEP_RESEARCH_MARKER_20260802T085033Z`, observed the result with
  facets, document grouping, highlight text and `score 0.929`, opened it in the
  document viewer, and ran Chat in `Extended` mode with `upload_sota_mvp`;
  the UI produced an answer with citation `[S1]`, the marker evidence and an
  enabled Debug action. Final readiness checks:
  `Invoke-RestMethod http://localhost:8000/ready` -> exit 0, status ok;
  `Invoke-RestMethod http://localhost:8081/ready` -> exit 0, status ok.
  Post-fix stable checks: `uv run pytest
  tests\unit\test_search_endpoint.py tests\unit\test_search_service.py -q`
  -> exit 0, 8 passed, 2 warnings; `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0;
  `uv run mypy src tests` -> exit 0, no issues found in 102 source files;
  `uv run pytest tests\unit -q` -> exit 0, 228 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `cd services/ui; pnpm lint` -> exit 0; `cd services/ui; pnpm typecheck`
  -> exit 0; `cd services/ui; pnpm build` -> exit 0;
  `cd services/ui; pnpm format:check` -> exit 0;
  `docker compose config --quiet` -> exit 0.
- Search Quality V2 foundation validation on 2026-08-02:
  `uv run pytest tests\unit\test_search_endpoint.py tests\unit\test_search_service.py -q`
  -> exit 0, 7 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\retrieval.py src\wikipediarag\search_service.py src\wikipediarag\api_app.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py`
  -> exit 0;
  `uv run mypy src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\retrieval.py src\wikipediarag\search_service.py src\wikipediarag\api_app.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py`
  -> exit 0, no issues found in 7 source files;
  `uv run ruff format --check src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\retrieval.py src\wikipediarag\search_service.py src\wikipediarag\api_app.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py`
  -> exit 0, 7 files already formatted;
  `uv run pytest tests\unit\test_retrieval_answering.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py -q`
  -> exit 0, 29 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 227 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 102 files already formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 102 source files;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0;
  `cd services/ui; pnpm format:check` -> exit 0;
  `docker compose config --quiet` -> exit 0.
- Stage 4 source UI validation on 2026-08-02:
  `cd services/ui; pnpm exec prettier --write src/App.tsx src/styles.css`
  -> exit 0;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0;
  `cd services/ui; pnpm format:check` -> exit 0;
  `uv run pytest tests\unit\test_external_sources.py -q -k "local_folder_connector_reports_changes_and_full_sync_tombstones or corporate_mock_connectors_freeze_contracts"`
  -> exit 0, 2 passed, 5 deselected, 2 warnings;
  Vite dev server was started with `pnpm exec vite --host 127.0.0.1 --port 5174`
  and `Invoke-WebRequest http://127.0.0.1:5174` -> exit 0, HTTP 200.
- Stage 4 external source foundations validation on 2026-08-02:
  `uv run ruff check src\wikipediarag\source_connectors.py src\wikipediarag\api_app.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\search_index.py src\wikipediarag\worker.py src\wikipediarag\schemas.py tests\unit\test_external_sources.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\source_connectors.py src\wikipediarag\api_app.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\search_index.py src\wikipediarag\worker.py src\wikipediarag\schemas.py tests\unit\test_external_sources.py`
  -> exit 0, 8 files already formatted;
  `uv run mypy src\wikipediarag\source_connectors.py src\wikipediarag\api_app.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\search_index.py src\wikipediarag\worker.py src\wikipediarag\schemas.py tests\unit\test_external_sources.py`
  -> exit 0, no issues found in 8 source files;
  `uv run pytest tests\unit\test_external_sources.py tests\unit\test_upload_batches.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py tests\unit\test_document_ingestion.py -q`
  -> exit 0, 35 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 224 passed, 4 warnings;
  `docker compose config --quiet` -> exit 0.
- Stage 3 document navigation implementation validation on 2026-08-01:
  `uv run pytest tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 33 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\db.py src\wikipediarag\document_ingestion.py src\wikipediarag\wiki_dump.py src\wikipediarag\zim_dump.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\schemas.py src\wikipediarag\api_app.py tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\db.py src\wikipediarag\document_ingestion.py src\wikipediarag\wiki_dump.py src\wikipediarag\zim_dump.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\schemas.py src\wikipediarag\api_app.py tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py`
  -> exit 0, 11 files already formatted;
  `uv run mypy src\wikipediarag\document_ingestion.py src\wikipediarag\wiki_dump.py src\wikipediarag\zim_dump.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\schemas.py src\wikipediarag\api_app.py tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py`
  -> exit 0, no issues found in 10 source files;
  `docker compose config --quiet` -> exit 0;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Stage 3 browser/MinIO upload smoke on 2026-08-01:
  Browser connector drove the React UI at `http://localhost:5173` with local
  auth `admin`, created KB `f763e161-9392-44f9-b0f4-c1d0cbc431f7`, uploaded
  `artifacts/validation/runtime-smokes/stage3-browser-upload-fixture-retry.md`
  through the UI file chooser and MinIO-backed upload path, and observed batch
  completion with `1 completed`, `0 failed`, `1 published`,
  parser `local_text_adapter`.
  Ordinary search for
  `STAGE3_BROWSER_MINIO_MARKER_20260801_232237` returned the uploaded document
  with `Open in viewer`; opening the result loaded `.document-viewer` with one
  `.document-chunk.highlighted`, marker text and locator chips
  `page: 1` / `block_index: 1`. In-document search returned the same hit, hit
  click reloaded the highlighted chunk, and TOC click loaded section context.
  The smoke initially exposed asyncpg `AmbiguousParameterError` on nullable
  `document_version_id` predicates in section replacement and viewer reads;
  fixed by explicit `CAST(:document_version_id AS text)` in the affected
  repository SQL.
  Follow-up checks:
  `uv run pytest tests\unit\test_document_viewer.py -q` -> exit 0, 6 passed,
  2 warnings;
  `uv run pytest tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 34 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\repository.py tests\unit\test_document_viewer.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\repository.py tests\unit\test_document_viewer.py`
  -> exit 0, 2 files already formatted;
  `uv run mypy src\wikipediarag\repository.py tests\unit\test_document_viewer.py`
  -> exit 0, no issues found in 2 source files;
  `docker compose up -d --build api worker` -> exit 0;
  final `Invoke-RestMethod http://localhost:8000/ready` -> exit 0, status ok
  for PostgreSQL and Model Gateway.
- Stage 2 ordinary search implementation validation on 2026-08-01:
  `uv run pytest tests\unit\test_auth_service.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_cli_cross_tenant_hardening.py tests\unit\test_search_endpoint.py tests\unit\test_api_auth_disabled.py tests\unit\test_multi_kb_retrieval.py -q`
  -> exit 0, 40 passed, 2 warnings;
  `uv run pytest tests\unit\test_document_deletion_lifecycle.py tests\unit\test_upload_batches.py tests\unit\test_acl_mirroring_metadata.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 26 passed, 2 warnings;
  targeted `uv run mypy` for the changed backend/test files -> exit 0;
  `uv run ruff check` for changed backend/test files -> exit 0;
  `uv run ruff format --check` for changed backend/test files -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
  Full `uv run mypy src tests` was also run at this point and exposed
  test-only typing issues that were closed in the following validation entry:
  `tests\unit\test_retrieval_answering.py:148-149` and
  `tests\unit\test_extended.py:55`.
  Runtime: `docker compose up -d --build` -> exit 0;
  `Invoke-RestMethod -Uri http://localhost:8000/ready` -> exit 0 after
  startup, status ok for PostgreSQL and Model Gateway;
  `uv run python -m wikipediarag.cli verify-document-upload --skip-compose`
  -> exit 0, passed=true, report
  `artifacts/validation/document-upload/20260801T181338Z`;
  `uv run python -m wikipediarag.cli verify-cross-tenant-hardening --skip-compose`
  -> exit 0, passed=true, report
  `artifacts/validation/cross-tenant-hardening/20260801T181428Z`.
  Ordinary search runtime smoke used local auth + CSRF and KB
  `eff3437d-f000-45d1-88fb-3a1d78f8ab67`: filtered
  `POST /api/v1/search` for `verify-metadata` returned HTTP 200 with 1 result,
  source metadata and locator, artifact
  `artifacts/validation/runtime-smokes/20260801T212051Z/ordinary-search-title-filtered.json`.
  Document deletion runtime smoke on `doc:a43385f99cb1eb1a043244a8` returned
  lifecycle `deleting`, created deferred purge job
  `cd045ccc-a27b-42ac-861e-44c3f6f5cf55`, and the deleted document was absent
  from ordinary search after delete; artifacts
  `artifacts/validation/runtime-smokes/20260801T212106Z/document-delete.json`,
  `artifacts/validation/runtime-smokes/20260801T212106Z/ordinary-search-after-delete.json`
  and
  `artifacts/validation/runtime-smokes/20260801T212117Z/document-purge-job.json`.
- Full backend typecheck cleanup on 2026-08-01:
  fixed the remaining test-only typing issues in
  `tests\unit\test_retrieval_answering.py` and
  `tests\unit\test_extended.py`;
  `uv run mypy src tests` -> exit 0, no issues found in 97 source files;
  `uv run pytest tests\unit\test_retrieval_answering.py tests\unit\test_extended.py -q`
  -> exit 0, 23 passed;
  `uv run ruff check tests\unit\test_retrieval_answering.py tests\unit\test_extended.py`
  -> exit 0;
  `uv run ruff format --check tests\unit\test_retrieval_answering.py tests\unit\test_extended.py`
  -> exit 0, 2 files already formatted.
- Stage 1 Docker runtime validation on 2026-08-01:
  `uv run pytest tests\unit\test_document_deletion_lifecycle.py tests\unit\test_upload_batches.py tests\unit\test_acl_mirroring_metadata.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 22 passed, 2 warnings;
  `docker compose config --quiet` -> exit 0;
  `docker compose up -d --build` -> exit 0;
  `Invoke-RestMethod -Uri http://localhost:8000/ready` -> exit 0 after
  startup, status ok for PostgreSQL and Model Gateway.
  Runtime artifacts were written under
  `artifacts/validation/runtime-smokes/20260801T172131Z/` and remain ignored.
  Authenticated parser batch upload smoke passed in the custom runtime harness:
  metadata-service, Xberg and Docling parser checks passed; two KBs were
  created (`f86cf74e-0120-4a38-91aa-fa935d6e6e2b`,
  `9b7fa803-94a1-4fbf-a4a2-0a5cca5d7947`); batch uploads completed with
  parser routes `docling` and `local_csv_adapter`; artifact
  `stage-1-runtime-smokes.json`.
  Official `uv run python -m wikipediarag.cli verify-document-upload --skip-compose`
  is blocked in the default auth environment: exit 1, report
  `artifacts/validation/document-upload/20260801T171726Z`, safe reason HTTP 401
  on `GET /api/v1/knowledge-bases`. Re-running with `AUTH_DISABLED=true`
  reached writes but failed with exit 1, report
  `artifacts/validation/document-upload/20260801T171823Z`, safe reason
  audit insert rejected non-UUID `auth-disabled:*` session id.
  Document deletion API smoke returned HTTP 202 for document
  `doc:f32a21fccea781d693ec4e1a`, purge job
  `f8b5b4d2-0424-417a-b195-9295549c99bc`; deleted document was absent from
  later retrieval, artifact `document-post-delete-retrieval.json`; deferred
  purge is blocked because the job ended `failed` with safe error code
  `ClientError`, artifact `document-deletion-purge.json`.
  OIDC group-grant runtime smoke passed with Keycloak profile and host-header
  callback flow: KB `269ebb8c-d027-489b-9f07-898bb3287b5b`, grant
  `c1ae7246-dbd4-4ab6-8409-3c84fc8b1ede`, OIDC read before revoke HTTP 200,
  read after revoke HTTP 403, no reindex; artifact
  `oidc-group-grant-access-existing-group-correct-password-v5.json`.
  Multi-KB retrieval passed against two ready KBs with compatible
  `upload_sota_mvp` profile and dedup disabled for evidence coverage:
  `/api/v1/search:debug` -> HTTP 200, `query_run_id`
  `2bb67226-2019-4caa-8a5c-d854d89b85d6`, both KB IDs present in final
  evidence and events; artifact
  `multi-kb-final-evidence-1449761485479121206.json`.
  `uv run python -m wikipediarag.cli verify-cross-tenant-hardening --skip-compose`
  is blocked: exit 1, report
  `artifacts/validation/cross-tenant-hardening/20260801T173951Z/cross-tenant-hardening-report.json`;
  API ready, platform-admin login and two tenant creation passed, then
  `/api/v1/search:debug` returned HTTP 409 `KB_NOT_READY` because profile
  embedding alias `mock_embed_default` did not match index alias `embed_default`;
  captured body artifact
  `artifacts/validation/cross-tenant-hardening/20260801T173951Z/search-debug-409-body.json`.
  API and worker were restored to the default local Docker profile afterwards;
  final `/ready` -> exit 0.
- Explicit transform/subquery retrieval observability validation on 2026-08-01:
  `uv run ruff check src\wikipediarag\retrieval.py src\wikipediarag\extended.py src\wikipediarag\api_app.py tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_api_readiness.py tests\unit\test_multi_kb_retrieval.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\retrieval.py src\wikipediarag\extended.py src\wikipediarag\api_app.py tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_api_readiness.py tests\unit\test_multi_kb_retrieval.py`
  -> exit 0, 7 files already formatted;
  `uv run pytest tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_model_client_observability.py -q`
  -> exit 0, 36 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 201 passed, 4 warnings;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Retrieval observability Docker runtime smoke on 2026-08-01:
  `docker compose up -d --build api model-gateway` -> exit 0;
  `Invoke-RestMethod -Uri http://localhost:8000/ready` -> exit 0,
  status ok for PostgreSQL and Model Gateway;
  authenticated `/api/v1/search:debug` direct runs for simple fact, comparison
  and explicit-negative-title questions -> HTTP 200 with persisted
  `query_run_id`s `d9ba16e6-519c-4c6d-94ac-117ccf7c44a4`,
  `a8f86877-56a9-42b0-82d9-30f5a6f0f3b3` and
  `ed8f3f67-5dfe-41d6-82b7-c113563c2797`;
  authenticated `/api/v1/chat` normal and extended synthetic unanswered queries
  -> HTTP 200 with persisted `query_run_id`s
  `5d4f4536-0cc7-4b06-b805-696260f182a2` and
  `399896ff-9561-4819-ba97-e2e5bc17076e`;
  incompatible Wikipedia+upload Multi-KB debug request -> HTTP 409
  `KB_NOT_READY` with safe reason `active index source is incompatible with
  retrieval profile`;
  compatible upload+upload Multi-KB debug request with `upload_mock` profile
  -> HTTP 200, `query_run_id`
  `103fb6de-18f1-48bf-b275-14497e1c7aa3`, both KB IDs present in run scope and
  candidate events;
  after fixing candidate-stage snapshots, repeated compatible upload+upload
  Multi-KB debug request -> HTTP 200, `query_run_id`
  `0c5e1b11-5be8-4695-91aa-4667b34efb1a`, PostgreSQL `rrf` payload contains
  BM25/dense/fusion ranks only and `rerank` appears only in the following
  `rerank` payload;
  protected feedback/evaluation append endpoints -> HTTP 200;
  unauthenticated query-run retrieval read -> HTTP 401;
  controlled Model Gateway outage chat -> SSE `run.failed`, persisted
  `query_run_id` `373cac08-e266-4473-a537-03c311ee3842` with
  `query_runs.status=failed`, `error_code=ModelGatewayError` and an
  `answer_generation` failure event carrying safe model metadata
  `safe_error_code=provider_network_error`, `attempts=3`, `retries=2` and no
  prompt/provider payload fields.
  PostgreSQL row counts after the smoke: `query_runs=626`,
  `retrieval_events=102914`.
- Candidate-stage snapshot regression check after runtime smoke:
  `uv run pytest tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py tests\unit\test_multi_kb_retrieval.py -q`
  -> exit 0, 25 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\api_app.py src\wikipediarag\retrieval.py tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\api_app.py src\wikipediarag\retrieval.py tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py`
  -> exit 0.
- Retrieval observability V1 backend/UI validation:
  `uv run pytest tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_model_client_observability.py -q`
  -> exit 0, 29 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 197 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed.
- Retrieval observability V1 static checks:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0;
  `uv run mypy src tests` -> exit 0.
- Retrieval observability V1 UI checks:
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Unit and focused quality checks after public batch upload and Multi-KB retrieval:
  `uv run pytest tests\unit\test_upload_batches.py tests\unit\test_multi_kb_retrieval.py -q`
  -> exit 0, 5 passed, 2 warnings.
- Full deterministic backend validation after the same increment:
  `uv run pytest tests\unit -q` -> exit 0, 191 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed.
- Static checks after the same increment:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0;
  `uv run mypy src tests` -> exit 0.
- UI validation after the same increment:
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Compose validation after parser sandboxing/autoscaling:
  `docker compose config --quiet` -> exit 0.
- Latest provider-backed reviewed Wikipedia gate:
  `uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000`
  -> exit 0, passed=true, blocking_failures=0.
- Latest gate report:
  `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260730T195822Z-reviewed-wikipedia-smoke-v1-release-gate-5b04e45f/report.json`.
- Document corpus verification retained from the document ingestion implementation:
  standard fixture set -> exit 0, report `artifacts/validation/document-corpus/20260729T205604Z`;
  full fixture set -> exit 0, report `artifacts/validation/document-corpus/20260729T205923Z`;
  smoke external set -> exit 0, report `artifacts/validation/document-corpus/20260729T210847Z`.
- Document upload verification retained from the document ingestion implementation:
  `uv run python -m wikipediarag.cli verify-document-upload --skip-compose`
  -> exit 0, report `artifacts/validation/document-upload/20260729T210047Z`.

## Active blockers and risks

- Multi-KB direct retrieval is implemented; Multi-KB Extended Search remains future work.
- Offline Deep Research matrix results are not yet a provider/runtime policy
  benchmark. The current 45% productive-target default should not be changed
  solely from the synthetic packer matrix.
- API `/ready` currently checks PostgreSQL and Model Gateway readiness; Redis/Valkey, MinIO and OpenSearch are Compose dependencies but are not confirmed readiness checks.
- Redis/Valkey is configured and present in Compose, but no Redis client usage was found in `src/`; current ingestion job claiming uses PostgreSQL `FOR UPDATE SKIP LOCKED`.
- Malware scanning, external ACL connector import policy, restore drills, production TLS/secrets, observability retention and resource sizing remain production hardening work.
- OpenTelemetry collector remains debug-exporter only; optional OpenSearch observability data-stream projection is not implemented.
- OpenRouter-backed gates depend on provider quota, credits, latency and model behavior.

## Next approved task

If Deep Research tuning continues, implement a runtime context-policy override
for smoke/experiment runs and compare the current 45% default against the
offline winner under mock and optional local Qwen/qwen3.6-27b validation before
changing production defaults.

## Related artifacts

- Architecture overview: [architecture.md](architecture.md).
- Focused architecture docs: [architecture/](architecture/).
- Agent instructions: [../AGENTS.md](../AGENTS.md).
- Historical implementation plans: [exec-plans/](exec-plans/).
- Historical status archive: [history/STATUS-archive.md](history/STATUS-archive.md).
- Latest reviewed gate report:
  `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260730T195822Z-reviewed-wikipedia-smoke-v1-release-gate-5b04e45f/report.json`.
