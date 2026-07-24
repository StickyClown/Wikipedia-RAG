# ADR-003 — Background job runtime

Status: accepted for MVP; review before very large workflows

## Decision

Use Dramatiq with Redis/Valkey broker for initial ingestion workers. PostgreSQL remains the durable job-state source of truth; queue delivery is not treated as durable business state.

## Consequences

- handlers must be idempotent;
- checkpoints and state transitions are persisted in PostgreSQL;
- messages carry identifiers, never full document payloads;
- Temporal may be evaluated later if workflow recovery complexity justifies it.
