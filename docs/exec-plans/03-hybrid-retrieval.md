# ExecPlan 03 — Hybrid retrieval, RRF and reranking

## Outcome

Queries retrieve tenant-filtered Wikipedia evidence through parallel BM25 and dense search, service-side RRF and reranking, while recording candidate-level events.

## In scope

- embedding call via Model Gateway;
- vector mapping/version contract;
- BM25 and HNSW searches in parallel;
- RRF with stage-specific ranks;
- rerank through gateway;
- dedup and page quotas;
- retrieval-run/event persistence;
- deterministic fixture evaluation.

## Out of scope

Generation, Extended Search, learned sparse, ColBERT and query rewrite.

## Acceptance criteria

- tenant filters apply identically to both retrieval paths;
- BM25, dense, RRF and rerank contributions are inspectable;
- one retriever timeout follows documented degradation policy;
- baseline Recall@K report is machine-readable;
- no hardcoded production tuning values outside versioned config.

## Validation

```bash
make test-unit TEST=retrieval
make test-integration TEST=hybrid-search
make eval EVAL_SET=wiki-mini
```

## Progress

- [ ] Plan refined.
- [ ] Implemented.
- [ ] Reviewed.
