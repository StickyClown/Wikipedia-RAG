# ExecPlan 36: Deep Research Tool Loop And Hard Questions

## Goal

Close the gap between durable Deep Research V1 lifecycle and real multi-step
research behavior: the system must inspect evidence from one source, derive the
next bounded research question, call retrieval again, and synthesize only from
verified visible evidence.

This is not a multi-agent rewrite. The Pareto target is a bounded local-first
planner/tool loop that reuses Model Gateway, Extended Search/query-run
infrastructure, research questions, evidence records and ACL trimming.

## Status

Implemented in the 2026-08-02 planner/tool-loop V1 slice:

- strict planner output schema with `extended_search` allowlist;
- deterministic fallback planner for mock/local smoke;
- `research_tool_calls` ledger with safe query hash/result metadata;
- durable `kind='derived'` questions with lineage and duplicate suppression;
- episode-level planner -> tool -> evidence -> verified claims -> derived
  question loop;
- contradiction-first repair question creation;
- API/CLI runtime context-policy override for productive, soft and hard input
  ratios.

Implemented in the 2026-08-03 local-first SOTA follow-up:

- stage-specific Deep Research context windows through Model Gateway metadata
  and `/v1/tokenize`;
- `80k` planner/synthesis profiles, `24k` verifier profile and unchanged
  ordinary-search context budget;
- scoped Deep Research runs across one to three KBs in the same tenant;
- local-private tools `document_section_lookup`, `search_within_document`,
  `table_csv_lookup` and `metadata_lookup` in addition to `extended_search`;
- durable `research_run_scopes`, `research_decisions` and
  `research_claim_relations`;
- heartbeat and stalled-tool recovery during active episodes.

Runtime results on 2026-08-02:

- isolated mock preflight passed `alias_reformulation_chain` end-to-end with
  9 completed hashed tool calls, 7 derived questions, evidence recall `1.0`,
  zero unsupported claims and ACL safety;
- the isolated OpenRouter/Qwen default-45% full pack exited unsuccessfully at
  its shared 900-second deadline. It did not validate any of the four hard
  cases as solved by a real-model proxy.

Remaining work is diagnosis of the provider/worker terminal path, a clean
default-45% baseline on the new stage-aware runtime, optional local-Qwen
validation and measured policy tuning from real matrix results.

## Current Gap

Implemented V1 behavior:

- creates durable runs, questions, episodes, evidence, coverage, reflections
  verified claims, safe tool metadata and reports;
- performs a validated planner -> `extended_search` step once per episode and
  can append bounded, deduplicated derived questions;
- checkpoints after every episode and supports pause/resume/cancel;
- uses deterministic initial question splitting.

Remaining behavior after the V1 slice:

- the full real-model hard gate needs a clean completion after the observed
  provider/worker terminal stall;
- the five-tool registry needs measured trajectory tuning on hard tasks;
- runtime default context policy remains 45% until measured mock/local-Qwen
  evidence supports changing it.

## Hard Fixture Targets

`tests/fixtures/deep_research/research_tasks_hard.json` defines the first target
set. These fixtures are intentionally not part of the default runtime smoke, but
the implemented planner/tool-loop V1 adds a dedicated `make
deep-research-hard-smoke` target for runtime gating.

Target task families:

- `alias_reformulation_chain`: Project Lantern -> LTN-42 -> RB-17 -> Night
  Harbor -> owning team.
- `policy_exception_bridge`: case -> region/data class -> default retention ->
  regional override.
- `contradiction_after_bridge`: ticket -> component/checklist -> launch blocker.
- `finance_alias_chain`: initiative -> cost center -> CSV budget -> scope note.

## Implemented Slice

1. Add a small internal research tool registry.
2. Start with one tool: `extended_search(query, kb_id, profile, filters)`.
3. Persist public-safe tool call metadata in the dedicated
   `research_tool_calls` table.
4. Add a bounded planner step through Model Gateway before each episode:
   it receives only the current episode envelope, not full traces or raw chunks.
5. Planner output schema:
   `next_action`, `tool_name`, `tool_query`, `derived_questions`,
   `needed_evidence`, `stop_reason`.
6. Validate planner output server-side:
   known tool only, max query length, no hidden markers, same tenant/KB scope,
   bounded number of derived questions and no duplicate normalized questions.
7. Append derived questions with `kind='derived'`, source episode id and
   evidence ids in metadata.
8. Keep document text as evidence only. Never execute instructions found in a
   retrieved document.
9. Continue to checkpoint after every episode and keep pause/resume/cancel at
   episode boundaries.

## Context Rules

- Planner context uses the existing packer ratios.
- Full retrieval events, full chunks, raw provider payloads, object keys and full
  draft reports are never packed.
- If the planner context exceeds the hard input limit, do not call the model:
  emit `context_over_budget`, compact evidence abstracts and retry later.
- Derived questions are capped as a percentage of `MAX_RESEARCH_QUESTIONS`, not
  as a fixed large number.

## Validation

Fast deterministic tests:

- planner schema validation and rejection paths;
- duplicate derived-question suppression;
- tool-call metadata redaction;
- no hidden ACL marker in planner input/output/report;
- hard fixtures load and carry `tool_loop_required`/`query_reformulation`
  tags.

Runtime validation:

- run default `research_tasks.json` first to prove no lifecycle regression;
- run selected hard tasks with mock provider/planner; the isolated alias-chain
  preflight passed;
- run the full pack through OpenRouter/Qwen only after isolated startup and
  Model Gateway readiness; the recorded default-45% baseline failed its shared
  deadline and remains a diagnostic result;
- require expected markers from at least three documents for hard tasks;
- require `kind='derived'` questions for hard tasks;
- require contradiction tasks to end as `conflicting` or explicit blocked report,
  not as confident covered claim.

Acceptance:

- default smoke remains deterministic and green;
- hard smoke fails before tool-loop support and passes after it;
- no ACL/security regression;
- no direct provider calls from business logic;
- no document text is treated as executable instructions.
