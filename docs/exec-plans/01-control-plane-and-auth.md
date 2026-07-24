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

- [ ] Plan refined after Phase 0 code exists.
- [ ] Implemented.
- [ ] Reviewed.
