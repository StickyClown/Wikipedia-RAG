# ExecPlan 08 — Bounded Extended Search

## Outcome

Complex, comparison, multi-hop or evidence-starved queries can enter a bounded iterative retrieval state machine that improves evidence coverage and exposes a complete agent trace.

## In scope

- rule/model-assisted intent router with explicit features;
- typed state machine;
- query decomposition and parallel subqueries;
- evidence ledger;
- coverage assessment;
- dedup/loop breaker;
- wall-time/step/model-call budgets;
- explicit stop reasons;
- normal-vs-extended evaluation.

## Out of scope

Multi-agent swarm, autonomous external web actions and full GraphRAG.

## Acceptance criteria

- normal factual queries remain on normal path;
- duplicate normalized tool calls do not loop;
- every run stops within hard budget;
- partial evidence yields qualified output;
- selected multi-hop slices improve without unacceptable latency/cost regression;
- trace UI shows state transitions and stop reason.

## Validation

```bash
make test-unit TEST=agent-state-machine
make test-integration TEST=extended-search
make eval EVAL_SET=wiki-multihop COMPARE=normal,extended
```

## Progress

- [x] Plan refined for bounded local MVP.
- [x] Implemented bounded local MVP.
- [x] Reviewed with local checks.

## MVP status

- Implemented a bounded single-orchestrator loop with local retrieval tool usage, evidence ledger, duplicate/step budget guard and persisted stop reason.
- Unit tests cover budget and loop-break behavior.

## Remaining production work

- Query decomposition, parallel subqueries, coverage scoring and UI state-transition visualization remain future hardening.
