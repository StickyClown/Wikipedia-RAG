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
| Deep Research | `POST/GET /api/v1/research-runs` and run action paths | Run creator with `VIEWER` on every requested KB in the run scope, or `EDITOR` on the primary run KB | Creates durable local-first research runs with a server-owned scope of one to three KBs, executes bounded planner/tool episodes through worker jobs, persists evidence, verified claims, claim relations, decisions, reflections, derived questions and safe tool metadata, then returns an ACL-trimmed final report. |

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
   Page quota identity is document-scoped: `(knowledge_base_id, document_id,
   page_id)` with locator/chunk fallbacks; a candidate dropped by token budget
   never consumes a quota slot.
7. Convert selected chunks into evidence ids `S1`, `S2`, ... with scores,
   ranks, source URL, section path and safe metadata.
8. Run deterministic answerability classification.
9. Persist retrieval events when the caller path owns a query run.

OpenSearch is a derived search representation. PostgreSQL and MinIO/object
storage remain the source-of-truth and backup boundary.

## Locked Retrieval Baseline

The release-gate retrieval configuration is `sota_mvp_normal`. It inherits the
`sota_mvp` hybrid BM25+dense+RRF+rerank pipeline and keeps conditional Extended
Search. The evaluation ablation `hybrid_rerank` uses the same retrieval core
but disables Extended Search, so it is not a configuration alias for
`sota_mvp_normal` even when their top-k metrics happen to match.

The 2026-08-07 baseline is locked to dataset hash
`a01d97a88620f5650601cc0a9ffe30165bb3f984048db0ca057a5b881d6a502a` and the
`generated-wikipedia-v1` snapshot. On 150 tasks, `sota_mvp_normal` and
`hybrid_rerank` both measured page Recall@10 `0.896`, chunk Recall@20
`0.904`, MRR@10 `0.817`, nDCG@10 `0.787456`, 16 gold-miss tasks and zero
execution errors. This locks the observed result; it does not merge the
profiles or their contracts. The profile settings match the previous baseline,
but its config hash changed from
`b6508422a73bddd5ec4d3a669e4dad9fe63e9f89a1ec280333d0e8129b27041d` to
`3c8ddf7024fa92da06f7f8257fc49593fc649112851d7d4fa276e873974f672c` because
the answerability/evaluation and RunContract policies were versioned; this is
a new canonical contract baseline, not a retrieval-quality regression.

Do not repeat this full retrieval run merely to compare these two profiles.
Repeat it only after a dataset/snapshot, index or run contract, profile or
override, model alias, or retrieval/evidence-control implementation change.
The authoritative artifact is
`artifacts/eval/retrieval-reports/generated-wikipedia-v1-a01d97a88620-retrieval.md`.

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
single-KB retrieval path. Multi-KB direct and Extended Search retrieval are implemented. Multi-KB
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
- `extended`: starts with Extended Search for one or more KBs in the server-owned scope.
- `auto`: schema-supported mode that participates in the same route-decision
  logic as non-extended modes.

If more than one KB is requested and route selection would use Extended Search,
the backend forces direct retrieval with reason
The route no longer emits `multi_kb_extended_search_not_enabled_v1`; every KB
branch keeps its own ACL scope and evidence identity.

Chat limits:

- `message`: 1 to 32000 characters;
- `knowledge_base_ids`: max 50;
- `client_request_id`: max 128 characters;
- `retrieval_profile`: max 80 characters.

## Extended Search

Extended Search is implemented as a bounded harness inside the chat
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
- evaluates the same provisional final-evidence set after every step and stops
  with `evidence_sufficient` only for `ANSWERABLE` evidence without missing
  answer-bearing terms or conflicts; `PARTIAL` and `CONFLICTING` enqueue
  bounded gap-repair queries;
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

## Durable Deep Research

Durable Deep Research is implemented as a bounded local-first research
lifecycle. It deliberately reuses the existing retrieval stack instead of
adding a large multi-agent runtime.

API surface:

- `POST /api/v1/research-runs`: create a run and enqueue an
  `ingestion_jobs.kind='deep_research'` worker job. The request accepts one
  primary `knowledge_base_id` plus optional `knowledge_base_ids` up to three
  total KBs in the same tenant.
- `GET /api/v1/research-runs`: list visible runs in the active tenant.
- `GET /api/v1/research-runs/{research_run_id}`: return the current
  ACL-trimmed detail view.
- `GET /api/v1/research-runs/{research_run_id}/events`: return compact
  episodes, tool calls, decisions, claim relations, coverage and reflections.
- `POST /api/v1/research-runs/{research_run_id}:pause`: request pause at the
  next episode boundary.
- `POST /api/v1/research-runs/{research_run_id}:resume`: enqueue a fresh
  worker job for a paused or failed run.
- `POST /api/v1/research-runs/{research_run_id}:cancel`: request cancellation.

Persistent state:

- `research_runs`: owner, tenant, primary KB, profile, retrieval overrides,
  lifecycle status, checkpoint/progress, context policy, heartbeat and final
  report.
- `research_run_scopes`: ordered one-to-three KB scope snapshot for a run.
- `research_questions`: deterministic initial decomposition plus bounded
  planner-derived questions with lineage metadata.
- `research_episodes`: one resumable episode per processed question, linked to
  the `query_run_id` created for retrieval debugger compatibility.
- `research_tool_calls`: public-safe tool ledger with allowed tool name,
  normalized query hash, normalized args hash, heartbeat, result counts,
  evidence ids, timings and stop reason; raw chunks, provider payloads, prompts
  and storage keys are not stored.
- The internal `validated_args` projection is tenant/run scoped and used only
  for deterministic source routing on resume; it is excluded from public
  detail.
- `research_evidence_records`: typed evidence memory by
  `(research_run_id, chunk_id)` with a stable evidence fingerprint for
  duplicate-progress detection.
- `research_claim_records`: verified claim records linked to evidence ids with
  supported, partial, unsupported or conflicting status.
- `research_claim_relations`: durable `supports`, `contradicts` and
  `depends_on` style edges between persisted claims.
- `research_coverage_records`: durable coverage state per question.
- `research_reflections`: short operational notes that guide the next step but
  are not treated as factual evidence.
- `research_decisions`: durable strategy choices such as planner-selected tool,
  evidence gain and repair/no-progress transitions.

Execution model:

1. The API resolves the actor, active tenant and server-owned research scope
   server-side: one primary KB plus up to two additional KB ids in the same
   tenant.
2. The API validates `VIEWER` access and active retrieval contract for every
   KB in the scope, then stores a context policy derived from the Deep Research
   stage profile plus any validated per-run override.
3. The worker claims a normal PostgreSQL ingestion job and processes one open
   research question per episode.
4. Each episode packs a compact context envelope and runs a bounded planner
   step. Mock/local profiles use deterministic fallback; non-mock planner calls
   go through Model Gateway `chat_completion`.
5. Validated planner output may call only the server-owned local-private tool
   registry. Broad retrieval uses `extended_search`; when the run scope spans
   more than one KB it dispatches through the existing multi-KB retrieval
   engine. Document tools resolve only already-visible evidence handles and
   re-check current ACL before use. A deterministic controller-owned router
   ranks compatible sources by relevance and novelty, then stable evidence id;
   it never grants the planner arbitrary document/tenant/KB handles. The raw
   tool query is executed but the durable public ledger stores only normalized
   hashes and safe metadata.
6. Episode output is compacted into evidence records, coverage records,
   verified claims, claim relations, decisions and an operational reflection.
   Evidence-discovered aliases, exceptions, owners, blockers, budget/scope
   hints and contradictions can append durable `kind='derived'` questions.
7. The worker checkpoints after every episode/tool transition, updates
   heartbeat timestamps during active tool calls, marks stale calls as
   recoverable and rebuilds the public final report from visible verified
   evidence, coverage, claims and reflections.

Question controller contract:

- `execution_state` is the lifecycle (`pending`, `running`, `done`); `outcome`
  is the terminal assessment (`covered`, `partial`, `exhausted`, `failed`, or
  null). The legacy `status` is only a compatibility projection.
- The selector orders required questions first, then bridge questions, then
  normal derived questions, with stable `created_at` and `id` tie-breakers.
- Each question has bounded `max_attempts`, `max_rewrites` and `max_depth`.
  The run also keeps its existing total question/tool/episode budgets and an
  absolute deadline.
- The planner is advisory: it proposes search queries, allowed tool
  candidates and discovered questions. `finish`, `blocked`, coverage changes,
  original-query changes and deletion of required questions are controller
  responsibilities. Invalid planner output falls back to the immutable
  current question as the `extended_search` query with no derived questions.
- There is no run-wide no-progress terminal condition. A repeated evidence
  fingerprint consumes the current question's bounded attempts; that question
  then becomes `partial` when useful value exists or `exhausted`, and the
  selector moves on.
- `ToolResult` is the common tool contract. Branch errors are classified as
  `transient`, `permanent`, `security` or `controller_bug`; only transient
  errors are retried. Coverage evaluation returns an assessment, while only
  the controller writes question and run lifecycle state.
- When the deadline expires, every remaining question is terminalized as
  `exhausted` with `run_deadline_exhausted`, and a deterministic partial report
  is persisted. Its synthesis lists confirmed findings, incomplete findings,
  unresolved questions, used evidence and limitations.
- Controller and persistence diagnostics keep transaction failures visible
  without retaining raw exception text or provider payloads. Dynamic error
  columns receive at most one SQL assignment; episode claims use
  `ON CONFLICT ... DO NOTHING` followed by a scoped read; and nullable
  heartbeat lease comparisons are explicitly cast to the persisted text
  column type. A heartbeat failure is therefore a controller/persistence
  error, not a reason to silently downgrade a completed research report.

Tool and reformulation status:

- Deep Research uses a single-agent bounded planner/tool loop. It is not a
  multi-agent swarm.
- The current allowed tool registry is `extended_search`,
  `document_section_lookup`, `search_within_document`, `table_csv_lookup` and
  `metadata_lookup`.
- Planner output is strictly validated for known tool names, bounded query
  length, bounded derived questions, duplicate suppression and unsafe public
  token rejection.
- Operational reflections are advisory notes only and are never treated as
  evidence.
- Document text is packed as evidence only. Retrieved document content is never
  executable instruction text.
- When coverage becomes `conflicting`, the run creates a repair-style derived
  question before confident synthesis.

Retrieval contract versioning: `RunContract.schema_version=2` includes the
context-selection policy version and Extended Search control-policy version,
so page-identity and answerability-gate behavior changes produce a new
`run_contract_id` without changing HTTP request/response shapes.

Context strategy:

- Planner/reflection uses `generator_fast` with `80k` declared context.
- Claim verification uses `generator_main` with `24k` input and `2k` output.
- Final synthesis uses `generator_main` with `80k` declared context.
- Ordinary Search and the chat Extended Search harness keep their normal
  smaller postprocess budget and are not widened by Deep Research settings.
- `productive_target = 45%`, `soft_limit = 55%`, `hard_input_limit = 70%`,
  `output_reserve = 15%` and `safety_reserve = 15%` of the stage profile
  context.
- `POST /api/v1/research-runs` accepts `context_policy_override` for
  `productive_target`, `soft_limit` and `hard_input_limit`; the CLI smoke
  exposes matching flags.
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

### Deep Research Runtime Architecture

The implemented runtime is a bounded server-owned research loop, not a generic
agent framework.

Lifecycle:

1. The API creates a `research_run`, validates the actor's `VIEWER` access for
   every requested KB, stores the ordered one-to-three-KB scope and enqueues a
   normal PostgreSQL-backed worker job.
2. The worker claims that job, selects the next open research question, packs
   the current episode context and calls the planner through Model Gateway.
3. A validated planner suggestion selects one tool call from the local-private
   registry; the controller owns all stop and coverage decisions.
4. Tool output is compacted into durable evidence, claims, coverage, decisions
   and reflections, then the worker checkpoints the run before the next
   episode.
5. Pause, resume and cancel operate at the run lifecycle level; recovery uses
   stored run state, persisted questions, tool-call heartbeats and episode
   checkpoints rather than re-running the whole research job from scratch.

Memory model:

- `research_questions` stores the deterministic initial split plus bounded
  derived follow-up questions.
- `research_episodes` stores resumable per-question execution state and links
  each episode to a `query_run_id` for retrieval-debugger compatibility.
- `research_tool_calls` stores only public-safe tool metadata: allowed tool
  name, normalized query hash, normalized args hash, bounded counters and stop
  reason.
- `research_evidence_records`, `research_claim_records`,
  `research_claim_relations`, `research_coverage_records`,
  `research_reflections` and `research_decisions` together form the durable
  Deep Research working memory.

Tool registry:

- `extended_search` performs broad retrieval through the existing hybrid search
  stack.
- `document_section_lookup`, `search_within_document`,
  `table_csv_lookup` and `metadata_lookup` operate only on already-visible
  evidence handles and re-check current ACL before access.

Security boundary:

- The API owns tenant, actor and KB scope resolution; the browser does not
  supply trusted authority.
- Retrieved document text is evidence, not executable planner instruction.
- Public reports and durable tool ledgers do not store raw prompts, raw
  provider payloads, storage keys or raw tool queries.
- Document-level access is enforced twice: once during retrieval and again when
  document-local tools resolve an evidence handle.

Context policy:

- Planner and reflection use `generator_fast` with `80k` declared context.
- Verifier uses `generator_main` with `24k` input and `2k` output.
- Synthesis uses `generator_main` with `80k` declared context.
- The runtime default remains `45%` productive target, `55%` soft limit,
  `70%` hard input limit, `15%` output reserve and `15%` safety reserve.
- Ordinary Search and the chat Extended Search harness keep their smaller
  non-Deep-Research profile and are not widened by Deep Research settings.

### How We Diagnose Model/Provider Failures

Current evidence supports a narrower claim than "the model is bad" or "the
system is fully correct". What we can show today is that the remaining blocker
is no longer isolated-stack startup, auth, upload or ingestion.

What the existing artifacts prove:

- The isolated mock hard gate completed the same hard fixture end-to-end:
  `artifacts/validation/deep-research-hard-gate/20260804T204840Z/report.json`.
- The bounded real-provider rerun also completed isolated Compose startup, API
  readiness, platform-admin login and all fixture uploads before the research
  loop degraded:
  `artifacts/validation/deep-research-hard-gate/20260805T042043Z/report.json`.
- In that real-provider run, the long-lived failure state was
  `planner_failed` with safe code `planner_invalid_schema`, which means Model
  Gateway returned planner content that still did not satisfy the local strict
  planner contract after bounded recovery and validation.
- The terminal error `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED` is the shared
  suite wall-clock budget expiring during run wait. It is not evidence that the
  stack failed to boot or that document ingestion failed.

What we should infer from that evidence:

- The remaining blocker is at the real-provider planner structured-output
  boundary: schema conformance, latency, or both under the current
  OpenRouter-backed Qwen alias.
- The current evidence narrows the blame surface, but it does not scientifically
  prove that model quality alone is the sole cause. A stronger proof would
  require an A/B rerun against another model or provider alias with the same
  planner prompt, schema and fixture, which is outside the scope of the current
  documentation pass.

Validation on 2026-08-07 narrows the current blocker further. The deterministic
controller passed the isolated mock hard gate and a preserved real-provider
focused run reached `completed` with no `ProgrammingError`, no open required
questions, zero unsupported claims and ACL safety. The remaining real-provider
failure is evidence quality (`2/3` required markers, missing
`DR_HARD_LANTERN_RUNBOOK`), so retrieval or provider behavior must be analyzed
separately; the controller must not change retrieval ranking or tenant/KB scope
to force this marker.

Relevant OpenRouter guidance matches this diagnosis:

- Structured outputs are supported per endpoint, not only per model, and
  support can vary across providers for the same model.
- `provider.require_parameters=true` is the documented way to restrict routing
  to providers that support all requested parameters, including structured
  output parameters.
- Even with strict structured outputs, exact schema compliance is not
  guaranteed on every endpoint because enforcement varies by provider.
- `Retry-After` may be returned on transient `429` and `503` responses and
  should be honored in retry backoff.

Official references:

- [OpenRouter Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs)
- [OpenRouter Provider Routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [OpenRouter Errors And Debugging](https://openrouter.ai/docs/api_reference/errors-and-debugging)

Quality and context validation:

- `tests/fixtures/deep_research/research_tasks.json` contains compact
  synthetic complex research tasks for single facts, multi-hop bridging,
  comparison matrices, temporal updates, contradictions, insufficient evidence,
  noisy distractors, soft-limit context pressure, mixed ACL visibility and
  pause/resume/cancel lifecycle.
- `tests/fixtures/deep_research/research_tasks_hard.json` is a separate hard
  target pack for planner/tool-loop validation. Its tasks require alias
  resolution, multiple source hops, evidence-driven query reformulation,
  policy exceptions, CSV evidence and contradiction handling after a bridge.
  They also declare trajectory expectations for completed `extended_search`
  tool calls, durable derived questions and forbidden public raw-query/provider
  payload leaks. They are not part of the default runtime smoke, but can be run
  with `make deep-research-hard-gate` or the compatibility target
  `make deep-research-hard-smoke`.
- `wikipediarag.deep_research_eval` validates fixture schema and scores
  `ResearchRunDetail` payloads for coverage, expected evidence marker recall,
  unsupported claims, contradiction handling, trajectory metrics, ACL leakage,
  resume integrity and packed-context efficiency.
- `make deep-research-smoke` runs the upload-backed runtime smoke with mock
  model provider defaults, writes
  `artifacts/validation/deep-research/<timestamp>/report.json` plus JUnit XML.
- `make deep-research-hard-gate` runs the hard fixture pack with
  `upload_sota_mvp`, Docker Compose `MODEL_PROVIDER=openrouter` and Qwen aliases
  from `config/models.yaml`. This is a development/proxy simulation of the
  future fully-local model layer; application code still reaches models only
  through Model Gateway aliases. It resolves the OpenRouter secret from
  `OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_FILE` or `.env` through the shared
  settings path, injects only the resolved value into the Compose environment
  and records only the non-secret source label. Unlike the shared mock smoke,
  it creates a unique Compose project with fresh project-scoped volumes and
  loopback-only API, MinIO and PostgreSQL ports, so stale jobs in an operator's
  local stack cannot affect a hard-gate result. The hard fixtures contain only
  Markdown and CSV, so the isolated profile omits Xberg, Docling and
  metadata-service and uses local parsing/metadata fallback; production parser
  coverage remains in document-upload validation. Containers are stopped after
  each gate without deleting volumes. It defaults to a 900 second post-readiness
  deadline shared by all fixtures and lifecycle actions, and writes
  `artifacts/validation/deep-research-hard-gate/<timestamp>/report.json`, JUnit
  XML and per-task `*-detail.json` artifacts. The report contains the isolated
  project identifier and safe endpoint metadata, never the database URL or an
  OpenRouter key value. Terminal-timeout errors use fixed safe codes/messages,
  never a serialized research-run or ingestion payload. `--skip-compose`
  remains the explicit external-API mode.
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
- Isolated hard-gate mock preflight on 2026-08-02 passed one complete
  `alias_reformulation_chain` run from upload through report:
  `artifacts/validation/deep-research-hard-gate/20260802T201703Z/report.json`
  recorded all three required markers, 9 completed `extended_search` tool
  calls, 7 durable derived questions, evidence recall `1.0`, zero unsupported
  claims and ACL safety. It is a synthetic trajectory validation, not a claim
  of general real-model research quality.
- The full isolated OpenRouter/Qwen default-45% hard pack on 2026-08-02 exited
  unsuccessfully at the shared 900-second deadline. None of the four target
  tasks is therefore validated as solved by the real-model proxy path. ACL
  safety remained true and unsupported claims remained zero in evaluated
  detail, but coverage/recall were insufficient. The next task is to diagnose
  that provider/worker terminal path before changing planner behavior or
  comparing a 35% candidate.
- The 45% productive target remains the default unless measured experiment rows
  show a safe Pareto improvement with no ACL/security regression, no increase
  in unsupported claims and equal or better coverage/evidence recall.

Current Deep Research limits:

- one to three KBs in the same tenant only;
- no web browsing;
- no GraphRAG, proposition index, ColBERT or learned sparse retrieval;
- no multi-agent role swarm or self-optimizing prompt loop;
- initial question splitting and final report synthesis are deterministic;
  planner-derived questions supplement, rather than replace, the initial split;
- contrarian/counter-evidence is represented through `conflicting` coverage
  state and targeted repair questions, not as an always-on extra episode
  policy;
- offline policy experiments are deterministic context-packer checks, not a
  replacement for runtime provider/local-Qwen experiments.

The hard task pack is a set of **research targets**, not completed product
capabilities: `policy_exception_bridge`, `contradiction_after_bridge` and
`finance_alias_chain` still need a successful real-model hard-gate result.

Future work should focus on hard runtime smoke, local-Qwen validation, richer
claim verification and measured latency/coverage tradeoffs before adding
broader orchestration.

## Security And Tenancy

Search and chat security rules:

- The API is the authorization boundary.
- `ActorContext` is created server-side from a session, local/demo bypass or
  test auth.
- Client-supplied tenant, user, group, filter and object-prefix authority are
  not trusted.
- Ordinary search and chat require `VIEWER` on every requested KB.
- Debug search and query-run retrieval require `EDITOR` on every query-run KB.
- Deep Research creation requires `VIEWER` on every requested KB in the
  stored run scope. Run reads and controls are allowed for the run creator
  while they keep `VIEWER` on the primary KB, or any actor with `EDITOR`
  on the primary KB.
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
- tuned real-provider Deep Research quality gates;
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
