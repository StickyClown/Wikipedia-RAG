# ExecPlan 24 - Authentication, Tenancy And Knowledge-Base Access

Status: Slice 1-6 implemented as a production-shaped MVP on 2026-07-30. Live Keycloak container and host-side OIDC code-flow smoke pass.

## Goal

Add production-shaped authentication and authorization before user-owned data is introduced, while preserving the existing default tenant, default KB and current Wikipedia index.

## Implemented Slice 1

- Added auth configuration fields for auth modes, bootstrap admin inputs, bounded sessions and configurable OIDC claim paths.
- Locked approved auth dependencies: `argon2-cffi` and `PyJWT[crypto]`.
- Added forward-only `ensure_schema` expansion for auth identities, sessions, OIDC flows, groups, group memberships, KB grants, audit events and query-run KB scope.
- Relaxed `users.email` to a nullable profile attribute and introduced `auth_identities.issuer + subject` as the durable identity key.
- Backfilled old tenant membership role names to `TENANT_ADMIN`/`MEMBER`.
- Seeded the default local user with an `OWNER` grant on the default Wikipedia KB.
- Added typed `ActorContext`, platform/tenant/KB role enums, KB capability policy and authorization errors.
- Added repository helpers for effective KB role resolution and audit event insertion.
- Added safe API error-envelope handlers with validation input redaction.

## Implemented Slice 2 Foundation

- Added `auth_service` primitives for Argon2id password hashing/verification, opaque random session/CSRF values and SHA-256 token hashes.
- Added startup bootstrap admin creation for `AUTH_MODE=local|hybrid` only when no `PLATFORM_ADMIN` exists and `BOOTSTRAP_ADMIN_PASSWORD_FILE` is mounted.
- Bootstrap admin creation sets `password_change_required=true`, never logs the password and never resets an existing password hash.
- Added local login endpoint disabled in strict `AUTH_MODE=oidc`.
- Added opaque HttpOnly app session cookie with configurable `Secure` and `SameSite` attributes.
- Added `/api/v1/auth/session` session inspection with fresh CSRF token issuance.
- Added password-change, logout/revocation and active-tenant selection endpoints requiring `X-CSRF-Token`; tenant selection rotates the session token.
- Added unit tests for Argon2id-only hashes, token hash shape, secret-file behavior and auth-mode gating.
- Validated bootstrap/login/session/logout and no-reset behavior against compose Postgres using a temporary bootstrap password file.

## Implemented Slice 3

- Added OIDC start/callback endpoints.
- Implemented discovery, Authorization Code + PKCE S256, confidential client token exchange and JWKS ID-token validation.
- Validated issuer, audience, nonce and signature deterministically in fake-provider tests.
- Matched identity strictly by `issuer + sub`; email/username stay profile attributes.
- Stored Keycloak access/refresh tokens only as an encrypted server-side session payload.

## Implemented Slice 4

- Added local/OIDC group CRUD and local membership editing.
- Added OIDC group sync helper that replaces only `membership_type='OIDC'` rows and never removes local memberships.
- Added admin users/tenants APIs.
- Added KB CRUD and KB grant CRUD.
- User-created KBs grant the creator `OWNER`.

## Implemented Slice 5

- Removed route-level `settings.default_tenant_id/default_user_id` usage from tenant-scoped FastAPI routes.
- Enforced `ActorContext.active_tenant_id` and KB roles across KBs, imports, upload sessions, upload completion, documents, reprocess, jobs, chat, search debug and query-run retrieval.
- Rejected multi-KB retrieval with `MULTI_KB_UNSUPPORTED`.
- Required CSRF for unsafe cookie-authenticated methods.

## Implemented Slice 6

- Added `compose.keycloak.yaml` with pinned `quay.io/keycloak/keycloak:26.7.0`.
- Added deterministic Keycloak realm import and smoke-only mounted secret fixtures.
- Added UI local/OIDC login, session display, logout, KB list/create/select and cookie/CSRF-aware fetches.
- Updated first-read docs and this ExecPlan document.

## Validation

```text
uv run pytest tests\unit\test_auth_policy.py tests\unit\test_auth_schema.py tests\unit\test_api_readiness.py
-> exit 0, 13 passed, 2 warnings

uv add argon2-cffi "PyJWT[crypto]"
-> exit 0

uv run ruff check .
-> exit 0, All checks passed!

uv run mypy src tests
-> exit 0, Success: no issues found in 77 source files

uv run pytest tests/unit tests/integration
-> exit 0, 145 passed, 4 warnings

$env:DATABASE_URL='postgresql+asyncpg://rag:change-me-local-only@localhost:5432/rag'; uv run python -m wikipediarag.migrate
-> exit 0, database schema is ready

ASGI local-auth smoke against compose Postgres with temporary BOOTSTRAP_ADMIN_PASSWORD_FILE
-> exit 0, bootstrap login 200, /auth/session CSRF issued, logout 200, changed bootstrap password file did not reset existing hash

uv run pytest tests/unit tests/integration
-> exit 0, 152 passed, 4 warnings

cd services/ui; pnpm build
-> exit 0

docker compose -f compose.yaml -f compose.keycloak.yaml --profile keycloak-smoke config
-> exit 0

docker compose -f compose.yaml -f compose.keycloak.yaml --profile keycloak-smoke up -d keycloak
-> exit 0 after the image was available locally

Host-side Keycloak OIDC Authorization Code + PKCE smoke against compose Postgres
-> exit 0, code callback completed, app session created, provider tokens stored encrypted server-side
```
