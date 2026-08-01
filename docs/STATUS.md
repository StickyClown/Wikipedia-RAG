# Project Status

Last updated: 2026-08-01

## Current milestone

Stage 1 Docker runtime validation has been run and recorded. The current focus
is fixing the blockers found during those smokes before declaring the runtime
baseline stable.

Completion criterion: document purge completes, the official document-upload
and cross-tenant smoke commands run under the default Docker auth/profile
configuration, and the smoke set can be repeated with no blocker entries.

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
- Query-run observability V1: chat/debug queries persist protected query runs and retrieval events with stable stage names, query transforms, candidate rank movement, decision reasons, answerability reason codes, Model Gateway safe metadata, feedback/evaluation event endpoints and an updated Retrieval Debugger. PostgreSQL remains the source of truth; OpenTelemetry export is optional/no-op when packages are absent.
- Retrieval observability now carries explicit per-run `transform_id`/`subquery_id` query context through direct, extended and Multi-KB stage events, candidate debug payloads, extended harness search yield metrics and answer context source summaries.
- Eval and validation tooling: local-auth eval client, release-gate provider preflight, warm retrieval profiling, MIRACL-RU adapter, document corpus verification and cross-tenant hardening smoke command.

## In progress

- Stage 1 runtime blockers: deferred document purge failure, auth/profile
  mismatch in the official smoke commands.
- Browser UI OIDC through Dockerized API after the public/internal Keycloak URL strategy is decided.
- External deployment hardening remains planned, not supported production operation.

## Latest validation

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

- Deferred document purge failed in the 2026-08-01 Docker smoke: DELETE/soft
  removal worked, but immediate-retention purge job ended `failed` with safe
  error code `ClientError`.
- Official `verify-document-upload --skip-compose` is not currently usable as a
  default-auth smoke: it gets HTTP 401 without auth handling; the
  `AUTH_DISABLED=true` workaround hits the audited-write UUID session-id bug.
- Official `verify-cross-tenant-hardening --skip-compose` is blocked by
  retrieval profile/index alias mismatch: `upload_mock` expects
  `mock_embed_default`, while the uploaded index exposes `embed_default`.
- Runtime browser/MinIO batch upload smoke has not yet been recorded for the implemented public multi-file UI.
- Multi-KB direct retrieval is implemented; Multi-KB Extended Search remains future work.
- API `/ready` currently checks PostgreSQL and Model Gateway readiness; Redis/Valkey, MinIO and OpenSearch are Compose dependencies but are not confirmed readiness checks.
- Redis/Valkey is configured and present in Compose, but no Redis client usage was found in `src/`; current ingestion job claiming uses PostgreSQL `FOR UPDATE SKIP LOCKED`.
- Malware scanning, external ACL connector import policy, restore drills, production TLS/secrets, observability retention and resource sizing remain production hardening work.
- OpenTelemetry collector remains debug-exporter only; optional OpenSearch observability data-stream projection is not implemented.
- OpenRouter-backed gates depend on provider quota, credits, latency and model behavior.

## Next approved task

Fix the Stage 1 runtime blockers without broad refactoring: deferred purge
`ClientError`, official document-upload smoke authentication, and official
cross-tenant smoke retrieval profile compatibility.

## Related artifacts

- Architecture overview: [architecture.md](architecture.md).
- Focused architecture docs: [architecture/](architecture/).
- Agent instructions: [../AGENTS.md](../AGENTS.md).
- Historical implementation plans: [exec-plans/](exec-plans/).
- Historical status archive: [history/STATUS-archive.md](history/STATUS-archive.md).
- Latest reviewed gate report:
  `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260730T195822Z-reviewed-wikipedia-smoke-v1-release-gate-5b04e45f/report.json`.
