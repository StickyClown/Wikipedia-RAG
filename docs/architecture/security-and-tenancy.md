# Security And Tenancy

This document separates implemented guarantees from local-MVP risks and future
hardening. It is based on the current backend implementation, not older status
text.

## Trust Boundaries

- Browser is untrusted. It may hold session cookies, CSRF tokens, selected KB ids and files selected by the user.
- API is the authorization boundary. It creates `ActorContext`, injects tenant/KB scope and validates roles.
- Worker is trusted application code but must not accept tenant authority from parser services or client metadata.
- Parser services are isolated helpers. They receive bytes/temp files only.
- Model Gateway is the only model-provider boundary for business code.
- PostgreSQL and MinIO are authoritative storage; OpenSearch is derived and must be tenant-filtered.

## ActorContext And Tenant Source

`ActorContext` contains user id, platform role, active tenant id, tenant role,
session id, authentication method, request id and trace id. It is created
server-side from:

- an app session cookie;
- `AUTH_DISABLED=true` local/demo bypass;
- `AUTH_MODE=test` only when `APP_ENV=test`.

Tenant-scoped routes require `active_tenant_id`. Client-supplied tenant filters,
object prefixes or user/group authority are not trusted.

Active tenant is stored in `auth_sessions.active_tenant_id`. The session tenant
selection endpoint requires CSRF and rotates the session token.

## Roles And KB Access

Implemented roles:

- Platform: `USER`, `PLATFORM_ADMIN`.
- Tenant: `MEMBER`, `TENANT_ADMIN`.
- Knowledge base: `VIEWER`, `EDITOR`, `MANAGER`, `OWNER`.

Effective KB role is the highest role from platform admin, tenant admin, direct
user grants, local group grants and OIDC group grants. Platform admin maps to
KB owner. Tenant admin maps to KB manager. Explicit deny rules and
document-level ACLs are not implemented.

## Permission Matrix

| Operation | Minimum role | Enforcement location |
| --- | --- | --- |
| Inspect session | Authenticated actor or `AUTH_DISABLED` actor | `GET /api/v1/auth/session` |
| Change password | Authenticated local session plus CSRF | `POST /api/v1/auth/local/password` |
| Logout | Authenticated session plus CSRF | `POST /api/v1/auth/logout` |
| Select active tenant | Tenant membership or platform admin plus CSRF | `POST /api/v1/auth/session/tenant` |
| Admin users and tenants | `PLATFORM_ADMIN` | `/api/v1/admin/users`, `/api/v1/admin/tenants` |
| Group CRUD | `TENANT_ADMIN` or `PLATFORM_ADMIN` | `/api/v1/groups` |
| List KBs | Authenticated actor with active tenant | `GET /api/v1/knowledge-bases` |
| Read KB metadata | KB `VIEWER` | `GET /api/v1/knowledge-bases/{kb_id}` |
| Create KB | Authenticated actor with active tenant; creator becomes owner | `POST /api/v1/knowledge-bases` |
| Update KB | KB `MANAGER` | `PATCH /api/v1/knowledge-bases/{kb_id}` |
| Delete KB | KB `OWNER` | `DELETE /api/v1/knowledge-bases/{kb_id}` |
| View KB grants | KB `MANAGER` | `GET /api/v1/knowledge-bases/{kb_id}/grants` |
| Grant viewer/editor/manager | KB `MANAGER` | grant create/update endpoints |
| Grant owner | KB `OWNER` | grant create/update endpoints |
| Delete KB grant | KB `MANAGER` | grant delete endpoint |
| Wikipedia XML/ZIM import | KB `EDITOR` | import endpoints |
| Create upload session or batch | KB `EDITOR` | upload endpoints |
| Complete upload session | KB `EDITOR` | upload complete endpoint |
| Read upload batch status | KB `EDITOR` | batch status endpoint |
| Read ingestion job | Active tenant plus KB role through job load/control paths | job endpoints |
| Cancel or resume ingestion job | KB `EDITOR` | job control endpoints |
| Read document/version metadata | KB `VIEWER` | document endpoints |
| Reprocess document | KB `EDITOR` | reprocess endpoint |
| Delete document | KB `OWNER` | document delete endpoint |
| Chat | KB `VIEWER` on every requested KB | `POST /api/v1/chat` |
| Debug search | KB `EDITOR` on every requested KB | `POST /api/v1/search:debug` |
| Query-run retrieval events | KB `EDITOR` for query-run KB scope | query-run retrieval endpoint |

## Session Cookie And CSRF

The browser receives an opaque HttpOnly app session cookie. PostgreSQL stores
only SHA-256 hashes of session and CSRF tokens. `GET /api/v1/auth/session`
rotates and returns a CSRF token. Unsafe cookie-authenticated requests require
`X-CSRF-Token`.

`SESSION_COOKIE_SECURE=false` is used for local HTTP Compose. External HTTPS
deployment must set secure cookies.

## Local Auth

Local passwords are verified with Argon2id. The bootstrap admin username
defaults to `admin`; local default password is development-only and may be
overridden by mounted secret file or environment. Passwords must not be logged.

## OIDC

OIDC login uses Authorization Code Flow with PKCE S256. The callback validates
discovery/JWKS, issuer, audience, nonce and ID-token signature. Identity
matching is `issuer + sub`; email and username are profile attributes and are
not used for automatic account merge. Provider access/refresh tokens are
encrypted server-side in the app session row.

OIDC group sync touches only `membership_type='OIDC'` rows and does not remove
local memberships.

## Retrieval Tenancy

Chat and debug retrieval resolve a server-owned KB scope list. The API requires
the appropriate role on every requested KB and checks each KB for an active
compatible index before retrieval starts. BM25, dense search, delete and debug
paths apply `tenant_id` and `knowledge_base_id` filters server-side.

Multi-KB direct retrieval is implemented for chat/debug. Extended Search remains
single-KB in the current slice.

## Parser And Upload Boundaries

Upload object keys are generated by the API and written to PostgreSQL. The
browser receives presigned URLs and required headers, not MinIO credentials.
Batch status and public document metadata must not expose object keys.

Parser services receive bytes/temp files over HTTP only. They must not receive
MinIO credentials, raw object keys, arbitrary URLs, tenant authority, prompts or
provider payloads.

## Audit Events

Audit events are inserted for auth, admin, group, KB and document lifecycle
operations. Audit payloads must remain safe and scoped.

## Log Redaction And Secret Handling

Normal logs and public validation reports must not contain:

- `.env` secrets or mounted secret values;
- provider access/refresh tokens;
- raw provider payloads or prompts;
- raw document text outside explicit answer/citation payloads;
- storage object keys, `s3://` URIs or parser stderr;
- cross-tenant probe payloads beyond safe IDs/status/error codes.

## Cross-Tenant Validation

The `verify-cross-tenant-hardening` command creates two tenants, uploads into
one tenant and probes access from another active tenant across KB, upload,
document, job, query-run retrieval, debug search and chat paths. It also checks
safe probe payloads for object-key/token/document-text leakage.

## AUTH_DISABLED

`AUTH_DISABLED=true` is a local/demo bypass. It returns a server-owned
platform-admin actor in the default tenant without a normal cookie session and
skips CSRF checks. It is unsafe for external deployment and must not be used as
production auth.

## Implemented Guarantees

- Server-side `ActorContext` for tenant and user scope.
- KB role enforcement on tenant-scoped API routes.
- CSRF for unsafe cookie-authenticated requests.
- Opaque HttpOnly app session cookie with hashed session/CSRF storage.
- Argon2id local password hashes.
- OIDC issuer/audience/nonce/signature validation and `issuer + sub` identity matching.
- Encrypted server-side provider token storage.
- Tenant/KB filters in retrieval and delete-by-query paths.
- Safe public document metadata and safe error envelopes.

## Accepted Local MVP Risks

- Local Compose includes deterministic development credentials.
- `AUTH_DISABLED=true` exists for local/demo workflows.
- UI failure rendering is incomplete for some forbidden and stream-failure paths.
- Redis/Valkey exists in Compose but runtime use is not confirmed.
- API `/ready` does not currently check every dependency.

## Production Hardening Not Yet Done

- External TLS/reverse proxy and secure cookie deployment.
- Production secret mounting and rotation policy.
- Malware scanning.
- External ACL connector policy.
- Restore drills and backup automation.
- Observability backend, retention and alerting.
- Browser UI OIDC smoke through Dockerized API.
