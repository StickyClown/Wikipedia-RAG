# ExecPlan 37 - Single Workspace Resource Grants And OIDC Groups

Status: in progress — clean-reset cutover
Created: 2026-08-17
Target implementer: GPT-5.6 Terra or an equivalently capable coding agent

> **Final cutover decision (2026-08-17).** This plan no longer migrates or
> converts legacy tenant data.  After the workspace-only code and bootstrap
> schema pass their checks, the guarded `workspace-reset` command deletes the
> configured PostgreSQL `public` schema, MinIO bucket, Redis DB and
> `wiki-chunks-*` indices, then initializes a new workspace.  A legacy schema
> must fail with `WORKSPACE_RESET_REQUIRED`; do not run the historical
> preflight/apply transform.  Historical migration text remains immutable only
> as ledger history and is not replayed for a clean bootstrap.

## 1. Objective

Replace the current multi-tenant, multi-layer authorization model with a
single-workspace resource-grant model inspired by current Open WebUI access
control.

The delivered system must have two simple authorization anchors:

1. A knowledge base is the primary shareable resource. Its grants are inherited
   by its documents by default.
2. A document is the smallest content resource with its own policy. It may keep
   inherited KB access or become private/selectively shared. Chunks, sections,
   versions, artifacts and evidence never own an independent ACL.

The same work must provide usable local groups and OIDC-managed groups:

- administrators can create local groups and assign users;
- KBs and documents can be shared with users or groups;
- OIDC group claims synchronize external memberships at login;
- JIT creation of OIDC groups is optional and disabled by default.

This is an intentional clean compatibility break. Do not retain the old tenant
API, tenant selector, KB role API or `document_access` JSON API after cutover.
Existing durable tenant data is deliberately not migrated: a legacy deployment
must stop with `WORKSPACE_RESET_REQUIRED` until its operator performs the
guarded clean reset described in this plan.

## 2. Required Reading And Working Rules

Before editing, read:

1. `README.md`;
2. `docs/STATUS.md`;
3. `docs/architecture/security-and-tenancy.md`;
4. `docs/architecture/data-and-storage.md`;
5. `docs/architecture/web.md`;
6. `docs/architecture/flows.md`;
7. this plan in full.

Follow `AGENTS.md`. In particular:

- preserve unrelated dirty-worktree changes;
- use a new numbered forward migration; never rewrite migration `001`-`009`;
- do not run the destructive apply phase against a non-test database without
  explicit user approval, a completed preflight report and a verified backup;
- keep PostgreSQL authoritative and OpenSearch/Redis derived;
- never expose content, credentials, claims or object keys in logs or migration
  reports;
- update `docs/STATUS.md` when implementation begins and after each delivered
  slice or blocker;
- use focused checks after each slice and run the complete authorization gate
  before declaring the plan complete.

Do not attempt this as one unreviewed patch. Implement the numbered slices in
order and stop at every checkpoint described below.

## 3. Decisions Already Made

These decisions are final for this execution plan. The implementer must not
reopen them unless repository evidence makes the design impossible or unsafe.

### 3.1 Workspace and administrators

- Remove tenants as a product and runtime authorization concept.
- Do not merge or transform existing tenants. The reset discards their data and
  creates one new global workspace.
- Keep two platform roles only: `USER` and `PLATFORM_ADMIN`.
- A platform administrator has read, write, share and delete access to every KB
  and document.
- The development bootstrap user is a `PLATFORM_ADMIN`; legacy user and role
  rows are not converted.
- Remove `TenantRole`, `tenant_memberships`, `active_tenant_id`, tenant selection
  and tenant administration endpoints.

### 3.2 Grants and ownership

- Grants are additive. There is no explicit deny rule.
- Supported principals are `USER` and `GROUP` only. Anonymous/public wildcard
  access is out of scope.
- Supported permissions are `READ` and `WRITE`.
- `WRITE` always satisfies a `READ` check. The persistence layer may store both
  rows for compatibility with an Open WebUI-shaped response, but authorization
  must treat `WRITE` as the stronger permission.
- Every KB and document has one `owner_user_id`.
- Ownership always supplies `READ`, `WRITE`, share and delete permission.
- Direct grants never change ownership.

### 3.3 KB/document inheritance

- Every document has `inherits_kb_access: bool`.
- New documents default to `inherits_kb_access=true`.
- Effective document access is the union of:
  - platform-administrator access;
  - document-owner access;
  - direct document grants;
  - effective KB grants and KB ownership when `inherits_kb_access=true`.
- Setting `inherits_kb_access=false` makes the document owner-only unless direct
  document grants exist.
- A direct document grant is sufficient to access that document even when the
  user has no general KB grant. It does not grant discovery of or access to any
  sibling document.
- A user with only partial document access may see a minimal safe KB shell
  (`id`, `name`, `access_scope=partial`) required to search/open those documents,
  but may not inspect KB configuration, sources, grants or unrelated counts.

### 3.4 Upload and management rules

- The user completing a direct upload becomes the document owner.
- An imported/synchronized document is owned by the owner of its KB.
- KB `WRITE` permits uploading, source configuration and content operations in
  that KB.
- Document `WRITE` permits reprocessing or updating that document only.
- Only a platform administrator, resource owner or KB owner may replace grants
  or delete the resource. A generic `WRITE` grantee may not reshare or transfer
  ownership.
- KB owners may manage every document in their KB, including non-inheriting
  documents. Ordinary KB writers may manage documents they own plus documents
  on which they have direct `WRITE`.
- Ownership transfer is out of scope for this plan.

### 3.5 Groups and OIDC

- Groups are global workspace resources with type `LOCAL` or `OIDC`.
- Local group membership is administrator-managed.
- OIDC membership is provider-managed and read-only in the UI.
- OIDC sync replaces only membership rows whose source is `OIDC`; it must never
  delete local membership rows.
- OIDC sync occurs after validated login claims and before the application
  session is returned.
- With sync enabled, an absent, null, malformed or empty configured group claim
  resolves to an empty external group set and revokes all previous OIDC
  memberships for that user. Emit only a safe audit code/count, never raw claims.
- Unknown external groups are ignored when JIT is disabled.
- Unknown external groups are created when JIT is enabled.
- `OIDC_GROUP_JIT_CREATION_ENABLED` defaults to `false`.

## 4. Target Authorization Model

### 4.1 Core types

Replace the old authorization enums with:

```python
class PlatformRole(StrEnum):
    user = "USER"
    platform_admin = "PLATFORM_ADMIN"


class ResourceType(StrEnum):
    knowledge_base = "KNOWLEDGE_BASE"
    document = "DOCUMENT"


class PrincipalType(StrEnum):
    user = "USER"
    group = "GROUP"


class ResourcePermission(StrEnum):
    read = "READ"
    write = "WRITE"
```

`ActorContext` becomes:

```python
@dataclass(frozen=True, slots=True)
class ActorContext:
    user_id: str
    platform_role: PlatformRole
    session_id: str
    authentication_method: AuthenticationMethod
    request_id: str
    trace_id: str
```

It must not contain tenant, role or group authority. Group membership is loaded
from PostgreSQL at the authorization boundary so a membership revocation takes
effect without session reissue.

### 4.2 Canonical grant shape

The canonical API representation is:

```json
{
  "id": "uuid",
  "principal_type": "USER | GROUP",
  "principal_id": "uuid",
  "permission": "READ | WRITE"
}
```

Create/replace payloads omit `id`; the server generates it. Normalize duplicate
entries by `(principal_type, principal_id, permission)`. Reject unsupported
values, nonexistent users/groups and cross-type identifiers with a safe `422`
error before mutating any row.

### 4.3 Authorization matrix

| Action | Allowed actors |
| --- | --- |
| List/open KB | admin, KB owner, KB `READ`/`WRITE`, or partial document grantee via safe shell |
| Query all inheriting documents in KB | admin, KB owner, KB `READ`/`WRITE` |
| Query directly shared documents | admin, document owner, direct document `READ`/`WRITE` |
| Create KB | any authenticated non-disabled user |
| Upload/create source in KB | admin, KB owner, KB `WRITE` |
| Read document | admin, document owner, direct document `READ`/`WRITE`, or inherited KB access |
| Reprocess document | admin, KB owner, document owner, direct document `WRITE` |
| Replace KB grants | admin or KB owner |
| Replace document grants/inheritance | admin, KB owner or document owner |
| Delete KB | admin or KB owner |
| Delete document | admin, KB owner or document owner |
| Manage groups/users | admin only |

Every public route must enforce this matrix in the backend. UI guards are not
authority.

### 4.4 Central authorization service

Create one small authorization module instead of duplicating SQL and branching
through handlers. It must expose typed operations equivalent to:

```python
load_actor_group_ids(conn, *, user_id) -> frozenset[str]
has_resource_permission(conn, *, actor, resource_type, resource_id, permission) -> bool
require_resource_permission(...)
require_resource_owner_or_admin(...)
load_kb_access_scope(conn, *, actor, kb_id) -> KnowledgeBaseAccessScope
load_document_access_scope(conn, *, actor, document_id) -> DocumentAccessScope
batch_authorize_documents(conn, *, actor, document_ids, permission="READ") -> set[str]
```

Batch operations are mandatory on retrieval paths. Do not introduce one SQL
query per candidate, chunk, citation or evidence item.

## 5. Database Contract And Migration

> **Clean-reset rule.** The remainder of this section supersedes the earlier
> migration language in this document. Historical migrations `001`–`009` stay
> immutable evidence of prior releases; they are neither edited nor replayed
> against a clean deployment. The only new ledger marker is
> `010_single_workspace_clean_reset_v1`.

### 5.1 New/changed authoritative tables

For an empty database, create the final workspace-only schema directly and add
`010_single_workspace_clean_reset_v1` to its schema ledger. `ensure_schema`
and normal `migrate` must first detect legacy `tenants` or
`tenant_memberships` tables and fail safely with `WORKSPACE_RESET_REQUIRED`,
without changing any legacy rows.

Create:

```sql
CREATE TABLE access_grants (
  id uuid PRIMARY KEY,
  resource_type text NOT NULL
    CHECK (resource_type IN ('KNOWLEDGE_BASE','DOCUMENT')),
  resource_id text NOT NULL,
  principal_type text NOT NULL CHECK (principal_type IN ('USER','GROUP')),
  principal_id uuid NOT NULL,
  permission text NOT NULL CHECK (permission IN ('READ','WRITE')),
  created_by_user_id uuid NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(resource_type, resource_id, principal_type, principal_id, permission)
);
```

Because `resource_id` addresses both UUID KB IDs and text document IDs, enforce
resource existence and principal type in repository/service code. Add indexes
for resource lookup and principal lookup.

Change:

- `knowledge_bases`: add non-null `owner_user_id` after backfill; remove
  `tenant_id` and tenant indexes;
- `documents`: add non-null `owner_user_id`, non-null
  `inherits_kb_access default true`; remove canonical ACL fields from metadata;
- `upload_sessions`: add non-null `created_by_user_id` for new rows after legacy
  backfill;
- `groups`: remove `tenant_id`; add nullable `external_issuer`; retain
  `group_type`, `external_id`, safe metadata and timestamps;
- `group_memberships`: rename/normalize `membership_type` to
  `membership_source` with `LOCAL|OIDC`; retain uniqueness on
  `(group_id,user_id,membership_source)`;
- `auth_sessions`: remove `active_tenant_id`;
- `audit_events`: remove the tenant foreign key/column; retain former tenant
  information only in bounded migration audit metadata when needed;
- all other authoritative and operational tables: remove `tenant_id` from
  columns, foreign keys, primary/unique keys, indexes and repository predicates.

New object-storage keys must derive from KB/document scope, never a tenant.
Old object keys are deleted by the clean reset and are not retained as
compatibility paths.

### 5.2 Guarded workspace-reset inventory

Implement a read-only inventory as the default workspace-reset command, for
example:

```powershell
make workspace-reset
```

The report contains only bounded counts and the confirmed categories/targets:
configured WikipediaRag database and `public` schema, configured MinIO bucket,
configured Redis DB, and OpenSearch prefix `wiki-chunks-*`. It must not expose
raw document metadata, claims, object keys, content, credentials or a list of
individual records.

The apply path requires all of `WORKSPACE_RESET_ENABLED=true`, `--apply` and
`--all-data-confirmed`. Before `DROP SCHEMA`, it verifies that the configured
target has a recognized WikipediaRag table set; an empty/unknown shared schema
is rejected with `WORKSPACE_RESET_TARGET_UNVERIFIED`. It is repeatable after a
partial derived-store failure and always converges to an empty workspace.

### 5.3 Apply mechanics and store boundaries

- Stop API and worker writers; run the final API image with only PostgreSQL,
  Redis, MinIO and OpenSearch started.
- Drop only the verified configured PostgreSQL `public` schema, clear only the
  configured MinIO bucket, execute `FLUSHDB` only in the configured Redis DB,
  and delete only `wiki-chunks-*` indices.
- Initialize the final workspace schema and development seed immediately after
  the authoritative PostgreSQL clear.
- Persist the bounded dry-run inventory, exact commands and exit codes under
  `artifacts/validation/exec37-final-reset/` and summarize them in
  `docs/STATUS.md`.
- Never use `docker compose down -v` in the normal path. A raw volume deletion
  is an emergency-only recovery for a service that cannot start, and is limited
  to the explicitly verified primary volumes named in the cutover checklist.

### 5.4 Derived-store outcome

The reset clears derived stores instead of rebuilding legacy content. The new
OpenSearch mapping has no `tenant_id` or metadata ACL. A later import creates
only KB-scoped identities. A failure clearing one derived store must not reopen
traffic; rerunning the guarded command remains safe.

## 6. API Contract

### 6.1 Removed interfaces

Remove routes and schemas for:

- `/api/v1/auth/session/tenant`;
- `/api/v1/admin/tenants` and tenant item routes;
- old KB role/grant CRUD using `VIEWER|EDITOR|MANAGER|OWNER`;
- old `PATCH /api/v1/documents/{id}/access` payload with
  `policy/user_ids/group_ids`;
- source `document_access_default` and source access update endpoint;
- tenant IDs in request/response bodies, cursor fingerprints and public errors.

Because clean cut was selected, do not ship compatibility aliases. Update every
repository-owned UI, CLI and test client in the same release.

### 6.2 Grant APIs

Add:

```text
GET /api/v1/knowledge-bases/{kb_id}/access-grants
PUT /api/v1/knowledge-bases/{kb_id}/access-grants
GET /api/v1/documents/{document_id}/access-grants
PUT /api/v1/documents/{document_id}/access-grants
```

`PUT` is an atomic full replacement:

```json
{
  "access_grants": [
    {
      "principal_type": "GROUP",
      "principal_id": "uuid",
      "permission": "READ"
    }
  ],
  "inherits_kb_access": false
}
```

`inherits_kb_access` is accepted only for a document. KB requests containing it
return `422`. Validate the complete replacement before deleting existing rows.
On failure, preserve the old grants unchanged.

Only admin/resource owner/KB owner can view the complete grant list. Ordinary
readers receive only derived booleans such as `write_access`, never other users'
IDs or group memberships.

### 6.3 Resource responses

Owner/admin responses may contain:

```json
{
  "owner_user_id": "uuid",
  "write_access": true,
  "share_access": true,
  "access_scope": "full | partial",
  "inherits_kb_access": true,
  "access_grants": []
}
```

For ordinary users:

- omit `access_grants`;
- expose `owner_user_id` only if already needed by the UI; otherwise expose a
  safe `owned_by_current_user` boolean;
- return `write_access`, `share_access=false` and `access_scope`;
- return cross-resource denial as the repository's standard safe 404 where ID
  disclosure would be harmful.

### 6.4 Group APIs

Retain and finish the existing group CRUD under `/api/v1/groups`, but update the
models to global workspace semantics. Responses include:

```json
{
  "id": "uuid",
  "name": "Engineering",
  "description": "...",
  "group_type": "LOCAL | OIDC",
  "external_id": null,
  "member_count": 12,
  "member_user_ids": []
}
```

- Admin list/detail may include member IDs.
- Resource-sharing selectors receive only group `id`, `name`, `group_type`.
- OIDC groups cannot have membership edited manually.
- Deleting a group that is referenced by grants returns `409 GROUP_IN_USE`
  with a bounded reference count. Do not cascade-delete access silently.

## 7. Authentication And OIDC Sync

### 7.1 Configuration

Keep the existing configurable nested group claim and add:

```python
oidc_group_sync_enabled: bool = False
oidc_group_jit_creation_enabled: bool = False
```

Deprecate/remove `oidc_group_catalog_sync_enabled` if it is unused or ambiguous.
Update `.env.example`, compose Keycloak configuration and deployment docs.

### 7.2 Sync algorithm

Within the validated OIDC callback transaction:

1. Parse the configured nested claim into a normalized set of non-empty string
   external IDs.
2. Lock current OIDC memberships for that user.
3. Resolve matching groups by `(issuer,external_id)`.
4. If JIT is enabled, create missing OIDC groups idempotently.
5. Replace only `membership_source='OIDC'` rows for that user.
6. Leave every `LOCAL` membership untouched.
7. Audit safe counts: received, matched, created, added, removed and ignored.
8. Commit user/session/membership changes consistently. Do not return a session
   whose claimed groups failed to synchronize.

Concurrent first login for the same external group must create one group via a
unique constraint/upsert, not duplicate rows.

## 8. Retrieval, Search, Cache And Research

### 8.1 Candidate authorization

Search input is divided into:

- fully readable KB IDs from KB ownership/grants;
- directly readable document IDs from ownership/document grants;
- KB IDs represented only by directly readable documents.

OpenSearch may filter by full KB IDs and explicit document IDs. It must not
receive user IDs, group IDs or grants as indexed chunk metadata.

After candidate retrieval, PostgreSQL must batch-confirm:

- document lifecycle is active;
- candidate belongs to the current published version;
- KB/document still exists;
- current actor still has document `READ`.

Use bounded adaptive over-fetch/search-after to fill the requested result window
after unauthorized candidates are removed. Bound attempts and total candidates
using settings/constants; a bound hit returns fewer safe results rather than
leaking or running without limit.

### 8.2 Cache correctness

Authorization-sensitive cache fingerprints must include a stable access marker
derived from:

- actor user ID;
- sorted current group IDs;
- maximum relevant grant/group-membership revision or a dedicated monotonic
  authorization revision;
- selected full KB IDs and explicit document IDs.

Regardless of the fingerprint, cached result documents are reauthorized against
PostgreSQL before response. Revocation may cause a safe cache miss/short page but
must never expose stale access.

### 8.3 Chat, citations and research

- Query-run and research scope no longer stores tenant authority.
- Stored evidence never grants future access.
- Citation opening, report rendering, research resume and persisted evidence
  loading all call the same current document authorization service.
- A run may continue with remaining visible evidence after access revocation; it
  must hide revoked evidence/claims and abstain when support becomes insufficient.
- Planner/tool/model inputs may select only server-authorized KB/document scopes.

### 8.4 Projection events

Remove document ACL projection events and OpenSearch update-by-query ACL writes.
Grant and membership changes are authoritative immediately in PostgreSQL and
invalidate/bump the authorization revision. Keep publication and lifecycle
projection/reconciliation behavior unchanged.

## 9. UI Work

The current UI is concentrated in `services/ui/src/App.tsx`; refactor access UI
into focused components if needed, but do not perform an unrelated application
rewrite.

Deliver:

1. Remove tenant indicators, tenant selector and tenant admin screens/types.
2. Add Admin > Groups:
   - list/search groups;
   - create/edit/delete local groups;
   - search users and edit local membership;
   - show OIDC badge, external ID and read-only membership;
   - handle empty/loading/forbidden/error states.
3. Replace role editors with a reusable grant editor:
   - user/group search;
   - `Read`/`Write` permission selector;
   - duplicate prevention;
   - explicit save/cancel;
   - safe display of server validation failures.
4. KB sharing uses the grant editor.
5. Document sharing additionally exposes `Inherit access from knowledge base`.
6. Upload UI states that the document will inherit KB access and that the
   uploader will own it.
7. KB list marks partial KB shells and does not expose settings/actions for them.
8. Hide share/delete controls unless server response grants those capabilities.

The frontend must not infer authority from group membership, ownership strings
or cached role data. It renders capability booleans returned by the API.

## 10. Implementation Slices

### Slice 0 - Baseline and inventory

- Record `git status --short`; preserve existing `AGENTS.md` and
  `docs/chunking-and-opensearch-problem.md` changes.
- Run focused existing authorization, OIDC, document, retrieval and research
  tests and record exact commands/results in `docs/STATUS.md`.
- Generate a machine-readable inventory of every `tenant_id`, `TenantRole`, KB
  role, `document_access`, group membership and ACL projection reference.
- Confirm Docker/runtime state before any integration check.

Checkpoint: no code behavior changed; inventory accounts for all current
authorization entry points.

### Slice 1 - New policy types and pure resolver tests

- Add resource/principal/permission types and pure permission-composition logic.
- Add exhaustive deterministic unit tests for the matrix in section 4.3.
- Do not switch handlers or storage yet.

Checkpoint: new policy tests pass; old tests still pass.

### Slice 2 - Final schema and legacy refusal

- Add the workspace-only bootstrap schema and ledger marker
  `010_single_workspace_clean_reset_v1` without rewriting historical
  migrations `001`–`009`.
- Add a fresh-bootstrap integration test proving the final schema, owner/grant
  foreign keys and idempotent initialization.
- Add a legacy-schema test proving `WORKSPACE_RESET_REQUIRED` leaves rows
  unchanged.

Checkpoint: fresh bootstrap and legacy refusal pass on a disposable PostgreSQL
database; no real database reset has occurred.

### Slice 3 - Central grant repository and authorization service

- Implement typed grant CRUD, batch authorization and safe audit events.
- Prevent partial replacement on invalid input.
- Remove ad hoc effective-role/group SQL from new code paths.

Checkpoint: owner/admin/direct/group/inherited/private and revocation tests pass.

### Slice 4 - Authentication and global groups

- Simplify `ActorContext` and auth session response.
- Remove active tenant selection and tenant administration.
- Implement OIDC sync/JIT rules.
- Convert group CRUD and user pickers to global workspace behavior.

Checkpoint: local login, OIDC login, session, local membership and strict OIDC
membership tests pass, including concurrent JIT creation.

### Slice 5 - KB/document/upload/source APIs

- Switch all KB/document boundaries to the centralized resource resolver.
- Add owner tracking and grant replacement APIs.
- Remove legacy KB role/document/source access APIs.
- Ensure upload owner is persisted at session creation/completion.
- Remove source ACL defaults; imported documents inherit their KB.

Checkpoint: API authorization tests cover allowed, insufficient, direct-share,
partial-KB and failed-mutation paths.

### Slice 6 - Retrieval and derived stores

- Remove ACL/tenant metadata from the index mapping and indexing payload.
- Add full-KB plus direct-document candidate scopes and bounded over-fetch.
- Add PostgreSQL batch current-state authorization.
- Replace cache scope markers and remove ACL projection work.

Checkpoint: search/retrieval tests prove revocation, stale index/cache safety,
directly shared documents and no per-candidate SQL loop.

### Slice 6.5 - Remove technical tenant runtime/storage code

This is a required open slice, not documentation debt. Remove all remaining
technical `tenant_id` arguments, SQL predicates, DTO fields, job/event fields,
stable-hash inputs, observability fields and storage/index identities from
executable code. Replace them with global resource IDs or KB/document-scoped
identities as appropriate. Remove temporary SQL normalizers and compatibility
shims once their callers have been converted.

Cover repository, ingestion, uploads, source connectors, worker dispatch,
projection/reconciliation, retrieval, query-run, Extended Search, Deep
Research, audit and CLI paths. Static boundary checks must allow tenant words
only in immutable historical migration text and explicitly archived documents.

Checkpoint: the workspace-only schema runs all executable paths without tenant
arguments; a targeted static audit finds no live tenant compatibility layer.

### Slice 7 - Chat, Extended Search and Deep Research

- Remove tenant arguments/storage from query/research paths.
- Reuse batch document authorization for citations and durable evidence.
- Cover revoke-after-persistence and resume-after-revoke.

Checkpoint: focused Extended/Deep Research tests pass without hidden legacy
authorization fallback.

### Slice 8 - UI cutover

- Remove tenant UI/types.
- Implement group administration and grant editors.
- Update every API payload/response type.
- Add focused Vitest and Playwright coverage.

Checkpoint: lint, typecheck, tests and production build pass; visible states are
verified.

### Slice 9 - Guarded clean-reset rehearsal and runtime gate

- On a disposable environment, run reset disabled/confirmation guard tests,
  dry-run, bounded store-boundary tests and a repeat run after one derived-store
  failure.
- Rebuild the final API image, stop API/worker writers, then run the actual
  guarded reset only after Slice 6.5 is complete.
- Start the complete stack and exercise local login, default admin ownership of
  the seeded KB, workspace grants, retrieval and readiness with no legacy data.

Checkpoint: the bounded report is retained under
`artifacts/validation/exec37-final-reset/` and contains no sensitive data.

### Slice 10 - Contract and status closure

- Update architecture contracts and contract-map tests.
- Update `.env.example`, README/deployment notes and functional verification.
- Update `docs/STATUS.md` with delivered behavior, migration readiness, exact
  validation and any remaining production apply blocker.
- Review the final diff for authorization gaps, data loss, migration ordering,
  logging, unbounded work and accidental scope growth.

Checkpoint: all acceptance criteria below are satisfied.

## 11. Required Tests

### 11.1 Unit tests

At minimum cover:

- platform admin full access;
- KB/document owner behavior;
- direct user and group `READ`/`WRITE`;
- `WRITE` satisfying `READ`;
- inheritance enabled/disabled;
- partial KB shell behavior;
- group membership union;
- no explicit deny semantics;
- grant replacement validation/atomicity;
- deleted/missing group references;
- OIDC claim missing/null/string/list/nested/empty;
- JIT disabled/enabled and concurrent upsert;
- local memberships surviving OIDC sync;
- no raw claim or principal dump in errors/logs.

### 11.2 Integration tests

- Fresh bootstrap has no `tenants`, `tenant_memberships`,
  `knowledge_base_grants`, `tenant_id` or `active_tenant_id` schema residue.
- A legacy schema returns `WORKSPACE_RESET_REQUIRED` without a mutation.
- Reset rejects disabled, incomplete-confirmation and unverified targets;
  dry-run exposes only bounded inventory and apply touches only the configured
  database/schema, bucket, Redis DB and `wiki-chunks-*` prefix.
- A repeat reset after a derived-store failure converges safely.
- Upload -> owner -> inherited access -> private -> group share lifecycle.
- Unauthorized grant update creates no partial rows/audit success.
- Immediate access loss after group/grant revocation.
- Stale OpenSearch/Redis cannot broaden access.
- Direct document share works without sibling/KB configuration exposure.
- Research/citation evidence is hidden after revocation.
- Reindex failure leaves KB safely unavailable.

### 11.3 UI tests

- Admin local group CRUD and membership editing.
- OIDC group read-only rendering.
- KB grant editor user/group read/write grants.
- Document inherit/private/direct-share flow.
- Partial KB rendering.
- Forbidden and failed saves preserve prior UI/server state.
- Tenant selector and legacy access controls are absent.

### 11.4 Suggested validation commands

Select exact test files as they are added, but the final gate must include the
Windows equivalents of:

```powershell
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
uv run pytest tests/unit -q
uv run pytest tests/integration -q

Push-Location services/ui
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm test:e2e
Pop-Location

make verify-cross-tenant-hardening
make test-functional-retrieval
```

Rename `verify-cross-tenant-hardening` to a workspace resource-access gate only
after preserving equivalent isolation, direct-share and stale-projection cases.
Never report a command as passing unless it was run successfully; record exact
commands and exit codes on Windows.

## 12. Acceptance Criteria

Implementation is complete only when all of the following are true:

1. No public/runtime authorization path depends on tenant or KB role enums.
2. No chunk, section, version, artifact, cache entry or evidence row owns an ACL.
3. PostgreSQL resource grants, ownership and group membership are the only
   content-authorization source of truth.
4. New uploads are owned by the uploader and inherit KB access.
5. Documents can be made private or shared directly with users/groups.
6. Local groups are administrable in the UI.
7. OIDC membership sync and optional JIT creation behave exactly as specified.
8. Admin has full access; the clean bootstrap creates the configured development
   administrator and seeded KB without tenant rows.
9. No legacy tenant data is converted, dual-read or retained after reset.
10. Retrieval, cache, citations and research reauthorize current document state.
11. ACL changes require no chunk reindex/update-by-query.
12. Legacy tenant, KB role, document ACL and source ACL APIs are removed from
    backend, UI, clients and tests.
13. The guarded reset inventory/apply/repeat rehearsal succeeds on a disposable
    environment and its evidence records only bounded counts and safe targets.
14. Architecture docs and `docs/STATUS.md` describe the delivered contract and
    exact validation truthfully.
15. Executable code has no technical tenant argument, SQL predicate, DTO field,
    event field, storage key, index identity, cache key or observability field.
    The only permitted tenant references are immutable historical migration text
    and explicitly archived documentation.

## 13. Explicit Non-goals

- anonymous or internet-public knowledge bases/documents;
- explicit deny grants or group precedence;
- nested groups;
- SCIM, LDAP or periodic provider-directory synchronization;
- multiple simultaneous OIDC providers;
- ownership transfer;
- per-chunk, per-section, per-version or per-citation ACL;
- preservation, conversion or migration of legacy tenant data;
- raw Docker-volume deletion in the standard reset path;
- unrelated retrieval-quality, chunking, model-provider or UI redesign work.

## 14. Final Implementer Checklist

Before claiming completion, answer each item with evidence:

- Which function is the single authorization boundary for KB/document reads?
- Which batch query reauthorizes retrieval candidates?
- How does a grant or group revocation invalidate cached access?
- How are partial KB shells prevented from exposing configuration or siblings?
- Which static audit proves no live technical tenant code remains outside the
  immutable historical/archived allowlist?
- Which fresh-bootstrap and legacy-refusal tests prove the final database
  contract?
- Which tests prove failed authorization creates no mutation/job?
- Which tests prove persisted research evidence does not preserve access?
- Where is the bounded reset inventory/rehearsal report?
- Which guarded reset action was run, against which verified target, and with
  which recorded exit code?

If any answer is missing, the plan is not complete.
