# ExecPlan 12 — Trusted Wikipedia dataset v2

## Outcome

An operator can generate a resumable parser-aware `trusted-wikipedia-v2` train dataset from the current local ZIM/parser output, inspect live run status and event logs, resume interrupted generation from `accepted.partial.jsonl`, run retrieval candidate pooling through the existing `/api/v1/search:debug` evaluation runner, and export a coverage report.

## Why this plan exists

ExecPlan 11 created synthetic JSONL evaluation over the imported ZIM. `SPEC.md` and `docs/quality/EVALUATION_PLAN.md` require stronger local datasets tied to exact evidence, parser output and provenance before retrieval optimization. This plan adds that dataset layer without changing retrieval, chunking, answer generation or database schema.

## In scope

- Dataset namespace `trusted-wikipedia-v2`.
- Train-only, unreviewed JSONL records.
- Parser-aware task fields for source spans, structural element, answer type, provenance, local verification results and negative candidates.
- Incremental run artifacts: `status.json`, `events.jsonl`, `accepted.partial.jsonl` and `rejected.jsonl`.
- Live trusted generation stdout progress with safe event metadata.
- Strict resume checks for corpus identity, model aliases, count, rejection budget and family targets.
- Whole-run trusted rejection budget and run lock protection.
- CLI commands for catalog, generate, status, pooling, report and optional MIRACL-RU mapping.

## Out of scope

- Human review, dev split and locked test.
- LLM-as-judge release gates.
- New database evaluation tables.
- Retrieval, reranker, chunking, ingestion or answer-generation changes.
- Direct HotpotQA import.

## Preconditions

- ExecPlan 11 evaluation subsystem exists.
- A local ZIM import/index exists for real runs.
- Model aliases resolve through the Model Gateway registry; mock aliases are valid for tests.

## Contracts and invariants

- All generated tasks use `split=train` and `review_status=unreviewed`.
- Gold evidence uses source spans and structural provenance; chunk IDs are derived links.
- Final dataset JSONL is published only after local validation.
- Partial accepted tasks are operational checkpoints, not published datasets.
- Pooling uses the existing retrieval-only runner and `/api/v1/search:debug`.
- No prompts, raw provider payloads or secrets are written to status or event logs.

## Milestones

### M12.1 Trusted schemas and artifacts

- Add v2 task/catalog/status models and atomic artifact helpers.
- Validation: `uv run pytest tests/unit/test_eval_trusted.py -q`.

### M12.2 Resumable generator and CLI

- Add catalog/generate/status/pool/report/MIRACL CLI commands.
- Validate partial writes and resume without duplicate tasks.
- Validation: targeted eval unit/integration tests.

### M12.3 Documentation and status

- Update evaluation contract and project status.
- Run lint, format, typecheck and relevant tests.

## Acceptance criteria

- `eval-trusted-catalog` writes a catalog JSONL and manifest.
- `eval-trusted-generate --count N --run-id X` writes status, events, rejected rows and accepted partial rows incrementally.
- `eval-trusted-generate --count N --run-id X` prints live progress to stdout with elapsed time, family progress, total accepted target, rejected budget, attempts and safe reasons.
- Final trusted dataset JSONL is published only when exactly `N` tasks are accepted; rejected candidates do not count toward `N`.
- `--rejection-budget` defaults to 30, is enforced across resume, and budget exhaustion leaves a checkpointed failed run without publishing a partial final dataset.
- A second active generate/resume for the same trusted run is blocked by a PID lock; stale lock takeover requires `--takeover-stale-run`.
- `eval-trusted-status --run-id X` reports phase, counters, active family and model aliases.
- `eval-trusted-generate --resume-run-id X` reuses accepted partial tasks and finishes without duplicate final task IDs.
- Final tasks include `source_spans`, `structural_element`, `answer_type`, `verification_results`, `negative_candidates`, `provenance`, `split=train` and `review_status=unreviewed`.
- `eval-trusted-pool` can run the existing retrieval-only evaluator against `trusted-wikipedia-v2`.
- `eval-trusted-report` writes Markdown and JSON coverage reports.

## Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## Demo

```bash
python -m wikipediarag.cli eval-trusted-catalog
python -m wikipediarag.cli eval-trusted-generate --count 20 --generator-alias mock_generator_main --verifier-alias mock_verifier --run-id trusted-demo
python -m wikipediarag.cli eval-trusted-status --run-id trusted-demo
python -m wikipediarag.cli eval-trusted-report --suite trusted-wikipedia-v2
```

## Rollback and recovery

Remove only `artifacts/eval/trusted-*`, `artifacts/eval/datasets/trusted-wikipedia-v2`, `artifacts/eval/retrieval-runs/trusted-wikipedia-v2` and related trusted reports to clear this plan's generated artifacts. Do not delete ZIM files, database volumes, OpenSearch indices, MinIO volumes or existing `generated-wikipedia-v1` artifacts.

## Progress

- [x] 2026-07-26: Plan accepted by user as train-only/unreviewed dataset generation.
- [x] 2026-07-26: Trusted schemas, resumable artifact writes, CLI commands and focused tests implemented.
- [x] 2026-07-26 23:00 +03:00: Ruff, format-check, mypy and full unit/integration/e2e pytest validation passed.
- [x] 2026-07-26 23:27 +03:00: Real OpenRouter-backed run `trusted-v2-test-50-clean` completed with 50 parser-aware train/unreviewed tasks and a published manifest.
- [x] 2026-07-26 23:47 +03:00: Follow-up plan implemented for trusted live stdout progress, whole-run rejection budget, provider retry reporting, run lock protection and production hard-negative Model Gateway generation.
- [x] 2026-07-26 23:57 +03:00: Real OpenRouter-backed reliability run `trusted-v2-test-50-reliable` completed with exactly 50 accepted tasks, 1 rejection within the 30-run budget and a published manifest.
- [x] 2026-07-26 23:59 +03:00: Trusted resume without `--count` now reuses the checkpoint count; full validation passed after the fix.
- [x] 2026-07-27 07:48 +03:00: Real OpenRouter-backed `trusted-v3-seed-300` generation completed with exactly 300 accepted train/unreviewed tasks, 5 rejections within the 30-run budget and filename-only `trusted-wikipedia-v3-*` copies.
- [x] 2026-07-27 13:22 +03:00: Full retrieval pooling for `trusted-v3-pool-300` completed over 1,500 supported task-runs after `--rerun-failed`; final persisted status has 0 failed API runs and reports were exported.

## Discoveries

- Existing retrieval-only runner can score `trusted-wikipedia-v2` because v2 rows keep the base `EvalTask` fields and add extra parser-aware fields.
- The current ZIM parser does not persist original HTML tag names per chunk, so structural type v2 starts with deterministic heuristics over section path, title and text.
- Windows file paths cannot use colon-delimited index versions; catalog filenames therefore use a stable hash of snapshot and index identity.
- Windows readers can temporarily lock `status.json` during atomic replacement; the shared artifact writer retries short `PermissionError` conflicts before surfacing a failure.
- A hard-negative pair has one gold source span and one disjoint local distractor; both pair members must not be emitted as gold evidence.
- On Windows, `os.kill(pid, 0)` can terminate the current Python process instead of acting as a harmless liveness probe; trusted run locks use WinAPI `OpenProcess` on Windows.
- Full 300-task retrieval pooling is strongly provider-latency-bound in the OpenRouter phase: the first full pass took `05:28:16`, with dense/rerank requests occasionally above 60 seconds but checkpointing remained usable.

## Decision log

- v2 keeps all records in `train` with `review_status=unreviewed`; human review and locked splits are future work.
- LLM-as-judge is not part of generation v2; `verification_results` are local deterministic checks.
- Optional MIRACL-RU mapping from this plan was superseded by ExecPlan 16: external rows are candidate-only and use `EXACT`/`REDIRECT`/`AMBIGUOUS`/`MISSING` binding statuses plus `AUTO_ACCEPT`/`REVIEW`/`REJECT` decisions.
- Production trusted candidates, including hard-negative tasks, are generated through the configured Model Gateway generator alias; deterministic candidates are restricted to explicit mock aliases for tests/local demos.
- Hard-negative validation keeps only E1 as gold evidence and stores E2 solely as a disjoint `negative_candidates` distractor.
- The user-requested `v3` output is filename-only branding for the generated artifacts; the dataset namespace, schema and manifest contents remain `trusted-wikipedia-v2` compatible.

## Final evidence

- `uv run pytest tests/unit/test_eval_trusted.py tests/unit/test_eval_generation.py tests/unit/test_eval_retrieval_runner.py -q` -> exit 0 (`14 passed`).
- `uv run pytest tests/unit/test_eval_trusted.py tests/unit/test_eval_generation.py tests/integration/test_eval_runner.py -q` -> exit 0 (`11 passed`).
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`61 files already formatted`).
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 56 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`44 passed`).
- `uv run pytest tests/unit/test_eval_trusted.py -q` -> exit 0 (`7 passed`) after Windows atomic-write and hard-negative regression coverage.
- `uv run ruff check src/wikipediarag/eval/artifacts.py src/wikipediarag/eval/trusted.py tests/unit/test_eval_trusted.py` -> exit 0.
- `uv run mypy src/wikipediarag/eval/artifacts.py src/wikipediarag/eval/trusted.py tests/unit/test_eval_trusted.py` -> exit 0.
- `uv run python -m wikipediarag.cli eval-trusted-catalog` -> exit 0; catalog contains 6,000 local parser-aware items for snapshot `5e698f31-09c0-0346-b23f-8a943b6646ea`.
- `uv run python -m wikipediarag.cli eval-trusted-generate --count 50 --resume-run-id trusted-v2-test-50-clean` -> exit 0; final manifest has 50 tasks and dataset hash `073d5f8dee9bdefb0f2d3c460bc26445000496051e45ce60e71f27c2287bc9bd`.
- `uv run python -m wikipediarag.cli eval-trusted-report --suite trusted-wikipedia-v2` -> exit 0; JSON and Markdown coverage reports written.
- `uv run ruff check .` -> exit 0; `uv run ruff format --check .` -> exit 0 (`61 files already formatted`); `uv run mypy src tests` -> exit 0 (`56 source files`); `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`47 passed`).
- `uv run pytest tests/unit/test_eval_trusted.py -q` -> exit 0 (`11 passed`) after live reporter, rejection budget, run lock and production hard-negative coverage.
- `uv run ruff check .` -> exit 0; `uv run ruff format --check .` -> exit 0 (`61 files already formatted`); `uv run mypy src tests` -> exit 0 (`56 source files`); `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`51 passed`).
- `uv run python -m wikipediarag.cli eval-trusted-generate --count 50 --rejection-budget 30 --run-id trusted-v2-test-50-reliable` -> exit 0; live stdout showed catalog/family/attempt progress and final `total=50/50 rejected=1/30 errors=0 retries=0`.
- `uv run python -m wikipediarag.cli eval-trusted-status --run-id trusted-v2-test-50-reliable` -> exit 0; status reported `state=completed`, `total=50/50`, `rejected=1/30`, `errors=0`.
- `uv run python -m wikipediarag.cli eval-trusted-report --suite trusted-wikipedia-v2` -> exit 0; report paths `artifacts/eval/trusted-reports/trusted-wikipedia-v2-d29de56d51c4.md` and `.json`.
- JSONL verification for `trusted-wikipedia-v2-5e698f31-09c0-0346-b23f-8a943b6646ea-d29de56d51c4.jsonl` -> 50 lines, 50 unique task IDs, 50 `train`, 50 `unreviewed`, 4 `hard_negative`, no remaining run lock.
- `uv run ruff check .` -> exit 0; `uv run ruff format --check .` -> exit 0 (`61 files already formatted`); `uv run mypy src tests` -> exit 0 (`56 source files`); `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`51 passed`) after the resume-without-count fix.
- `docker compose up -d` -> exit 0; services started without clearing volumes.
- `uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://localhost:8081` -> exit 0; real OpenRouter aliases, 1024-dimensional embeddings, typed JSON and rerank were verified.
- `uv run python -m wikipediarag.cli eval-trusted-catalog` -> exit 0; catalog contains 6,000 local parser-aware items for snapshot `5e698f31-09c0-0346-b23f-8a943b6646ea`.
- Background `uv run python -m wikipediarag.cli eval-trusted-generate --count 300 --rejection-budget 30 --run-id trusted-v3-seed-300` completed by persisted status; `uv run python -m wikipediarag.cli eval-trusted-status --run-id trusted-v3-seed-300` -> exit 0 with `state=completed`, `total=300/300`, `rejected=5/30`, `errors=4`, `retries=3`.
- JSONL verification for `trusted-wikipedia-v2-5e698f31-09c0-0346-b23f-8a943b6646ea-8a1d43738f9b.jsonl` -> 300 lines, 300 unique task IDs, 300 `train`, 300 `unreviewed`, required parser-aware fields present, dataset hash `8a1d43738f9bed78ea45eaa136fc16982e9a2000120adc9a7d977ba85f115751`, no remaining run lock.
- Filename-only v3 copies written: `artifacts/eval/datasets/trusted-wikipedia-v2/trusted-wikipedia-v3-5e698f31-09c0-0346-b23f-8a943b6646ea-8a1d43738f9b.jsonl` and `.manifest.json`.
- First `uv run python -m wikipediarag.cli eval-trusted-pool --suite trusted-wikipedia-v2 --batch-size 10 --run-id trusted-v3-pool-300` pass completed via persisted status with 1,498/1,500 successful supported task-runs and 2 transient API failures after `05:28:16`.
- `uv run python -m wikipediarag.cli eval-trusted-pool --suite trusted-wikipedia-v2 --batch-size 10 --resume-run-id trusted-v3-pool-300 --rerun-failed` -> exit 0; `uv run python -m wikipediarag.cli eval-retrieval-status --run-id trusted-v3-pool-300` -> exit 0 with `state=completed`, `processed=1500/1500`, `failed=0`.
- `uv run python -m wikipediarag.cli eval-retrieval-report --latest` -> exit 0; wrote `artifacts/eval/retrieval-reports/trusted-v3-pool-300.md` and `.json`.
- `uv run python -m wikipediarag.cli eval-trusted-report --suite trusted-wikipedia-v2` -> exit 0; wrote `artifacts/eval/trusted-reports/trusted-wikipedia-v2-8a1d43738f9b.md` and `.json`.
- `sota_mvp_normal` on `trusted-v3-pool-300`: page recall `@1=0.9127`, `@5=0.9564`, `@10=0.9709`, `@20=0.9855`; chunk recall `@10=0.9200`; MRR@10 `0.8844`; nDCG@10 `0.8756`; path completion `0.8836`; p50 `1688 ms`; p95 `5927 ms`; error rate `0`; retrieval miss count `21`.
