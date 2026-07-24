# ADR-006 — Extended Search orchestration

Status: accepted baseline

## Decision

Start with a typed bounded Python state machine and one orchestrator. Add LangGraph only if durable checkpointing or trace tooling creates measurable value. Do not implement a multi-agent swarm.

## Consequences

- normal RAG remains the default path;
- explicit router triggers Extended Search;
- tool calls have hard count/time/token budgets and deduplication hashes;
- evidence ledger is append-only for a run;
- every stop reason is persisted.
