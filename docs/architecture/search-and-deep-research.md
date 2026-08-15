# Search, Answer and Deep Research

This document defines the current retrieval and research contracts. Historical
measurements and failure analyses live under `docs/history/`, `docs/research/`
and `docs/exec-plans/`.

## Retrieval Pipeline

```text
authorized scope → contract readiness → BM25 + dense candidates
                 → fusion → rerank → parent expansion
                 → PostgreSQL current-state/ACL confirmation → evidence
```

- Every lane uses server-owned tenant and KB scope.
- Physical indices are versioned and selected through active KB metadata.
- Results expose `index_contract_id`; citations retain source, document,
  version and chunk provenance.
- OpenSearch and Redis are candidate layers. PostgreSQL confirms current active
  version, publication and document access before exposure.
- Missing/incompatible retrieval state fails with `KB_NOT_READY`.
- Multi-KB direct search authorizes every KB and fails closed if any required
  scope is unavailable.

## Search and Extended Search

Ordinary search runs the configured retrieval profile once. Extended Search is
a bounded harness that may decompose/rewrite a question, run additional
retrieval calls and combine authorized evidence. It remains attached to a query
run so debugger events and citations are inspectable.

Extended Search does not grant new scope. Every derived query reuses the same
server-owned tenant/KB/document access boundary.

## Grounded Answer

Answer generation receives only confirmed evidence. The response contains
answer text, resolvable evidence/citations and query-run identity or a typed
safe failure. Citation validation rejects unsupported or unresolvable
references; provider output is never treated as authorization.

## Deep Research

A run has one primary KB and a server-owned snapshot of up to three same-tenant
KBs. PostgreSQL stores lifecycle, scope, questions, episodes, tool calls,
evidence, claims, relations, coverage, decisions and reflections.

Each episode:

1. claims a valid lease and reads the current checkpoint;
2. builds a bounded context envelope from visible durable state;
3. validates a planner decision;
4. executes one allowlisted tool call or a terminal decision;
5. persists public-safe tool metadata and deduplicated evidence;
6. verifies supported/partial/unsupported/conflicting claims;
7. advances state with compare-and-set.

The closed tool registry includes retrieval, section lookup, in-document search,
table/CSV lookup and metadata lookup. Tool arguments cannot select arbitrary
tenant/KB/object authority. Document content is evidence only.

Pause, resume, cancel, heartbeat recovery and partial terminal reports are
durable. Final synthesis is rebuilt from evidence still visible under the
current actor; revoked or unpublished evidence is removed from model context,
claims requiring it and public reports.

## Model Contract

Chat, embeddings, rerank and token counting are distinct Model Gateway
operations. Business code calls aliases; Gateway resolves the active revision
and endpoint adapter. Endpoint suitability is based on the operation contract
and health, not on remote/local placement. Provider-specific options and errors
terminate at the Gateway boundary.

## Search-quality Evaluation

The `eval-quality-*` workflow is separate from runtime search behavior:

1. prepare and manually review material;
2. freeze dataset and material hashes;
3. ingest into an isolated evaluation scope;
4. run dev with a fresh run ID;
5. resume the same immutable contract for test;
6. group metrics only when dataset, material, retrieval, model and index
   compatibility keys match.

Successful questions are not repeated on resume; failed questions require an
explicit rerun policy. Reports store safe identifiers, counts and metrics, not
document text, model prompts, credentials or provider payloads.

P0.1 is closed: its 220-question synthetic run validates workflow execution and
compatibility guards, not production search quality. The pinned RRNCB reference
is document-level only; section/chunk evidence metrics require reviewed anchors
and are not inferred from missing data. Current deferred work is in
[STATUS.md](../STATUS.md).

## Failure Semantics

- Authorization failure: safe forbidden/not-found behavior without mutation.
- KB/index incompatibility: `KB_NOT_READY`.
- Required model alias failure: typed Gateway/model error and degraded readiness.
- Insufficient evidence: explicit partial/insufficient outcome, never invented
  support.
- Stale search projection: safe omission plus durable reconciliation.
- Lost worker/episode lease: no unowned state advance; resume/recovery uses
  durable checkpoint state.
