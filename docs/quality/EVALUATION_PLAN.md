# Evaluation plan

## Principle

Codex must build evaluation before optimizing retrieval. Changes are isolated: one major variable per ablation.

## Dataset target

Create 300–500 questions bound to fixed Wikipedia/document snapshots:

- 120–150 single-hop facts;
- 40–60 rare entities and redirects;
- 30–50 ambiguity/disambiguation;
- 30–50 list/table/infobox;
- 40–60 date/comparison;
- 40–60 multi-hop;
- 20–40 unanswerable/no-evidence;
- at least 60–100 Russian or cross-lingual cases.

Each case stores expected pages/sections, admissible evidence, forbidden unsupported claims and expected normal/extended mode.

## Retrieval metrics

Recall@5/10/20/50, nDCG@10, MRR, hit rate, context precision/recall, duplicate rate, source diversity and evidence coverage.

## Answer metrics

Correctness, faithfulness, citation precision/recall, claim coverage, unsupported-claim rate, completeness and appropriate refusal.

## Operational metrics

p50/p95/p99 per stage, OpenSearch latency, queue wait, embedding throughput, rerank latency, TTFT, tokens/sec, ingestion rate, parser degradation, provider cost and GPU memory. Local JSONL eval reports must keep stage timing metrics separate from retrieval/answer quality metrics.

## Required ablation order

1. BM25 only.
2. Dense only.
3. BM25 + dense + RRF.
4. Add reranker.
5. Chunking variants.
6. Parent/neighbor expansion.
7. Dedup/page quotas/context packing.
8. Embedding model challengers.
9. Triggered query rewrite/decomposition.
10. Normal vs Extended Search.

## Release rule

Do not publish a new pipeline/model/index if it regresses critical slices, increases no-answer false positives beyond budget, fails citation thresholds, exceeds SLO/cost limits or lacks tested rollback.

Evaluation output must be machine-readable and stored under `artifacts/eval/` or object storage, never committed when large. The first local ZIM implementation uses JSONL artifacts plus manifests and does not add database tables.
