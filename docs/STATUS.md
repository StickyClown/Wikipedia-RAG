# Project Status

Last updated: 2026-07-30

## Current Phase

ExecPlan 24 Slice 1-6 are implemented as a production-shaped MVP. The platform now has auth identities, session/group/grant/audit schema, default-tenant backfill, typed `ActorContext`, KB role policy, safe API error envelopes, bootstrap admin creation from a mounted password file, Argon2id local password verification, opaque server sessions, CSRF issuance from `/api/v1/auth/session`, logout/revocation, tenant selection, OIDC Authorization Code + PKCE S256 foundation, JWKS ID-token validation, server-side encrypted OIDC token storage, local/OIDC groups, admin users/tenants, KB grants, route-level ActorContext enforcement and UI login/session/KB controls. The pinned Keycloak container starts and the host-side Keycloak OIDC code-flow smoke passes.

ExecPlan 25.1+25.2 remains implemented for the local default-tenant RAG platform with Russian Wikipedia ingestion plus async document uploads, universal metadata, isolated parser services and corpus verification.

ExecPlan 26 is completed and closed. It adds deterministic Legal RAG Bench-style root-cause diagnostics to answer/retrieval eval artifacts and summaries. It does not add LLM judging and does not change `/api/v1/chat`, retrieval runtime, Model Gateway, ingestion or database schema.

Latest reviewed Wikipedia provider gate remains:

- state: `completed`
- passed: `true`
- blocking_failures: `0`
- report: `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260729T162834Z-reviewed-wikipedia-smoke-v1-release-gate-a8a2f1ea/report.json`

## Implemented Capabilities

- ZIM/libzim + Kiwix Wikipedia import with deterministic chunks, redirects as provenance, checkpoints and published OpenSearch index versions.
- XML multistream import remains as a regression/local fallback.
- Model Gateway liveness/readiness split, OpenRouter alias smoke checks, mock provider profiles and no direct provider calls from business code.
- Hybrid retrieval with BM25, dense vectors, RRF, rerank, dedup/page quota, parent expansion, answerability and citation validation.
- Bounded Extended Search MVP for controlled multi-query evidence expansion.
- Dated release-gate reports with configuration snapshots, one root run contract, execution path, child retrieval/tool contracts, safe failure fields and per-question step events.
- Eval/reporting diagnostics classify answer and retrieval task results as `passed`, `retrieval_error`, `hallucination_or_unsupported`, `reasoning_error`, `hard_negative_attribution`, `unanswerable_false_positive`, `execution_error` or `not_evaluated`; JSONL task results, short eval payloads, task diagnostics, summaries and release-gate blocker details expose the diagnosis where available.
- Async upload sessions: presigned MinIO upload first, completion second, background ingestion job after durable object visibility.
- Forward-only `ensure_schema` expansion for knowledge sources, upload batches/sessions, document versions/artifacts, ingestion job items and chunk locator/publication metadata.
- Universal document metadata on versions: upload/system timestamps, source/document dates, date candidates/source/confidence, detected language/confidence/alternatives, MIME/signature facts, parser route/version/options, hashes, warnings and safe public metadata.
- Isolated parser services: Xberg default parser, Docling Serve CPU fallback/high-quality parser and metadata-service for fast local language/date extraction.
- App-owned `NormalizedDocument` contract with stable blocks, tables, locators, parser reports, warnings and deterministic normalized/chunk hashes.
- Worker item claiming uses bounded `FOR UPDATE SKIP LOCKED`; failed/cancelled/parser-error jobs do not publish searchable chunks.
- UI upload panel creates sessions, uploads to presigned URLs, completes sessions, polls async progress and shows parser route, metadata and terminal errors.
- Document corpus verification uses generated fixtures plus optional pinned URL/SHA/license manifest samples.
- ExecPlan 24 Slice 1 auth foundation: forward-only `ensure_schema` expansion for `auth_identities`, `auth_sessions`, `auth_oidc_flows`, `groups`, `group_memberships`, `knowledge_base_grants` and `audit_events`; `users.email` is nullable profile metadata; identity matching is represented by `issuer + subject`.
- ExecPlan 24 Slice 2 local-auth foundation: startup bootstrap admin from `BOOTSTRAP_ADMIN_PASSWORD_FILE` only when no `PLATFORM_ADMIN` exists, Argon2id password hashes, local login in `AUTH_MODE=local|hybrid`, opaque HttpOnly session cookies, SHA-256 session/CSRF hashes in `auth_sessions`, `/auth/session` CSRF issuance, logout revocation and active-tenant selection with session rotation.
- ExecPlan 24 Slice 3 OIDC foundation: `/api/v1/auth/oidc/start` and callback implement Authorization Code Flow with PKCE S256, OIDC discovery, JWKS signature validation, exact issuer/audience/nonce validation, `issuer + sub` identity matching, no email/username auto-merge and encrypted server-side access/refresh token storage.
- ExecPlan 24 Slice 4 groups and KB sharing: local/OIDC group CRUD/membership separation, full OIDC group paths, OIDC membership sync that touches only `membership_type='OIDC'`, KB owner-on-create and direct/group KB grants.
- ExecPlan 24 Slice 5 route enforcement: FastAPI tenant/user scope is resolved from immutable `ActorContext`; route-level default tenant/default user usage was removed from tenant-scoped APIs; chat/query requires `VIEWER`, debug/query-run retrieval/upload/reprocess/import/job control require `EDITOR`, grants/source-style management require `MANAGER`, delete/owner grants require `OWNER` or platform admin.
- ExecPlan 24 Slice 6 smoke/UI/docs: pinned Keycloak compose profile and deterministic realm import were added; UI now has local/OIDC login, session display, logout, KB list/create/select and cookie/CSRF-aware fetches.
- Authorization policy foundation: typed platform, tenant and KB role enums; immutable `ActorContext`; highest-role KB policy; tenant-admin/platform-admin helpers; repository helpers for effective KB role resolution and audit insertion.
- Default tenant backfill maps legacy tenant membership roles to `TENANT_ADMIN`/`MEMBER` and grants the seeded local user `OWNER` on the default Wikipedia KB without deleting or rebuilding the current Wikipedia index.
- API errors now use the safe error envelope for HTTP and validation failures, with validation input redacted.
- Approved auth dependencies are locked and in use: `argon2-cffi` for Argon2id password hashes and `PyJWT[crypto]` for OIDC/JWKS validation.

## Validation Evidence

Latest local validation after completing ExecPlan 26 Legal RAG Bench-style eval diagnostics:

```text
uv run ruff check src\wikipediarag\eval\diagnostics.py src\wikipediarag\eval\schemas.py src\wikipediarag\eval\runner.py src\wikipediarag\eval\retrieval_runner.py src\wikipediarag\eval\commands.py src\wikipediarag\eval\review.py tests\unit\test_eval_diagnostics.py tests\integration\test_eval_runner.py tests\unit\test_eval_retrieval_runner.py
-> exit 0, All checks passed!

uv run pytest tests\unit\test_eval_diagnostics.py -q
-> exit 0, 9 passed

uv run mypy src\wikipediarag\eval\diagnostics.py src\wikipediarag\eval\schemas.py src\wikipediarag\eval\runner.py src\wikipediarag\eval\retrieval_runner.py src\wikipediarag\eval\commands.py src\wikipediarag\eval\review.py tests\unit\test_eval_diagnostics.py
-> exit 0, Success: no issues found in 7 source files

uv run pytest tests\unit\test_eval_diagnostics.py tests\unit\test_eval_metrics.py tests\integration\test_eval_runner.py tests\unit\test_eval_review.py -q
-> first run exit 1: timing-sensitive test_eval_runner.py::test_runner_batch_size_is_bounded_in_flight_backfill_scheduler failed; immediate targeted rerun passed
-> rerun exit 0, 31 passed

uv run pytest tests\unit\test_eval_retrieval_runner.py -q
-> exit 0, 6 passed

git diff --check
-> exit 0, CRLF normalization warnings only

try { Invoke-RestMethod -Uri http://localhost:8000/ready -TimeoutSec 5 | ConvertTo-Json -Depth 8 } catch { "READY_CHECK_FAILED: $($_.Exception.Message)" }
-> initial exit 0, status=degraded, postgres=ok, model_gateway=failed

uv run python -m wikipediarag.cli smoke-models --provider openrouter
-> initial exit 1, aliases unhealthy while gateway readiness was degraded

docker compose restart model-gateway
-> exit 0

Start-Sleep -Seconds 20; Invoke-RestMethod http://localhost:8081/ready; Invoke-RestMethod http://localhost:8000/ready
-> exit 0, gateway status=ok, API status=ok

uv run python -m wikipediarag.cli smoke-models --provider openrouter
-> exit 0, embedding_dimensions=1024, typed_json ok, rerank ordered

uv run python -m wikipediarag.cli eval-reviewed-short --suite reviewed-wikipedia-smoke-v1 --split dev --config-id sota_mvp_normal --task-id trusted-wiki-000018 --task-id trusted-wiki-000217 --task-id trusted-wiki-000251 --task-id trusted-wiki-000295 --batch-size 2 --retrieval-batch-size 2 --api http://localhost:8000
-> first live attempt exit 0 but every task failed with auth errors because API was in normal AUTH_DISABLED=false mode and the eval HTTP client has no local-auth session

$env:AUTH_DISABLED='true'; docker compose up -d --no-deps --force-recreate api; Remove-Item Env:AUTH_DISABLED
-> exit 0, API recreated for local/demo eval bypass

Invoke-RestMethod http://localhost:8000/ready; Invoke-RestMethod http://localhost:8000/api/v1/auth/session
-> exit 0, API status=ok, auth-disabled session authenticated=true

uv run python -m wikipediarag.cli eval-reviewed-short --suite reviewed-wikipedia-smoke-v1 --split dev --config-id sota_mvp_normal --task-id trusted-wiki-000018 --task-id trusted-wiki-000217 --task-id trusted-wiki-000251 --task-id trusted-wiki-000295 --batch-size 2 --retrieval-batch-size 2 --api http://localhost:8000
-> exit 0, all 4 answer tasks and all 4 retrieval tasks completed

uv run python -m wikipediarag.cli eval-task-diagnostics --suite reviewed-wikipedia-smoke-v1 --split dev --config-id sota_mvp_normal --task-id trusted-wiki-000018 --task-id trusted-wiki-000217 --task-id trusted-wiki-000251 --task-id trusted-wiki-000295 --json
-> exit 0, missing_task_ids=[]
-> trusted-wiki-000018 answer=passed retrieval=passed
-> trusted-wiki-000217 answer=passed retrieval=passed
-> trusted-wiki-000251 answer=passed retrieval=retrieval_error
-> trusted-wiki-000295 answer=reasoning_error retrieval=passed

$env:AUTH_DISABLED='false'; docker compose up -d --no-deps --force-recreate api; Remove-Item Env:AUTH_DISABLED
-> exit 0, API restored to normal auth mode

Invoke-RestMethod http://localhost:8000/ready; Invoke-RestMethod http://localhost:8000/api/v1/auth/session
-> exit 0, API status=ok, anonymous session authenticated=false
```

Latest local validation after completing ExecPlan 24:

```text
$env:DATABASE_URL='postgresql+asyncpg://rag:change-me-local-only@localhost:5432/rag'; uv run python -m wikipediarag.migrate
-> exit 0, database schema is ready

uv run ruff check .
-> exit 0, All checks passed!

uv run ruff format --check .
-> exit 0, 83 files already formatted

uv run mypy src tests
-> exit 0, Success: no issues found in 81 source files

uv run pytest tests/unit tests/integration
-> exit 0, 152 passed, 4 warnings

uv run pytest tests\unit\test_oidc_service.py tests\unit\test_auth_service.py
-> exit 0, 13 passed

$env:DATABASE_URL='postgresql+asyncpg://rag:change-me-local-only@localhost:5432/rag'; uv run python -m wikipediarag.migrate
-> exit 0, database schema is ready

uv run ruff check .
-> exit 0, All checks passed!

uv run ruff format --check .
-> exit 0, 83 files already formatted

uv run mypy src tests
-> exit 0, Success: no issues found in 81 source files

uv run pytest tests/unit tests/integration
-> exit 0, 152 passed, 4 warnings in 16.95s

cd services/ui; pnpm typecheck
-> exit 0

cd services/ui; pnpm lint
-> exit 0

cd services/ui; pnpm build
-> exit 0

docker compose -f compose.yaml -f compose.keycloak.yaml --profile keycloak-smoke config
-> exit 0, merged compose config rendered successfully; raw output not recorded because host .env values are expanded

docker compose -f compose.yaml -f compose.keycloak.yaml --profile keycloak-smoke up -d keycloak
-> exit 0, after the image was available locally, `wikipediarag-keycloak-1` started

Invoke-RestMethod http://localhost:8084/realms/wikipediarag/.well-known/openid-configuration
-> exit 0, issuer `http://localhost:8084/realms/wikipediarag`

Host-side Keycloak OIDC Authorization Code + PKCE smoke against compose Postgres
-> exit 0, code callback completed, platform_role USER, opaque app session created, access/refresh tokens stored encrypted server-side, no access token in app session token

git diff --check
-> exit 0, no whitespace errors; Git reported expected LF-to-CRLF working-copy warnings on Windows

rg -n --hidden --glob '!.env' --glob '!infra/keycloak/smoke-secrets/**' --glob '!infra/keycloak/wikipediarag-realm.json' --glob '!docs/STATUS.md' --glob '!artifacts/**' --glob '!.git/**' --glob '!.venv/**' --glob '!services/ui/node_modules/**' --glob '!services/ui/dist/**' --glob '!zim/**' --glob '!zip/**' --glob '!*.pyc' 'OPENROUTER_API_KEY=sk-|sk-or-v1|BEGIN .*PRIVATE KEY|change-this-admin-password|new-password-must-not-apply' .
-> exit 1, no concrete OpenRouter keys, private keys or local-auth smoke password values found outside explicit smoke fixtures
```

Latest parser/corpus verification reports retained from the ExecPlan 25.1+25.2 implementation run:

```text
make verify-document-corpus
-> exit 1, GNU Make is not installed in this Windows host PATH

uv run python -m wikipediarag.cli verify-document-corpus --fixture-set standard
-> exit 0, passed=true, total=18, report_dir=artifacts\validation\document-corpus\20260729T205604Z

uv run python -m wikipediarag.cli verify-document-corpus --fixture-set full --skip-compose
-> exit 0, passed=true, total=21, report_dir=artifacts\validation\document-corpus\20260729T205923Z

uv run python -m wikipediarag.cli verify-document-corpus --fixture-set smoke --include-external --skip-compose
-> exit 0, passed=true, total=4, report_dir=artifacts\validation\document-corpus\20260729T210847Z

uv run python -m wikipediarag.cli verify-document-upload --skip-compose
-> exit 0, passed=true, report_dir=artifacts\validation\document-upload\20260729T210047Z
```

Parser image tags/digests recorded during implementation:

```text
ghcr.io/xberg-io/xberg:1.0.3
-> ghcr.io/xberg-io/xberg@sha256:69435354060fbf8495b102494536505a9c45142cd5392d5f79b98906f70fd69c

quay.io/docling-project/docling-serve-cpu:v1.28.0
-> quay.io/docling-project/docling-serve-cpu@sha256:cc207e1eb768878456ed98042c5d84fae56af3729a9c03d3e5c8fef393902956
```

## Local Data State

- Real ZIM pages imported: `10,000` canonical non-redirect pages.
- Real ZIM chunks indexed: `14,281`.
- OpenSearch index: `wiki-chunks-387df2fb225f794d`.
- Redirect provenance is persisted for the local ZIM snapshot.
- Document corpus reports and downloaded external bytes live under ignored `artifacts/`.

## Gitignore Audit

Tracked ignore policy covers current generated and sensitive local state:

- local secrets/config: `.env`, `.env.*`, `openrouter_key.txt`, `secrets/`;
- Python/tool caches: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, coverage outputs;
- UI/build outputs: `node_modules/`, `dist/`, `build/`;
- runtime/generated artifacts: `artifacts/`, `data/`, `uploads/`, `zip/`;
- large local model/data files: `models/`, `*.zim`, `*.gguf`, XML dump patterns;
- keys/certs: `*.pem`, `*.key`.

No additional `.gitignore` change is required for the current commit. `config/document_corpus_manifest.json` is intentionally tracked because it contains only URLs, checksums, licenses and expected assertions, not downloaded corpus bytes.

## Known Limitations

- ExecPlan 24 is implemented as an MVP, and the live Keycloak container plus host-side code-flow smoke pass. Full browser UI OIDC through the Dockerized API was not run in this turn.
- Retrieval remains single-KB only; requests with more than one KB fail safely with `MULTI_KB_UNSUPPORTED`.
- Eval HTTP client does not yet create a local-auth session; live local provider-backed evals currently use the documented `AUTH_DISABLED=true` bypass and must restore normal auth mode afterwards.
- Data-path enforcement is route-level plus original retrieval query scoping for existing API paths; a deeper worker/cache/object-key audit should remain part of hardening before external deployment.
- Document ingestion is local/default-tenant only and is not production-hardened for malware scanning, retention/deletion, ACL mirroring, parser autoscaling or external deployment.
- Public multi-file batch creation is not exposed yet; the DB/job framework supports independent job items and bounded worker claiming.
- Language/date metadata is deterministic and local but heuristic; binary/scanned files get final metadata only after parser/OCR text exists.
- OpenRouter-backed gates depend on provider quota, credits, latency and model behavior.
- Warm retrieval p95 has exceeded target in previous real evals and needs profiling.
- Large/legal corpus expansion should stay manifest-driven and outside ordinary CI until explicitly approved.

## Next Improvement Plan

Recommended next stage: do a focused hardening review of worker/cache/object-storage deletion semantics before external deployment.

- Add a dedicated cross-tenant runtime smoke that exercises chat, debug, uploads, documents, jobs, query-run retrieval and object keys against two real tenants.
- Verify browser UI OIDC through the Dockerized API after deciding the internal/public Keycloak URL strategy for containerized local development.
