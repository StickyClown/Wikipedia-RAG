# Research summary for implementation decisions

The supplied 2024–2026 research document supports the following baseline:

- retain BM25 as a strong exact-term/entity retriever;
- combine BM25 and dense retrieval with RRF;
- prefer section-aware parent-child/small-to-big chunking over universal fixed overlap;
- start with a compact multilingual reranker and evaluate larger challengers separately;
- treat context packing, dedup, page/source quotas and citation validation as first-class modules;
- keep normal RAG deterministic and activate query transformations/agent loop only for ambiguous, comparison, multi-hop or insufficient-evidence cases;
- avoid full GraphRAG and multi-agent systems as the first production path;
- evaluate on a fixed mixed Russian/multilingual set tied to exact snapshots.

The original DOCX is retained in this directory for provenance. `docs/architecture.md` is the implementation authority when wording differs.
