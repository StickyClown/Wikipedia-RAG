# Deep Research Runtime Failure Analysis

Snapshot date: 2026-08-04
Snapshot time: 2026-08-04T23:12:41+03:00
Status: partial real-runtime analysis; `deep-research-tool-matrix` was still running at snapshot time

## Scope

This note analyzes why the current real-provider Deep Research runtime is not
passing the hard gate and why the new runtime tool matrix is progressing slowly
and failing in multiple ways.

The goal is not to redesign the system from scratch. The goal is to identify
the concrete failure classes visible in the current code and runtime artifacts,
separate primary causes from secondary symptoms, and define a repair order.

## Inputs

Commands used:

- `uv run python -m wikipediarag.cli deep-research-hard-gate --declared-context-tokens 80000`
- `uv run python -m wikipediarag.cli deep-research-tool-matrix --declared-context-tokens 80000`

Primary artifacts:

- `artifacts/validation/deep-research-hard-gate/20260804T185155Z/report.json`
- `artifacts/validation/deep-research-hard-gate/20260804T185155Z/alias_reformulation_chain-detail.json`
- `artifacts/validation/deep-research-hard-gate/20260804T185155Z/policy_exception_bridge-detail.json`
- `artifacts/validation/deep-research-tool-matrix/20260804T190919Z/extended_search_only/*-detail.json`

Relevant implementation points:

- `src/wikipediarag/research_planner.py`
- `src/wikipediarag/deep_research.py`
- `src/wikipediarag/model_client.py`
- `src/wikipediarag/cli.py`

## Executive Summary

The current failures are driven mainly by planner-contract fragility and real
provider/runtime instability, not by the 45% context policy.

What the current evidence says:

- The `80k` profile is applied correctly. The hard gate run recorded planner
  budgets of `36000/44000/56000` productive/soft/hard input tokens.
- Actual packed context usage in the failing hard-gate run stayed tiny. The
  best measured `max_context_ratio` was only `0.011675`. This is far below any
  soft or hard input limit.
- The dominant failures are:
  - invalid planner JSON;
  - planner output schema mismatch for `tool_args`;
  - provider-side or gateway-side failures during planner calls;
  - long episode chains caused by question growth without a strong saturation
    rule;
  - hard-gate suite deadline semantics that starve later fixtures.
- The five-tool registry is partially working. In
  `search_plus_document_tools`, the runtime did execute
  `document_section_lookup`, so this is not a fake matrix that only replays
  `extended_search`.

Conclusion:

- Increasing context is not the right next fix.
- The first repair target is planner robustness and bounded progression.

## Hard Gate Results

Suite:

- command: `deep-research-hard-gate`
- fixture pack: `tests/fixtures/deep_research/research_tasks_hard.json`
- provider mode: `openrouter`
- declared context: `80000`
- result: `passed=false`
- report: `artifacts/validation/deep-research-hard-gate/20260804T185155Z/report.json`

Per-task outcome:

| Task | Result | Key facts |
| --- | --- | --- |
| `alias_reformulation_chain` | failed | `JSONDecodeError`, `0` episodes, `0` tool calls, `0` evidence, `0` derived questions |
| `policy_exception_bridge` | failed | `ValidationError`, `2` completed tool calls, `7` derived questions, `coverage_score=0.25`, `evidence_recall=0.6667` |
| `contradiction_after_bridge` | failed | `DEEP_RESEARCH_RUN_TERMINAL_TIMEOUT` |
| `finance_alias_chain` | failed | suite-level `TimeoutError` before completion |

Important implication:

- The hard gate is not failing because the stack cannot start.
- The stack starts, ingests documents, creates research runs, and executes at
  least part of the loop.
- The failure happens inside the research lifecycle itself.

## Tool Matrix Snapshot

Suite:

- command: `deep-research-tool-matrix`
- report dir: `artifacts/validation/deep-research-tool-matrix/20260804T190919Z`
- provider mode: `openrouter`
- declared context: `80000`
- status at snapshot: still running

### `extended_search_only`

This mode completed all six fixture attempts before the snapshot moved to the
next mode.

Observed outcome:

- `1` completed
- `5` failed

Per-task snapshot:

| Task | Run status | Error | Tool usage | Notes |
| --- | --- | --- | --- | --- |
| `section_alias_owner_chain` | completed | none | `3` completed `extended_search` calls | only clear success in the mode |
| `within_doc_exception_clause` | failed | `JSONDecodeError` | `2` completed `extended_search` calls | useful progress existed before failure |
| `mixed_tool_contradiction` | failed | `ValidationError` | `3` completed `extended_search` calls | partial product value existed before failure |
| `csv_budget_reconciliation` | failed | `ValidationError` | `1` completed `extended_search` call | no CSV tool usage possible in this mode |
| `metadata_effective_policy` | failed | `ValidationError` | `0` tool calls | likely failed at planner contract before useful work |
| `acl_visible_handle_only` | failed | `ValidationError` | `0` tool calls | likely failed at planner contract before useful work |

Interpretation:

- `extended_search_only` can solve at least one multi-hop chain task.
- It is structurally misaligned with fixtures that require document tools.
- The current runtime does not fail fast when the selected tool mode is too weak
  for the task.

### `search_plus_document_tools`

This mode had not completed at snapshot time, but it already produced strong
signals.

Observed state at snapshot:

- one run failed with `provider_http_500`
- one run was still active
- the active run had:
  - `5 covered` questions;
  - `1 partial` question;
  - `4` completed `extended_search` calls;
  - `1` completed `document_section_lookup` call;
  - no remaining `open` questions at the last DB check

Interpretation:

- The registry expansion is real. The runtime is capable of taking at least one
  document-tool path.
- The bottleneck moved from “tool unavailable” to “planner/provider/finalization
  instability under a richer loop”.

## Root Causes

## 1. Planner JSON handling is too brittle on the real-provider path

Evidence:

- `alias_reformulation_chain` failed with `JSONDecodeError` before any episode
  or tool call was created.
- `within_doc_exception_clause` in `extended_search_only` failed with
  `JSONDecodeError` after useful progress.
- Worker logs captured:
  `json.decoder.JSONDecodeError: Expecting ',' delimiter ...`

Code path:

- `src/wikipediarag/research_planner.py`
- `plan_research_step(...)` calls `chat_completion(...)`
- response is passed directly into `json.loads(content)`
- for `profile.requires_real_provider`, any exception is re-raised

Why this fails:

- The code assumes the provider will always return valid strict JSON because
  `response_format` was requested.
- On the real-provider path this assumption is false. The model sometimes emits
  invalid JSON even when the requested output is close to correct.
- The current `_repair_planner_payload(...)` only repairs a small logical shape
  for `finish`/`blocked`; it does not repair malformed JSON.

Why this is a primary cause:

- This failure can happen before the first tool call.
- It blocks the whole lifecycle even when retrieval, context packing and DB
  state are healthy.

Fix direction:

- Add a bounded planner-output recovery layer before hard failure:
  - extract the best JSON object span from text;
  - repair common syntax mistakes;
  - retry planner once or twice with a short “return valid JSON only” repair
    prompt;
  - emit a diagnostic event such as `planner_invalid_json`.

## 2. Planner schema and runtime validator disagree on `tool_args`

Evidence:

- Worker logs captured:
  `ValidationError: tool_args values must be scalar or a string list`
- The failing planner output included nested data under `tool_args`.
- This showed up after the tool registry became richer.

Code path:

- `planner_json_schema(...)` in `src/wikipediarag/research_planner.py`
- `ResearchPlannerOutput.validate_tool_args(...)`

Why this fails:

- The JSON schema allows:
  - `tool_args` as a generic object with `additionalProperties=True`
- The runtime validator allows only:
  - scalar values;
  - list of strings

This means the planner is allowed to produce a payload that the validator later
rejects.

Why this is a primary cause:

- It is a server-owned contract mismatch.
- The planner is not violating a single clear contract; the code defines two
  incompatible contracts.

Fix direction:

- Make planner schema and validator identical.
- Prefer per-tool typed arg schemas instead of one generic object.
- Reject impossible shapes at schema level, not only after model generation.

## 3. The system still lacks a strong no-progress saturation rule

Evidence:

- The active `search_plus_document_tools` run expanded to many derived questions.
- At one point it had `13` questions total, with `3 covered`, `1 partial` and
  many `open`.
- The run kept executing episodes long after useful evidence already existed.

Code path:

- `append_research_questions(...)` is called every episode
- `MAX_DERIVED_QUESTIONS_PER_EPISODE = 5`
- `MAX_RESEARCH_QUESTIONS + MAX_DERIVED_RESEARCH_QUESTIONS` limits total count,
  but there is no explicit consecutive-no-gain stop rule in
  `_run_research_episodes(...)`

Why this fails:

- The current loop is budget-bounded, but not meaningfully saturation-bounded.
- New derived questions are appended faster than the runtime proves or closes
  them.
- The system can keep converting one partially answered topic into a wide fanout
  of speculative subquestions.

Why this is a primary cause:

- It directly increases planner calls, provider exposure and wall-clock time.
- It turns a valid tool path into a long unstable loop.

Fix direction:

- Stop after a bounded number of consecutive episodes with:
  - no new evidence;
  - no new covered questions;
  - no new claim support.
- Tighten derived-question generation:
  - stronger duplicate normalization across languages;
  - suppress low-value restatements;
  - prefer “close current gap” over “open another branch”.

## 4. `extended_search_only` is allowed to keep trying tasks it cannot really solve

Evidence:

- In `extended_search_only`, fixtures that fundamentally expect document tools
  mostly failed.
- Some of them still accumulated questions and even coverage before failing,
  which means the runtime spent expensive model budget on the wrong strategy.

Why this fails:

- The runtime knows which tools are allowed.
- It does not have a strong mechanism for concluding:
  “this mode cannot safely progress further, stop with a bounded partial result”.

Why this matters:

- The tool matrix is supposed to compare modes, not let weak modes drift until
  they fail by accident.

Fix direction:

- Add a server-owned “mode cannot satisfy next required action” stop path.
- If planner repeatedly needs document-local evidence but only
  `extended_search` is available, finish with explicit missing coverage instead
  of continuing to branch.

## 5. Planner-critical model calls are not resilient enough

Evidence:

- `search_plus_document_tools` produced a run failure with `provider_http_500`.
- Worker logs also showed `ModelGatewayError: model gateway request failed`.

Code path:

- `src/wikipediarag/research_planner.py`
- `chat_completion(... max_provider_attempts=1)`

Why this fails:

- `model_client.py` already supports transient retries.
- The planner explicitly disables that resilience by forcing
  `max_provider_attempts=1`.
- This makes the most fragile step of the loop the least retried one.

Why this is a primary cause:

- A single transient provider hiccup can kill an otherwise productive run.
- This is especially harmful after useful evidence has already been collected.

Fix direction:

- Raise planner retries to at least `2` or `3`.
- Differentiate:
  - provider transient failure;
  - planner invalid JSON;
  - planner schema violation
- Resume from checkpoint with a short bounded replan instead of failing the
  whole run immediately.

## 6. The hard-gate suite deadline hides per-task truth

Evidence:

- `contradiction_after_bridge` failed with
  `DEEP_RESEARCH_RUN_TERMINAL_TIMEOUT`
- `finance_alias_chain` failed with suite-level `TimeoutError`

Code path:

- `src/wikipediarag/cli.py`
- `deep-research-hard-gate` sets one shared `hard_gate_deadline`
- each fixture consumes from the same remaining budget

Why this fails:

- The later fixtures are not getting a fresh runtime budget.
- A slow earlier fixture can make a later fixture fail without actually testing
  it properly.

Why this matters:

- This is acceptable for a release gate, but it is bad for diagnosis.
- It makes the report mix product failures and scheduling artifacts.

Fix direction:

- Keep the shared deadline for the gate verdict if needed.
- Also record per-fixture hard limits and distinguish:
  - fixture runtime timeout;
  - suite wall-clock exhaustion.

## 7. Context budget is not the current bottleneck

Evidence:

- `policy_exception_bridge` recorded:
  - `max_context_ratio = 0.011675`
  - `avg_context_ratio = 0.00862`
- Many failed runs never even reached the first meaningful episode.
- No current evidence shows soft-limit trimming or hard-input clipping as the
  dominant failure path.

Implication:

- Tuning `35% / 45% / 55%` now is premature.
- Enlarging the context window will not fix invalid planner JSON, bad arg
  shapes, provider 500s or question explosion.

Fix direction:

- Do not make context-ratio changes the first response to these failures.
- Stabilize planner and progression first, then rerun the policy comparison.

## 8. Useful partial work is often discarded by late-step failure semantics

Evidence:

- `within_doc_exception_clause` failed with `JSONDecodeError` but still had:
  - `3` questions in `covered/partial`;
  - `2` completed tool calls
- `policy_exception_bridge` had:
  - partial coverage;
  - non-zero evidence recall;
  - useful derived questions
  - yet the whole run status became `failed`

Why this fails:

- Any late planner or provider exception flips the run to `failed`.
- The product loses the distinction between:
  - “no useful work happened”
  - “useful work happened, but the final lifecycle could not close cleanly”

Fix direction:

- Preserve a safe partial terminal result:
  - keep validated evidence and coverage;
  - report the failure class separately;
  - allow operators to inspect partial value without calling the run a success.

## What Is Working

The current runtime is not broken in every dimension.

Confirmed positive signals:

- isolated compose runtime boots reliably;
- auth, upload and ingestion complete;
- `80k` stage budget is correctly persisted and used;
- Deep Research does execute durable episodes and checkpoints;
- `extended_search_only` solved one hard multi-hop fixture end-to-end;
- `search_plus_document_tools` successfully executed
  `document_section_lookup`;
- no current evidence suggests ACL leakage or raw provider/object-key leakage in
  the public report surfaces we inspected.

These are important because they show the architecture is viable. The failures
are concentrated in the planner/runtime control plane, not in the entire stack.

## Recommended Repair Order

## P0: fix before any more context tuning

1. Make planner schema and runtime validator identical.
2. Add bounded invalid-JSON recovery for planner output.
3. Increase planner provider retries above `1`.
4. Add explicit planner failure diagnostics:
   `invalid_json`, `invalid_schema`, `provider_http_500`, `empty_content`.

## P1: fix loop control and runtime value retention

1. Add consecutive-no-progress saturation stopping.
2. Reduce derived-question fanout and improve duplicate normalization.
3. Add a safe partial terminal outcome for runs that already collected valid
   evidence and coverage.
4. Add a mode-aware stop path when the current tool registry is insufficient for
   the next useful action.

## P2: improve evaluation and operator signal

1. Split suite wall-clock exhaustion from per-fixture timeout in hard gate.
2. Write incremental aggregate snapshots during tool matrix execution, not only
   at the end.
3. Add per-run failure taxonomy to reports so later analysis does not depend on
   raw worker logs.

## Suggested Validation After Fixes

Run in this order:

1. Deterministic tests for planner invalid JSON repair and per-tool arg schema.
2. Mock runtime smoke for the new saturation and partial-terminal behavior.
3. Real-provider rerun of:
   - `alias_reformulation_chain`
   - `within_doc_exception_clause`
   - `section_alias_owner_chain`
4. Real-provider rerun of `search_plus_document_tools` first mode-only subset
   before attempting the full three-mode matrix again.

## Bottom Line

The current Deep Research real-runtime failures are mainly contract and control
plane failures:

- planner output is not robust enough for a real provider;
- richer tool modes expose schema mismatches and loop-control weaknesses;
- provider failures are not retried aggressively enough;
- hard-gate timing semantics make diagnosis noisier than necessary.

The current evidence does not support the claim that the main problem is the
`45%` context target or the `80k` context window size. The next fixes should be
planner robustness, schema alignment, saturation control and provider retry
policy.
