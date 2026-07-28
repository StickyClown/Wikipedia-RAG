# ExecPlan 13 - RAG stage timings and eval concurrency

## Outcome

An operator can clearly see how much time is spent in each normal retrieval, answer generation and Extended Search stage, and retrieval evaluation batch execution uses a bounded in-flight scheduler where a completed request immediately frees a slot for the next task.

## Why this plan exists

`SPEC.md` and `docs/quality/EVALUATION_PLAN.md` require per-stage operational latency metrics. ExecPlan 12 produced trusted evaluation datasets and retrieval pooling, but retrieval-only batching still runs tasks sequentially inside each batch and reports mostly total/context latency. This plan adds additive timing metadata and makes `--batch-size` a true concurrency limit for retrieval pooling.

## In scope

- Add per-stage timing payloads to retrieval events and debug output.
- Add answer generation timing payloads to chat usage metadata.
- Treat existing Extended Search as the current deep research mode and time its tool calls and total run.
- Persist combined chat timing summaries in `query_runs.usage`.
- Propagate timing keys into eval result `latency_ms` and config metrics.
- Make retrieval-only eval scheduling maintain a bounded in-flight request set.
- Add deterministic tests for timing payloads and bounded scheduler behavior.

## Out of scope

- New database tables or migrations.
- New public deep research mode separate from Extended Search.
- OpenTelemetry exporter changes.
- Load-testing with real provider traffic.
- Changing retrieval ranking, chunking, prompts or model aliases.

## Preconditions

- ExecPlan 12 is complete.
- Existing `/api/v1/chat`, `/api/v1/search:debug`, `eval-run`, `eval-retrieval-run` and `eval-trusted-pool` are available.
- Timing payloads remain additive JSON fields for compatibility.

## Contracts and invariants

- Existing `context.latency_ms` remains present for current consumers.
- Timing metadata never includes prompts, raw provider bodies, document text, secrets or exception details.
- `--batch-size N` in retrieval-only evaluation means at most `N` in-flight API calls for one supported config.
- Completed/resumed task rows are still skipped unless `--rerun-failed` requests failed retries.
- Existing result JSONL remains readable because new timing keys are stored in existing `latency_ms` dictionaries.

## Milestones

### M13.1 Runtime timing payloads

- Add retrieval, generation and Extended Search timing summaries.
- Validation: targeted unit tests for `retrieval.py`, `answering.py` and `extended.py`.

### M13.2 Evaluation propagation and scheduler

- Copy timing keys from API payloads into eval results and summaries.
- Replace sequential retrieval batch loop with bounded in-flight scheduling.
- Validation: targeted eval runner/report tests.

### M13.3 Documentation and final validation

- Update contracts/status with exact commands and results.
- Run lint, format check, typecheck and full tests.

## Acceptance criteria

- `/api/v1/search:debug` returns retrieval events with per-stage `latency_ms` and a final `timings` event.
- `/api/v1/chat` `usage.updated.data.timings_ms` includes retrieval and generation timing keys; Extended Search runs also include deep research/extended timing keys.
- `query_runs.usage` stores the same combined timing summary.
- Eval task results include observed timing keys in `latency_ms`.
- Eval summaries include p50/p95 metrics for observed stage timing keys.
- Retrieval-only eval with `--batch-size 10` never has more than 10 in-flight requests and starts a replacement task as soon as one completes.
- Regression tests cover generator/trusted generator bounded backfill behavior.

## Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## Demo

```bash
python -m wikipediarag.cli eval-retrieval-run --suite trusted-wikipedia-v2 --batch-size 10 --run-id timing-demo
python -m wikipediarag.cli eval-retrieval-report --latest
python -m wikipediarag.cli eval-retrieval-status --run-id timing-demo
```

## Rollback and recovery

Revert code changes and remove only newly created eval run artifacts under `artifacts/eval/retrieval-runs/*/timing-demo` and related reports. Do not delete ZIM files, database volumes, OpenSearch indices, MinIO volumes or existing dataset artifacts.

## Progress

- [x] 2026-07-27: Plan accepted by user for implementation.
- [x] 2026-07-27: Runtime timing payloads implemented for retrieval, generation and Extended Search.
- [x] 2026-07-27: Evaluation scheduler and reporting implemented with bounded in-flight retrieval pooling.
- [x] 2026-07-27: Targeted and full validation completed.

## Discoveries

- Existing generation and trusted generation already use a bounded pending-task backfill loop, but lacked explicit regression coverage for slot replacement behavior.
- Retrieval-only evaluation previously interpreted `batch_size` as a sequential grouping size, not a true in-flight request limit.

## Decision log

- Deep research is represented by the existing `Extended Search` harness for this plan; no new API mode is added.
- Timing fields are additive JSON payloads only, avoiding a migration in this milestone.

## Final evidence

- `uv run pytest tests/unit/test_retrieval_answering.py tests/unit/test_extended.py tests/unit/test_eval_retrieval_runner.py tests/unit/test_eval_generation.py::test_generate_family_refills_slot_when_attempt_completes tests/unit/test_eval_trusted.py::test_trusted_generate_family_refills_slot_when_attempt_completes tests/integration/test_eval_retrieval_runner_integration.py tests/integration/test_eval_runner.py -q` -> exit 0 (`18 passed`).
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`61 files already formatted`).
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 56 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`58 passed`).
