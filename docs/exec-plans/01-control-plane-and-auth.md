# ExecPlan 01 — Control plane and authorization foundation

## Outcome

Authenticated users can access only their tenant-scoped knowledge bases and query-run history through typed APIs with authorization tests.

## In scope

- user/tenant/membership domain;
- chosen development auth profile and a production OIDC interface boundary;
- knowledge-base CRUD metadata;
- audit events for administrative writes;
- server-derived tenant context;
- authorization and cross-tenant regression tests.

## Out of scope

Document ingestion, retrieval and full enterprise SSO implementation.

## Acceptance criteria

- tenant A cannot read/update tenant B resources by ID guessing;
- role matrix tests cover owner/admin/editor/viewer;
- auth-disabled mode cannot be enabled in production profile;
- audit entries contain actor, action, tenant, target and request ID;
- migrations and API contract tests pass.

## Validation

```bash
make lint
make typecheck
make test-unit
make test-integration TEST=auth-tenancy
make smoke
```

## Progress

- [x] Plan refined after Phase 0 code exists.
- [x] Implemented local development tenant/user seed and server-owned tenant scope.
- [ ] Production auth/RBAC implemented.
- [x] Reviewed local MVP scope.

## MVP status

- Implemented seeded local tenant, user, membership and knowledge base.
- Retrieval, upload, query-run and debugger paths use server-owned default tenant context rather than client-provided tenant filters.

## Remaining production work

- OIDC, role matrix enforcement, production auth-disabled guard and audit log remain deferred.
