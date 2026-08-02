# Search And Deep Research Backend

This document is the backend and product contract for ordinary search, debug
search, chat retrieval, the current Extended Search harness and durable Deep
Research V1. It describes implemented behavior first, then
calls out limits and planned work.

Active project state remains in [../STATUS.md](../STATUS.md). This document is
not an implementation plan for new retrieval algorithms.

## Purpose And Audience

Use this document when deciding whether a backend change preserves the search
contract, when explaining search behavior to product stakeholders, or when
reviewing Deep Research changes.

The document covers:

- what users and API clients can do today;
- how the backend selects, retrieves, ranks and validates evidence;
- which authorization and tenancy rules are required for every retrieval path;
- what Extended Search can do now;
- what durable Deep Research V1 does now and what remains outside its scope.

## Current Capabilities

| Capability | API path | Minimum KB role | Implemented behavior |
| --- | --- | --- | --- |
| Ordinary search | `POST /api/v1/search` | `VIEWER` on every requested KB | Hybrid search over published chunks with filters, pagination, highlights, facets and optional document grouping. |
| Debug search | `POST /api/v1/search:debug` | `EDITOR` on every requested KB | Runs retrieval diagnostics, persists a query run and retrieval events, and returns no generated answer. |
| Chat | `POST /api/v1/chat` | `VIEWER` on every requested KB | Streams an SSE chat run with retrieval, answer generation, citation validation, usage diagnostics and persisted query-run events. |
| Query-run retrieval | `GET /api/v1/query-runs/{query_run_id}/retrieval` | `EDITOR` for the query-run KB scope | Returns stored retrieval events and a safe query-run summary. |
| Query-run feedback | `POST /api/v1/query-runs/{query_run_id}/feedback` | `EDITOR` for the query-run KB scope | Appends sanitized user feedback as a retrieval event. |
| Query-run evaluation | `POST /api/v1/query-runs/{query_run_id}/evaluation` | `EDITOR` for the query-run KB scope | Appends sanitized evaluator scores and reason codes as a retrieval event. |
| Deep Research V1 | `POST/GET /api/v1/research-runs` and run action paths | Run creator with `VIEWER`, or `EDITOR` on the run KB | Creates durable single-KB research runs, executes bounded episodes through worker jobs, persists typed evidence/coverage/reflection records and returns an ACL-trimmed final report. |

All retrieval paths are tenant-scoped by the API. The browser never supplies
trusted `tenant_id`; the API resolves it from `ActorContext`.

## Retrieval Pipeline

Retrieval starts from a server-owned KB scope. If the request does not include
`knowledge_base_ids`, the API uses the configured default KB id.

Before search begins, the backend validates:

- the caller has the required KB role for every requested KB;
- each KB exists in the active tenant;
- each KB has an active read alias and registered index version;
- chat and debug retrieval use a compatible active retrieval contract for the
  requested retrieval profile;
- the active index source type, embedding alias and embedding dimensions match
  the profile and Model Gateway registry.

The core retrieval flow is:

1. Normalize whitespace in the user query.
2. Resolve the retrieval profile from `config/retrieval.yaml` plus request
   overrides.
3. Run first-stage retrieval:
   - BM25 against OpenSearch when enabled by the profile.
   - Dense retrieval when enabled by the profile. Query embeddings are produced
     through the Model Gateway embedding alias.
4. Fuse first-stage candidates with Reciprocal Rank Fusion when configured and
   both first-stage retrievers are active. Otherwise merge candidates without
   RRF.
5. Apply rerank through the Model Gateway rerank alias when enabled.
6. Postprocess candidates with profile policy: deduplication, page quota,
   optional parent expansion, context packing and final evidence bounds.
7. Convert selected chunks into evidence ids `S1`, `S2`, ... with scores,
   ranks, source URL, section path and safe metadata.
8. Run deterministic answerability classification.
9. Persist retrieval events when the caller path owns a query run.

OpenSearch is a derived search representation. PostgreSQL and MinIO/object
storage remain the source-of-truth and backup boundary.

## Ordinary Search

`POST /api/v1/search` is the public viewer-scoped search endpoint. It uses the
same hybrid retrieval machinery as chat, but returns search results instead of
a generated answer.

Implemented behavior:

- supports one or many KB ids;
- requires `VIEWER` on every requested KB;
- checks every requested KB has an active registered index before retrieval;
- runs hybrid retrieval with an internal search-sized override so the result
  window is large enough for filtering, grouping and pagination;
- infers `upload_sota_mvp` or `upload_mock` for homogeneous upload scopes when
  clients omit `ranking_profile` and active upload index metadata is compatible;
- applies OpenSearch-side filter payloads where supported and repeats safe
  request-level filtering on returned evidence;
- supports simple filters for document type, language, date range, source,
  source kind and source id;
- supports filter expressions with `eq`, `contains`, `in`, `gte` and `lte`;
- returns snippets, optional highlights, scores, ranks, source metadata,
  locators and section paths;
- optionally returns facets for `source_type`, `document_type`, `language` and
  `knowledge_base_id`;
- optionally groups hits by document and exposes the best document score plus
  up to three hits per group;
- supports offset and opaque cursor pagination.

Search does not create a query run and does not persist retrieval events. It is
for user-facing document discovery, not retrieval debugging.

Current search limits:

- `query`: 1 to 32000 characters;
- `knowledge_base_ids`: max 50;
- `limit`: 1 to 50;
- request `offset`: 0 to 500;
- internal search window: max 250;
- `cursor`: max 512 characters;
- `ranking_profile`: max 80 characters;
- `filter_expressions`: max 32.

## Debug Search

`POST /api/v1/search:debug` is an editor-only diagnostic path.

Implemented behavior:

- accepts `message` or `query`;
- supports one or many KB ids;
- requires `EDITOR` on every requested KB;
- always selects the direct retrieval route with reason
  `search_debug_endpoint`;
- creates and completes a query run;
- persists path-selection and retrieval-stage events;
- returns the retrieval result, search plan, root-cause artifact,
  `answer_artifact` and `query_run_id`;
- does not generate an answer.

Debug search uses `retrieve_multi`; a single-KB request falls through to the
single-KB retrieval path. Multi-KB direct retrieval is implemented. Multi-KB
Extended Search is not implemented.

Debug search limits:

- `message` / `query`: 1 to 32000 characters;
- `knowledge_base_ids`: max 50;
- `top_k`: 1 to 50;
- `retrieval_profile`: max 80 characters.

## Chat

`POST /api/v1/chat` streams an SSE response. It is the main answer-generation
path.

Implemented behavior:

- requires an authenticated actor, CSRF for unsafe cookie-authenticated
  requests and `VIEWER` on every requested KB;
- resolves an active retrieval profile from request `retrieval_profile`,
  `retrieval_overrides` and settings;
- validates active retrieval contracts for every KB before the SSE retrieval
  work starts;
- creates a persisted query run before streaming;
- emits `run.started` with a safe search plan;
- persists `path_selected`;
- runs direct retrieval, Extended Search first, or Extended Search repair based
  on mode, profile policy, classifier signal and answerability;
- runs answer generation through Model Gateway aliases;
- emits `message.delta` with answer text and evidence;
- emits `usage.updated` with retrieval, citation validation, timings, search
  plan, root cause and answer artifact;
- persists answer-generation, citation-validation, optional claim-verification
  and query-complete events;
- marks the query run completed or failed;
- emits `run.completed` or `run.failed`.

Supported chat modes:

- `normal`: direct route unless profile policy and query classifier select
  Extended Search, or direct retrieval later needs Extended Search repair.
- `extended`: starts with Extended Search when the KB scope is single-KB.
- `auto`: schema-supported mode that participates in the same route-decision
  logic as non-extended modes.

If more than one KB is requested and route selection would use Extended Search,
the backend forces direct retrieval with reason
`multi_kb_extended_search_not_enabled_v1`.

Chat limits:

- `message`: 1 to 32000 characters;
- `knowledge_base_ids`: max 50;
- `client_request_id`: max 128 characters;
- `retrieval_profile`: max 80 characters.

## Extended Search

Extended Search is implemented as a bounded single-KB harness inside the chat
retrieval flow. It is not a durable Deep Research run lifecycle.

Extended Search can start in two ways:

- first path: chat mode/profile/query classifier selects `extended_first`;
- repair path: direct retrieval returns `PARTIAL` or `UNANSWERABLE` and the
  profile allows `always` or `conditional` Extended Search.

Implemented behavior:

- builds bounded subqueries from the original question;
- adds bridge queries for selected multi-hop patterns;
- tracks query transforms with `transform_id`, `subquery_id`, query role and
  query hash;
- runs normal retrieval for each subquery against one primary KB;
- deduplicates evidence by chunk id;
- expands neighboring chunks around top evidence;
- tracks visited pages, duplicate tool calls, coverage inventory and evidence
  ledger entries;
- selects final evidence from retrieved steps and renumbers it;
- recomputes answerability over final evidence;
- persists retrieval events and a completed `agent_runs` row with ledger state.

Harness budgets currently include:

- `max_steps`: 8;
- `max_subqueries`: 6;
- `max_rewrites_per_subquery`: 2;
- `max_parallel_tool_calls`: 4;
- `max_unique_documents`: 20;
- `max_total_retrieved_chunks`: 300;
- `max_context_tokens`: from profile postprocess policy;
- `max_wall_time_seconds`: 90.

Stop reasons:

- `evidence_sufficient`;
- `budget_reached`;
- `duplicate_tool_call`;
- `no_new_evidence`;
- `coverage_stalled`;
- `conflict_unresolved`.

What Extended Search cannot do today:

- run across multiple KBs;
- pause, resume or continue after process failure;
- maintain durable typed evidence memory across runs;
- expose a managed long-running research job API;
- create durable coverage records outside the current `agent_runs` ledger;
- guarantee complete coverage for broad research topics;
- replace document-level authorization controls.

## Durable Deep Research V1

Durable Deep Research V1 is implemented as a bounded single-KB research
lifecycle. It deliberately reuses the existing retrieval stack instead of
adding a large multi-agent runtime.

API surface:

- `POST /api/v1/research-runs`: create a run and enqueue an
  `ingestion_jobs.kind='deep_research'` worker job.
- `GET /api/v1/research-runs`: list visible runs in the active tenant.
- `GET /api/v1/research-runs/{research_run_id}`: return the current
  ACL-trimmed detail view.
- `GET /api/v1/research-runs/{research_run_id}/events`: return compact
  episodes, coverage and reflections.
- `POST /api/v1/research-runs/{research_run_id}:pause`: request pause at the
  next episode boundary.
- `POST /api/v1/research-runs/{research_run_id}:resume`: enqueue a fresh
  worker job for a paused or failed run.
- `POST /api/v1/research-runs/{research_run_id}:cancel`: request cancellation.

Persistent state:

- `research_runs`: owner, tenant, single KB, profile, retrieval overrides,
  lifecycle status, checkpoint/progress, context policy and final report.
- `research_questions`: deterministic bounded question decomposition.
- `research_episodes`: one resumable episode per processed question, linked to
  the `query_run_id` created for retrieval debugger compatibility.
- `research_evidence_records`: deduplicated typed evidence memory by
  `(research_run_id, chunk_id)`.
- `research_claim_records`: minimal deterministic claim records linked to
  evidence ids.
- `research_coverage_records`: durable coverage state per question.
- `research_reflections`: short operational notes that guide the next step but
  are not treated as factual evidence.

Execution model:

1. The API resolves the actor, active tenant and single KB scope server-side.
2. The API validates `VIEWER` access and active retrieval contract, then stores
   a context policy derived from the retrieval profile's declared
   `max_context_tokens`.
3. The worker claims a normal PostgreSQL ingestion job and processes one open
   research question per episode.
4. Each episode creates a `query_runs` row with mode `deep_research`, invokes
   Extended Search with the run profile/overrides and persists the linked
   retrieval events.
5. Episode output is compacted into evidence records, coverage records, a
   minimal claim and an operational reflection.
6. The worker checkpoints after every episode and rebuilds the public final
   report from visible evidence, coverage, claims and reflections.

Context strategy:

- `productive_target = 45%`, `soft_limit = 55%`,
  `hard_input_limit = 70%`, `output_reserve = 15%` and
  `safety_reserve = 15%` of the declared profile context.
- The packer builds only a current episode envelope: immutable rules, current
  question, compact run progress, selected coverage gaps, compact evidence
  abstracts and the latest operational reflection.
- Full retrieval traces, retrieval event payloads, raw document chunks and
  draft reports are not packed into the model context.
- If the envelope exceeds the soft limit, older reflections are removed first,
  then lower-value evidence abstracts are trimmed.
- If the envelope remains above the hard input limit, the episode records a
  compact over-budget signal and later work must compact or narrow evidence
  breadth before generation-heavy use.

Quality and context validation:

- `tests/fixtures/deep_research/research_tasks.json` contains compact
  synthetic complex research tasks for single facts, multi-hop bridging,
  comparison matrices, temporal updates, contradictions, insufficient evidence,
  noisy distractors, soft-limit context pressure, mixed ACL visibility and
  pause/resume/cancel lifecycle.
- `wikipediarag.deep_research_eval` validates fixture schema and scores
  `ResearchRunDetail` payloads for coverage, expected evidence marker recall,
  unsupported claims, contradiction handling, ACL leakage, resume integrity and
  packed-context efficiency.
- `make deep-research-smoke` runs the upload-backed runtime smoke with mock
  model provider defaults, writes
  `artifacts/validation/deep-research/<timestamp>/report.json` plus JUnit XML.
- `make deep-research-matrix` runs the offline context-packer policy matrix for
  target ratios `35%`, `45%` and `55%` across evidence packing and reflection
  modes, then writes
  `artifacts/validation/deep-research-matrix/<timestamp>/report.json` plus
  JUnit XML.
- Latest measured local validation on 2026-08-02:
  `artifacts/validation/deep-research/20260802T152743Z/report.json` passed
  10/10 runtime fixtures; `artifacts/validation/deep-research-matrix/20260802T153246Z/report.json`
  passed 27 policy aggregates / 270 fixture-policy rows and ranked
  `target_35_abstracts_only_none` first in the offline packer sweep.
- The 45% productive target remains the default unless measured experiment rows
  show a safe Pareto improvement with no ACL/security regression, no increase
  in unsupported claims and equal or better coverage/evidence recall.

Current Deep Research V1 limits:

- single KB only;
- no web browsing;
- no GraphRAG, proposition index, ColBERT or learned sparse retrieval;
- no multi-agent role swarm or self-optimizing prompt loop;
- deterministic question splitting, evidence abstracts, claims and final
  report synthesis;
- contrarian/counter-evidence is represented through `conflicting` coverage
  state, not as an always-on extra episode policy;
- offline policy experiments are deterministic context-packer checks, not a
  replacement for runtime provider/local-Qwen experiments.

Future work should focus on runtime policy overrides, local-Qwen validation,
richer claim verification and measured latency/coverage tradeoffs before
adding broader orchestration.

## Security And Tenancy

Search and chat security rules:

- The API is the authorization boundary.
- `ActorContext` is created server-side from a session, local/demo bypass or
  test auth.
- Client-supplied tenant, user, group, filter and object-prefix authority are
  not trusted.
- Ordinary search and chat require `VIEWER` on every requested KB.
- Debug search and query-run retrieval require `EDITOR` on every query-run KB.
- Deep Research creation requires `VIEWER` on the selected KB. Run reads and
  controls are allowed for the run creator while they keep `VIEWER`, or any
  actor with `EDITOR` on the run KB.
- Tenant and KB filters are applied server-side for BM25, dense retrieval,
  delete and debug paths.
- Unsafe cookie-authenticated requests require CSRF unless running in explicit
  local/test bypass modes.
- Model calls go through Model Gateway aliases. Business code must not call
  providers directly.
- Safe errors must not expose secrets, provider payloads, object keys, raw
  parser stderr or cross-tenant data.

Current authorization starts at the KB level and adds minimal document-level
security trimming for trusted `metadata.document_access`. Documents are
KB-visible by default, can be tenant-visible for all active-tenant actors, or
restricted to explicit user/group allowlists. Restricted documents are trimmed
from search, chat, debug retrieval, Extended Search neighbor expansion and
document viewer paths unless the actor has an admin/manager bypass or matches
the allowlist.

## Business Requirements

The current backend is expected to satisfy these requirements:

- return only published, ready chunks;
- keep tenant and KB isolation on every retrieval path;
- use a Model Gateway contract for embedding, rerank, generation and verifier
  calls;
- generate grounded answers from selected evidence;
- require citations by default in configured answer profiles;
- validate citations deterministically unless profile policy disables it;
- persist enough query-run events for debugging and evaluation;
- report KB readiness failures through the safe `KB_NOT_READY` contract;
- avoid normal logs or public diagnostics that leak secrets, raw provider
  payloads, storage object keys or unsafe cross-tenant probe data;
- keep OpenSearch rebuildable rather than authoritative.

The backend is not expected to satisfy these requirements yet:

- source-specific external ACL engines beyond the minimal trusted
  `document_access` metadata contract;
- Multi-KB Extended Search;
- Multi-KB Deep Research and tuned research quality gates;
- production external deployment guarantees;
- full dependency readiness checks for Redis/Valkey, MinIO and OpenSearch from
  API `/ready`;
- malware scanning or production restore drills.

## Validation Expectations

Docs-only changes to this contract do not require the full backend, integration
or UI suite. Any code change to retrieval, authorization, public schemas,
query-run persistence, citation validation or profile contracts should include
deterministic tests for:

- success and important failure paths;
- `KB_NOT_READY` readiness failures;
- cross-tenant and cross-KB isolation;
- role differences between viewer and editor paths;
- citation and answerability behavior;
- Multi-KB direct retrieval behavior;
- safe diagnostic payloads.
