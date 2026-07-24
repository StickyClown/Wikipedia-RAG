# Definition of Done and release checklist

## Per change

- [ ] Scope matches the active ExecPlan.
- [ ] Public behavior is covered by tests.
- [ ] Important error, timeout and cancellation paths are covered.
- [ ] Tenant/access handling is explicit.
- [ ] Structured logs and spans exist at service boundaries.
- [ ] No secret or full provider payload is logged.
- [ ] Lint, format, types and relevant tests pass.
- [ ] Documentation and STATUS are current.
- [ ] Diff reviewed for accidental generated files, lockfile drift and broad suppressions.

## Per service

- [ ] Container runs as non-root where practical.
- [ ] Health and readiness endpoints are distinct.
- [ ] Graceful shutdown works.
- [ ] Timeouts and bounded retries are configured.
- [ ] Configuration is validated at startup.
- [ ] Metrics and trace correlation IDs are emitted.
- [ ] Image uses pinned base/version in release profile.

## Per release

- [ ] Clean clone/bootstrap succeeds.
- [ ] Database migration from previous release succeeds.
- [ ] Rollback/recovery rehearsal is documented.
- [ ] E2E smoke scenario succeeds.
- [ ] Security checklist passes.
- [ ] Relevant eval slices do not regress beyond budget.
- [ ] SLO smoke/load evidence is recorded.
- [ ] Release notes list schema/index/model/template changes.
