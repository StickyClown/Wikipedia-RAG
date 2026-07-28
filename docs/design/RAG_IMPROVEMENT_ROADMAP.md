# RAG Improvement Roadmap

Status: design backlog  
Date: 2026-07-28  
Sources: `C:\Users\Компьютер\Downloads\RAG_PLAN.md`, `docs/architecture.md`, `docs/STATUS.md`  
Current project state: after ExecPlan 17 reviewed dev/test and release gate  

This document is not an active ExecPlan and does not authorize implementing
these improvements together. Each section must be converted into a dedicated
ExecPlan before code, schema, index, model or UI changes begin.

The roadmap captures six design directions that should be used for future
project planning after the reviewed evaluation/release-gate foundation. The
main principle is to keep the existing hybrid retrieval core and improve
adaptivity, grounding, latency, context assembly, model behavior and document
ingestion around it.

## 1. Claim-Level Verifier

Goal: verify semantic support for each factual claim, not only the validity of
citation IDs.

Proposed flow:

```text
answer
-> claims
-> candidate evidence spans
-> support score/verdict
-> repair, qualification or refusal
```

Contracts to design:

- The deterministic citation validator remains the first validation layer.
- Claim support verdicts are `supported`, `partially_supported`,
  `unsupported` and `contradicted`.
- Every verifier decision must reference supplied evidence IDs and source span
  metadata; verifier output must not introduce new evidence.
- Unsupported factual claims must be removed, qualified or converted into an
  insufficient-evidence statement before the answer is accepted.
- Normal logs must not include full document text, raw provider responses or
  prompts.

Metrics:

- unsupported claim rate;
- claim-support precision and recall;
- citation precision and citation recall;
- repair/refusal rate;
- added latency by verifier stage.

Out of scope:

- replacing deterministic citation validation;
- using the generator as the only judge of its own factual support;
- accepting unsupported claims because the citation ID has a valid shape.

## 2. Complexity Router

Goal: choose normal retrieval, decomposition or bounded Extended Search only
when the query needs it.

Routing classes to design:

- `single_hop`;
- `comparison`;
- `multi_hop`;
- `list_aggregation`;
- `ambiguous`;
- `evidence_starved`;
- `conflict`.

Default policy:

- Use cheap deterministic classification first.
- Keep normal deterministic RAG as the default path.
- Use decomposition for comparison, list aggregation and multi-hop queries.
- Use iterative retrieval when the first pass is evidence-starved.
- Use bounded Extended Search only for coverage gaps, conflicts or queries that
  require tool-like reasoning.
- Add model-based routing only after reviewed eval evidence proves deterministic
  routing is insufficient.

Metrics:

- route accuracy by reviewed slice;
- quality lift on hard slices;
- false Extended Search trigger rate;
- p95 latency by route;
- model calls and cost per query.

Out of scope:

- always-on query rewrite;
- making the agent path the default;
- adding multi-agent swarm behavior.

## 3. Latency Hardening

Goal: reduce p95 retrieval latency without regressing critical reviewed slices.

Design directions:

- Introduce per-route rerank top-k budgets.
- Keep stage profiling for BM25, dense embedding, dense search, fusion, rerank,
  context packing, generation and verification.
- Add safe query/result caches where tenant scope, index contract, run contract,
  model alias and retrieval profile are part of the key.
- Tune OpenSearch HNSW/search parameters with measured recall and latency.
- Split easy and hard retrieval profiles instead of maximizing every stage for
  every query.
- Preserve backpressure and hard budgets for Extended Search.

Gate expectations:

- no regression on reviewed quality thresholds;
- `error_rate=0` for release-gate suites;
- stage timing reports preserve p50 and p95 metrics;
- cache behavior is observable through safe hit/miss counters;
- no cross-tenant cache or trace exposure.

Out of scope:

- changing vector store without a dedicated ADR;
- removing rerank globally without evaluated evidence;
- suppressing latency by hiding slow or failed stages from reports.

## 4. Parent-Child Context And Packing

Goal: make small-to-big retrieval a primary design principle for complex
queries.

Design directions:

- Retrieve with child chunks that are small enough for precise matching.
- Expand selected child hits to parent spans when the answer needs local
  context.
- Apply page/source quotas before packing.
- Deduplicate near-identical fragments and redirect aliases.
- Use position-aware packing so the strongest evidence is not buried in the
  middle of the context.
- Preserve source URLs, section paths, chunk IDs and parent IDs for citation
  validation and trace debugging.

A/B design:

- Compare current profile versus parent-child focused profile.
- Evaluate on multi-hop, comparison, list/table-like and hard-negative slices.
- Keep index contract and chunking version explicit when any global rechunking
  is tested.

Metrics:

- evidence recall;
- context precision;
- answer completeness;
- citation recall;
- context token cost;
- duplicate rate;
- source diversity.

Out of scope:

- global rechunking without a versioned index plan;
- proposition indexing for all documents;
- GraphRAG as a substitute for measured parent-child retrieval.

## 5. Model-Specific Harness Profiles

Goal: separate prompt, tool and verification behavior by model role while
preserving the Model Gateway alias boundary.

Profiles to design:

- `generator_main`;
- `verifier`;
- `extended_search_agent`;
- `local_llamacpp`;
- `openrouter`.

Contracts to design:

- Business code continues to use logical aliases only.
- Provider-specific behavior remains in profile/config layers.
- Prompt, response-format, tool-description and refusal policies are versioned.
- Profiles define JSON strictness, repair policy, citation rules and supported
  tool semantics.
- Startup smoke tests must prove required profile capabilities before the alias
  is considered healthy.

Metrics:

- JSON validity rate;
- tool-call stability;
- answer repair rate;
- refusal correctness;
- unsupported-claim rate by profile;
- provider error and fallback rate.

Out of scope:

- direct OpenRouter or llama.cpp calls from business code;
- replacing the Model Gateway contract;
- one universal prompt contract for all model roles.

## 6. Universal Ingestion

Goal: move from a Wikipedia-focused MVP to production document ingestion.

Target flow:

```text
upload
-> safety/MIME
-> parser isolation
-> canonical document
-> template version
-> chunks
-> embeddings
-> staging index
-> validation
-> atomic publish
```

Initial formats:

- PDF;
- DOCX;
- PPTX;
- XLSX/CSV;
- HTML/text;
- image/OCR as a degraded path.

Contracts to design:

- Original files are immutable and stored in object storage.
- Parser output becomes a versioned canonical document artifact.
- Chunk IDs are deterministic within parser, template, embedding and index
  versions.
- Background jobs are idempotent, resumable and never publish partial failed
  content.
- Large ingestion does not run synchronously inside an HTTP request.
- Parser isolation, MIME detection, file size limits and malware scanning policy
  must be explicit before production upload.
- Preview, reprocess and reindex flows must show parser/template/index versions.

Metrics:

- parse success and degraded rate;
- ingestion throughput;
- failed publish rate;
- preview correctness;
- parser timeout rate;
- object artifact completeness;
- reprocess/reindex success rate.

Out of scope:

- full VLM parsing for every PDF;
- GraphRAG;
- proposition indexing;
- permanent mocks for production parser integrations;
- synchronous large-file indexing in HTTP requests.

## Planning Notes

- ExecPlan 17 reviewed evaluation/release gate remains the foundation for
  proving later improvements.
- The recommended future order is:
  1. Claim-Level Verifier.
  2. Complexity Router.
  3. Latency Hardening.
  4. Parent-Child Context And Packing.
  5. Model-Specific Harness Profiles.
  6. Universal Ingestion.
- If an improvement requires changing an accepted ADR, create a new ADR first.
- If an improvement changes index contracts, parser contracts, model aliases or
  public API behavior, document compatibility and rollback in its ExecPlan.

## Documentation Validation

No code tests are required for creating this design backlog. If a future change
updates this file together with code, run the normal validation commands and
record exact results in `docs/STATUS.md`:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```
