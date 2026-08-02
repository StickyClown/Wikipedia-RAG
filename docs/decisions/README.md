# Architecture Decision Records

Use ADRs for durable architecture decisions that future engineers or agents
must understand before changing direction.

Create an ADR for:

- changing an approved runtime component or storage boundary;
- changing tenancy, authorization or source-of-truth semantics;
- introducing a new provider boundary or parser trust boundary;
- choosing a production deployment/security model;
- superseding a previous durable decision.

Do not create an ADR for:

- ordinary implementation steps already captured by an ExecPlan;
- transient validation results;
- small refactors that do not change architecture;
- speculative future work without an approved decision.

Accepted ADRs are immutable. To change an accepted decision, create a new ADR
that supersedes the old one and link both records.

Naming convention:

```text
ADR-NNNN-short-title.md
```

Use zero-padded numbers, starting with the next available number.

Template: [ADR-template.md](ADR-template.md).
