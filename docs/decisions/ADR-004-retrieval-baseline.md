# ADR-004 — Retrieval baseline

Status: accepted

## Decision

Use OpenSearch BM25 and dense HNSW in parallel, service-side reciprocal rank fusion, cross-encoder reranking, dedup/page quotas, selective parent/neighbor expansion and token-budget context packing.

## Consequences

- preserve every stage's ranks/scores in retrieval events;
- do not calibrate BM25 and vector scores into a common numeric scale for baseline fusion;
- learned sparse and late interaction remain ablation branches;
- retrieval parameters are versioned configuration, not constants spread through code.
