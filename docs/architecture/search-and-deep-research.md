# Search And Deep Research Backend

This document is the backend and product contract for ordinary search, debug
search, chat retrieval, the current Extended Search harness and the planned
durable Deep Research capability. It describes implemented behavior first, then
calls out limits and planned work.

Active project state remains in [../STATUS.md](../STATUS.md). This document is
not an implementation plan for new retrieval algorithms.

## Purpose And Audience

Use this document when deciding whether a backend change preserves the search
contract, when explaining search behavior to product stakeholders, or when
reviewing future Deep Research work.

The document covers:

- what users and API clients can do today;
- how the backend selects, retrieves, ranks and validates evidence;
- which authorization and tenancy rules are required for every retrieval path;
- what Extended Search can do now;
- what durable Deep Research must add later before it can be treated as a
  long-running research workflow.

## Current Capabilities

| Capability | API path | Minimum KB role | Implemented behavior |
| --- | --- | --- | --- |
| Ordinary search | `POST /api/v1/search` | `VIEWER` on every requested KB | Hybrid search over published chunks with filters, pagination, highlights, facets and optional document grouping. |
| Debug search | `POST /api/v1/search:debug` | `EDITOR` on every requested KB | Runs retrieval diagnostics, persists a query run and retrieval events, and returns no generated answer. |
| Chat | `POST /api/v1/chat` | `VIEWER` on every requested KB | Streams an SSE chat run with retrieval, answer generation, citation validation, usage diagnostics and persisted query-run events. |
| Query-run retrieval | `GET /api/v1/query-runs/{query_run_id}/retrieval` | `EDITOR` for the query-run KB scope | Returns stored retrieval events and a safe query-run summary. |
| Query-run feedback | `POST /api/v1/query-runs/{query_run_id}/feedback` | `EDITOR` for the query-run KB scope | Appends sanitized user feedback as a retrieval event. |
| Query-run evaluation | `POST /api/v1/query-runs/{query_run_id}/evaluation` | `EDITOR` for the query-run KB scope | Appends sanitized evaluator scores and reason codes as a retrieval event. |

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

## Future Deep Research

Durable Deep Research is planned work. It should build on the current retrieval,
query-run observability and Extended Search foundations, but it must not be
described as implemented until it has its own durable lifecycle.

Required future capabilities:

- create a durable research run with status, owner, tenant, KB scope and
  request metadata;
- split a broad topic into typed research questions and subquestions;
- maintain typed evidence memory with source, chunk, claim, coverage and
  contradiction records;
- persist coverage records separately from transient retrieval events;
- support stop, resume and failure recovery;
- preserve document-level ACL/security trimming before using mixed-permission
  sources inside one KB;
- produce a final report only from verified and authorized evidence;
- expose progress, budgets, stop reasons and safe error states to clients;
- define eval gates for coverage, citation precision, unsupported claims,
  latency and access-control safety.

Until those capabilities exist, backend behavior should be called Extended
Search, not durable Deep Research.

## Security And Tenancy

Search and chat security rules:

- The API is the authorization boundary.
- `ActorContext` is created server-side from a session, local/demo bypass or
  test auth.
- Client-supplied tenant, user, group, filter and object-prefix authority are
  not trusted.
- Ordinary search and chat require `VIEWER` on every requested KB.
- Debug search and query-run retrieval require `EDITOR` on every query-run KB.
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
- resumable Deep Research;
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
