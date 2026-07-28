# ExecPlan 11 — Wikipedia ZIM evaluation system v1

## Outcome

A local operator can run a smoke evaluation against the current imported ZIM, generate a versioned private Wikipedia suite with CLI-controlled count/concurrency/model aliases/family mix, watch live progress in stdout, inspect persisted generation/retrieval status during the run, resume interrupted runs from checkpoint, compare answer and retrieval-only profiles through the real API, and export Markdown/JSON reports with retrieval, answer, citation, latency, token and cost metrics.

## Why this plan exists

`SPEC.md` requires evaluation datasets, regression gates and operational metrics. ExecPlan 10 completed the real ZIM/Kiwix/OpenRouter demo path; this plan adds the first reproducible evaluation layer without changing the working RAG pipeline.

## In scope

- CLI commands `eval-smoke`, `eval-generate`, `eval-run`, `eval-report` and `eval-full`;
- CLI command `eval-generate-status`;
- CLI commands `eval-retrieval-run`, `eval-retrieval-status` and `eval-retrieval-report`;
- JSONL task schema and artifact storage under `artifacts/eval/`;
- smoke gate for current ZIM snapshot/index/profile;
- generated `generated-wikipedia-v1` suite with requested task family distribution;
- runtime generation config for count, concurrency, generator/verifier aliases, family weights/targets and stable `run_id`;
- persisted generation status plus accepted-task checkpoint for resume;
- six-config paired eval runs;
- retrieval-only `/api/v1/search:debug` batch runner with per-task JSONL, status, ETA and resume;
- deterministic retrieval, answer, citation, URL, latency, token and cost metrics;
- unit/integration tests for metrics, gold matching, unanswerable tasks, citations and reproducibility.

## Out of scope

- new database eval tables;
- public benchmark ingestion;
- UI for evaluation reports;
- changing chunking, retrieval, reranking, answer generation or Extended Search behavior;
- GraphRAG, ColBERT, learned sparse or multi-agent evaluation.

## Preconditions

- ExecPlan 10 ZIM/Kiwix/OpenRouter demo path is complete;
- a real imported ZIM exists in PostgreSQL/OpenSearch;
- Kiwix serves the same ZIM;
- real eval commands require the Model Gateway aliases for `sota_mvp`.

## Contracts and invariants

- Evaluated answers go through `/api/v1/chat`.
- Retrieval-only runs go through `/api/v1/search:debug` and never call `/api/v1/chat`.
- Direct DB reads are limited to gold catalog construction and candidate ID enrichment.
- Dataset/run artifacts include `snapshot_id`, `index_version`, ZIM checksum and retrieval profile hash.
- Smoke must pass before generation.
- Weak ablation configs may fail quality thresholds; their failures are reported, not merged.
- `artifacts/eval/` remains uncommitted.

## Milestones

### M11.1 Eval primitives

- Add schemas, hashing, artifact IO, corpus snapshot loading and metric functions.
- Validation: `uv run pytest tests/unit/test_eval_metrics.py tests/unit/test_eval_generation.py -q`.

### M11.2 CLI and runner

- Add smoke/generate/run/report commands and six config runner.
- Validation: `uv run pytest tests/integration/test_eval_runner.py -q`.

### M11.3 Documentation and release gate

- Add evaluation contract, runtime/generation status contract, Makefile targets and status evidence.
- Validation: lint, format, mypy, full pytest and real smoke where prerequisites exist.

## Acceptance criteria

- `python -m wikipediarag.cli eval-smoke --count 10` runs first and prints per-question results.
- `eval-generate --count 150` creates exactly 150 tasks with the default family counts and required JSONL fields.
- `eval-generate --count 20 --concurrency 20 --family-weight comparison_multi_hop=1` creates only `comparison_multi_hop` tasks without silently clamping concurrency.
- `eval-generate-status --latest` reports the persisted phase, counters, active family, last update and recent accepted questions of the current or latest run.
- `eval-generate --resume-run-id <id>` resumes only when corpus identity, model aliases and family targets match the stored run status.
- Every answerable task has existing gold page, section and chunk IDs.
- `eval-run` writes separate results for all six configs.
- `eval-report --latest` writes Markdown and JSON reports.
- `eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10` writes separate retrieval-only result JSONL files, progress logs and status.
- `eval-retrieval-status --latest` reports current config, batch, task, processed count, latency and ETA.
- `eval-retrieval-report --latest` writes Markdown and JSON reports without answer/citation/token metrics.
- Re-running the same dataset/config reuses completed results.

## Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
uv run python -m wikipediarag.cli eval-smoke --count 10
uv run python -m wikipediarag.cli eval-generate --count 150
uv run python -m wikipediarag.cli eval-generate-status --latest
uv run python -m wikipediarag.cli eval-run --suite generated-wikipedia-v1
uv run python -m wikipediarag.cli eval-report --latest
uv run python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10
uv run python -m wikipediarag.cli eval-retrieval-status --latest
uv run python -m wikipediarag.cli eval-retrieval-report --latest
```

## Demo

```bash
python -m wikipediarag.cli eval-smoke --count 10
python -m wikipediarag.cli eval-generate --count 20 --concurrency 10 --family-weight comparison_multi_hop=1 --run-id demo-run
python -m wikipediarag.cli eval-generate-status --run-id demo-run
python -m wikipediarag.cli eval-generate --resume-run-id demo-run
python -m wikipediarag.cli eval-run --suite generated-wikipedia-v1
python -m wikipediarag.cli eval-report --latest
python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10
python -m wikipediarag.cli eval-retrieval-status --latest
python -m wikipediarag.cli eval-retrieval-report --latest
```

## Rollback and recovery

Remove only `artifacts/eval/` to clear generated datasets/runs/reports. Do not delete ZIM files, database volumes, OpenSearch indices or MinIO volumes. Code rollback does not require data migration because v1 adds no tables.

## Progress

- [x] 2026-07-26: Plan created and scoped for JSONL eval over current ZIM.
- [x] 2026-07-26: Eval schemas, corpus snapshot loading, metrics, runner, reports and CLI commands implemented.
- [x] 2026-07-26: Targeted unit/integration tests added and passing.
- [x] 2026-07-26 17:07 +03:00: Added typed `eval-generate` progress events, CLI stdout reporter and tests for accepted, rejected, invalid JSON, local validation rejection and provider-error paths.
- [x] 2026-07-26 17:07 +03:00: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests` and `uv run pytest tests/unit tests/integration tests/e2e -q` passed after the observability changes.
- [x] 2026-07-26 18:06 +03:00: Added typed generation runtime config, CLI flags for count/concurrency/model aliases/family weights/run IDs, persisted `status.json` + `accepted.partial.jsonl`, `eval-generate-status`, resume validation and bounded worker/semaphore generation.
- [x] 2026-07-26 18:06 +03:00: Added generator/progress/CLI tests for custom aliases, family-target normalization, persisted status output and resume-from-checkpoint.
- [x] 2026-07-26 18:06 +03:00: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests` and `uv run pytest tests/unit tests/integration tests/e2e -q` passed after the flexible runtime/checkpoint change.
- [x] 2026-07-26: Added retrieval-only batch runner over `/api/v1/search:debug`, persisted status/logs/results, resume support, retrieval reports and CLI commands.
- [x] 2026-07-26: Added unit/integration tests for search-debug candidate extraction, hard-negative metrics, retrieval-only summaries, progress formatting, config isolation, resume and reports.
- [x] 2026-07-26: Real retrieval-only run `retrieval-validation-150` completed on the 150-task `generated-wikipedia-v1` dataset: 750/750 supported task-runs, 0 API failures, 5 supported configs completed and `sota_mvp_conditional_harness` marked unsupported for `/search:debug`.
- [ ] 2026-07-26: Real count-10 smoke/generation/run/report evidence on the current OpenRouter/ZIM snapshot.

## Discoveries

- The current `index_versions.source_type` for the ZIM index is `zim`, while documents/chunks use `wikipedia_zim`; eval lookup accepts both.
- Host `.env` may contain container URLs. The eval command layer adapts them to localhost/`./zim` when running outside the container.
- Current imported ZIM has shallow section paths, so deep-section tasks use late/non-lead chunks rather than h3+ headings.
- One top-level generation attempt may contain up to two internal generator/verifier LLM retries; progress output therefore reports multiple safe rejection events before the next numbered attempt begins.
- Near-duplicate local validation intentionally ignores short numeric-only differences, so generated questions must differ in meaningful lexical tokens rather than only article numbers.
- A later manual 20-task generation can overwrite `datasets/generated-wikipedia-v1/latest.json`; the 150-task manifest can be restored from its versioned `.manifest.json` when the retrieval run must target the full suite.

## Decision log

- Evaluation v1 uses JSONL artifacts rather than new DB tables.
- `eval-generate` requires a smoke marker from at least 10 tasks.
- Reranker delta is `pre_rank - post_rank`; positive means the gold evidence moved up.
- Generator observability uses typed internal events plus a CLI-only reporter; the core generator stays decoupled from stdout and persists run status/checkpoint artifacts without publishing a partial dataset.
- Resume is checksum/profile/model-target strict: a stored run can continue only against the same corpus identity and the same resolved generation configuration.
- `sota_mvp_conditional_harness` is reported as unsupported in retrieval-only runs because `search:debug` does not execute the conditional chat harness; full harness quality stays in `eval-run`.

## Final evidence

- `uv run pytest tests/unit/test_eval_generation.py tests/unit/test_eval_progress.py -q` -> exit 0 (`12 passed`).
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`59 files already formatted`).
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 54 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`40 passed`).
- `uv run python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10 --run-id retrieval-validation-150` -> completed via persisted status: 750/750 supported task-runs, 0 failed, elapsed `01:52:47`.
- `uv run python -m wikipediarag.cli eval-retrieval-status --latest` -> exit 0, reported `state=completed`, `processed=750/750`, `failed=0`.
- `uv run python -m wikipediarag.cli eval-retrieval-report --latest` -> exit 0, wrote `artifacts/eval/retrieval-reports/retrieval-validation-150.md` and `.json`.
- `uv run python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10 --resume-run-id retrieval-validation-150` -> exit 0, reused completed rows and finished without re-running task calls.
- Real `eval-smoke` / `eval-generate` / `eval-generate-status` / `eval-run` / `eval-report` were not re-run in this change because they depend on the current OpenRouter-backed corpus state and incur provider time/cost.
