# Project status

Last updated: 2026-07-28 10:09 +03:00

## Current phase

ExecPlan 19 observable release gate runs is complete. It was added after ExecPlan 18 stopped the real reviewed smoke gate because `eval-release-gate` did not expose realtime stage progress during provider-backed answer evaluation.

## Active ExecPlan

`docs/exec-plans/19-observable-release-gate-runs.md` is complete. ExecPlan 18 can now be resumed with `eval-release-gate` because the command exposes live progress and an inspectable status/log wrapper.

## Next design backlog

`docs/design/RAG_IMPROVEMENT_ROADMAP.md` tracks future design directions for claim-level verification, complexity routing, latency hardening, parent-child context packing, model-specific harness profiles and universal ingestion. It is not an active ExecPlan; each item requires its own approved ExecPlan before implementation.

## Current implementation update

- Added deterministic `IndexContract` and `RunContract` payloads with `sha256:` IDs.
- ZIM and XML ingestion write `metadata.index_contract_id` and `metadata.index_contract` to `index_versions`.
- `/api/v1/chat` and `/api/v1/search:debug` validate KB readiness before searching; incompatible/missing active index versions fail as safe `KB_NOT_READY`.
- Retrieval profile events, `RetrievalResult`, chat usage and persisted `query_runs.usage` include `index_contract_id` and `run_contract_id`.
- Eval task results and config summaries preserve contract IDs; summaries flag mixed contract IDs as errors.
- Answer and retrieval reports render a Contracts section.
- Added deterministic AnswerabilityGate v1 with statuses `ANSWERABLE`, `PARTIAL`, `UNANSWERABLE` and `CONFLICTING`.
- Direct and Extended Search retrieval results include top-level `answerability`; retrieval events include an `answerability` stage.
- `insufficient_evidence` remains for compatibility but is derived from answerability status.
- Chat skips generator provider calls for `UNANSWERABLE` and `CONFLICTING` gate decisions, and `PARTIAL` answers are prompted to state covered and missing parts explicitly.
- Conditional Extended Search fallback is now gated by `PARTIAL`/`UNANSWERABLE` rather than chunk count.
- Added external transfer models `ExternalQuestion`, `CorpusBoundCandidate` and `TrustedTask` for MIRACL Russian candidate mapping.
- `eval-miracl-map` now writes candidate JSONL plus manifest under `artifacts/eval/external/miracl-ru/`.
- MIRACL binding statuses are `EXACT`, `REDIRECT`, `AMBIGUOUS` and `MISSING`; decisions are `AUTO_ACCEPT`, `REVIEW` and `REJECT`.
- Only exact/confident redirect bindings are auto-accepted; ambiguous rows go to review and missing rows are rejected. No transferred row is published as locked `test`.
- Added normal retrieval stage timings for BM25, dense embedding/search, fusion, rerank, context packing and total retrieval.
- Added safe `timings_ms` summaries to `/api/v1/search:debug`, `/api/v1/chat` `usage.updated` and persisted `query_runs.usage`.
- Added generation timings for model chat, answer parsing, citation validation and insufficient-evidence refusal without provider calls.
- Added Extended Search timing for each harness tool call plus total deep-research/extended run duration.
- Added eval result propagation for observed timing keys and report-level `stage_latency_<key>_p50_ms` / `stage_latency_<key>_p95_ms` metrics.
- Changed retrieval-only evaluation pooling so `--batch-size N` is a max in-flight request limit; completed requests immediately free a slot for the next eligible task.
- Added regression coverage for retrieval pooling backfill and generation/trusted-generation concurrency backfill.
- Added `trusted-wikipedia-v2` dataset generation with base `EvalTask` compatibility plus parser-aware fields: `trusted_family`, `source_spans`, `structural_element`, `answer_type`, `verification_results`, `negative_candidates`, `provenance`, `split=train` and `review_status=unreviewed`.
- Added trusted catalog, generation status, event log, accepted partial checkpoint and rejected-attempt artifacts under `artifacts/eval/trusted-*`.
- Added strict trusted resume validation for snapshot ID, index version, ZIM checksum, retrieval profile hash, model aliases, count, rejection budget and family targets.
- Added trusted CLI commands: `eval-trusted-catalog`, `eval-trusted-generate`, `eval-trusted-status`, `eval-trusted-pool`, `eval-trusted-report` and optional `eval-miracl-map`.
- Added trusted live stdout generation progress for catalog/family/attempt/generated/accepted/rejected/retry/final events, with safe reasons only.
- Added trusted `--rejection-budget` defaulting to 30 for the whole run, exact accepted-count publication, provider retry reporting and PID run lock protection with explicit stale takeover.
- Trusted resume without `--count` reuses the stored checkpoint count instead of the CLI default.
- MIRACL-RU transfer artifacts now use candidate-only binding statuses `EXACT`, `REDIRECT`, `AMBIGUOUS` and `MISSING`, with decisions `AUTO_ACCEPT`, `REVIEW` and `REJECT`; legacy `needs_review`/`not_found` mapping was superseded by ExecPlan 16.
- Added focused tests for trusted schema distribution, catalog structural manifest, resumable partial generation and train/unreviewed reporting.
- Hardened trusted artifact writes for transient Windows `status.json` locks and added regression coverage for the retry path.
- Corrected hard-negative evidence construction: only the primary local source is gold and the paired local source is a disjoint distractor; production hard-negative tasks now use the configured Model Gateway generator alias, while deterministic generation is restricted to explicit mock aliases.
- Completed real run `trusted-v2-test-50-clean`: 50 tasks, all `train/unreviewed`, dataset hash `073d5f8dee9bdefb0f2d3c460bc26445000496051e45ce60e71f27c2287bc9bd`.
- Completed real reliability run `trusted-v2-test-50-reliable`: exactly 50 accepted tasks, 1 rejected duplicate within the 30-run budget, all `train/unreviewed`, dataset hash `d29de56d51c455c55e33a39cf5ffc15d7bc2b341e0ce2c9a881abc3deeecce46`.
- Added local JSONL evaluation subsystem for the current ZIM snapshot.
- Added CLI commands: `eval-smoke`, `eval-generate`, `eval-run`, `eval-report` and `eval-full`.
- Added typed `eval-generate` progress events and live stdout reporting for `eval-generate` and the generation stage inside `eval-full`.
- Added flexible generation runtime overrides from CLI: count, concurrency, generator alias, verifier alias, family weights, `run_id` and `resume_run_id`.
- Added persisted generation run-state under `artifacts/eval/generate-runs/<run_id>/` plus `eval-generate-status` for out-of-process status checks and strict resume from checkpoint.
- Added retrieval-only evaluation commands `eval-retrieval-run`, `eval-retrieval-status` and `eval-retrieval-report` over `/api/v1/search:debug`.
- Added retrieval batch progress, ETA, per-task result JSONL, lifecycle event logs, persisted run status and resume/failed-rerun support under `artifacts/eval/retrieval-runs/`.
- Added reviewed eval workflow commands: `eval-review-candidates`, `eval-freeze-reviewed` and `eval-release-gate`.
- Review workflow maps `AUTO_ACCEPT` to `reviewed`, keeps unresolved `REVIEW` rows `unreviewed`, maps `REJECT` to `rejected` and preserves snapshot/index/contract provenance.
- `eval-freeze-reviewed` writes immutable locked dev/test manifests under `artifacts/eval/datasets/<suite>/locked/`, reuses identical suite/snapshot/hash artifacts and fails rather than overwriting a different locked hash.
- `eval-release-gate` runs answer and retrieval eval only on locked reviewed dev/test rows; `test` findings are blocking and `dev` findings are diagnostic.
- Release gate v1 checks mixed contract IDs, citation precision, unsupported claims, unanswerable accuracy, retrieval false-positive evidence, by-family recall/MRR/nDCG regressions and p95 latency regressions.
- ExecPlan 18 smoke suite `reviewed-wikipedia-smoke-v1` was created from latest `trusted-wikipedia-v2` JSONL with source dataset hash `8a1d43738f9bed78ea45eaa136fc16982e9a2000120adc9a7d977ba85f115751`.
- Reviewed pool hash is `2d767e8afbb2d152813bb267aa58b38cc60d90799e92c1ec10ec269560c06645` with `300` reviewed rows.
- Locked smoke dev/test manifests exist under `artifacts/eval/datasets/reviewed-wikipedia-smoke-v1/locked/`; dev hash `e3ada32289a78dc98a79f936d058ac5707e997c9aab611060d8c4f04f28ea5de`, test hash `3fc3c61f23a6ee16930163ae8ab3d53ac4880365f3cdcab72542823e368d07f4`.
- Repeated freeze reused both locked splits; verification showed `10` dev rows and `10` test rows, all `review_status=reviewed`.
- `eval-release-gate` did not complete: one direct run timed out after 30 minutes and a second run was interrupted/stopped after it continued writing partial artifacts without live progress.
- `AGENTS.md` now requires long-running eval/ingestion/release-gate/generation commands to expose realtime progress/status or use an inspectable logging wrapper before milestone execution.
- Added answer eval run status for `run_suite`: `status.json`, `logs/events.jsonl`, progress callbacks, elapsed time, ETA, processed/completed/failed counters and safe task-ID-only stdout formatting.
- `eval-run` now uses the answer eval CLI reporter so long answer evaluation runs are visible while they execute.
- `eval-release-gate` now writes top-level release gate status under `artifacts/eval/release-gates/<suite>/<suite>-release-gate/status.json`, appends release gate events, streams flushed stage progress and forwards child answer/retrieval progress into the top-level status.
- Added `eval-release-gate-status --suite <name> [--json]` to inspect the latest release gate status without starting evaluation or modifying locked datasets.
- Release gate final JSON now includes additive `timings_ms` and `release_gate_run` fields.
- Progress output no longer includes full eval questions; it uses task IDs, config IDs, counters and timings.
- Added `make eval-smoke`, `make eval-generate EVAL_COUNT=150` and `make eval-full`.
- Added `docs/contracts/EVALUATION_CONTRACT.md`.

## Completed in this slice

- Added ADR-008: XML remains supported, default real demo source is ZIM/libzim + Kiwix.
- Added `config/retrieval.yaml` with typed `sota_mvp`, `test_mock`, `bm25_only`, `rewrite_off` and `parent_expansion_off` profiles.
- Added `config/models.yaml` aliases for OpenRouter and explicit mock aliases.
- Added Kiwix Compose service on `ghcr.io/kiwix/kiwix-serve:3.8.2`, host port `8083`, read-only `./zim:/data:ro`.
- Mounted the same `./zim:/zim:ro` into API/worker while preserving existing XML `./zip:/data:ro`.
- Added ZIM/libzim adapter, source URL builder from exact `zim_entry_path`, service/asset filtering, redirect persistence, parent-child chunks and deterministic IDs.
- Added `POST /api/v1/wikipedia/zim-imports` and `make import-zim-small WIKI_LIMIT=10000`.
- Added ZIM checkpoint fields for scanned entries, accepted articles, redirects, skipped entries, archive metadata, embedding alias/dimensions and index version.
- Added dynamic OpenSearch index naming/version metadata keyed by source snapshot, profile, embedding alias and dimensions.
- Refactored retrieval around one `RetrievalProfile` object with BM25/dense/RRF/rerank/postprocess switches and validated overrides.
- Added query embedding instruction for Russian Wikipedia factual-answer retrieval; document embeddings remain instruction-free.
- Extended Model Gateway alias resolution and strict OpenRouter startup probes for `sota_mvp`.
- Preserved mock provider only through explicit test/mock aliases and profile.
- Extended answer validation, provider usage/cost metadata propagation, deterministic citation/source URL checks and clickable UI source anchors.
- Extended bounded deterministic Extended Search state, tool ledger, stop reasons and debugger payload.
- Added closed-by-default UI `Advanced retrieval settings` for profile, top_k, BM25, dense, fusion, rerank, parent expansion and Extended Search.
- Hardened ZIM job failure handling: missing `.zim` now marks the job `failed` and does not spin in `running`.
- Added safe operator-visible generation progress with elapsed time, family/total counts, generated questions, rejection reasons and final summary without exposing prompts or raw provider errors.
- Added strict local cheap-filters for generic, leaky, duplicate and weak-answer candidates, plus `comparison_multi_hop` validation for multi-document reasoning paths.

## Validation evidence

- 2026-07-28 ExecPlan 19 focused validation: `uv run pytest tests/unit/test_eval_review.py tests/integration/test_eval_runner.py -q` -> exit 0 (`6 passed`).
- 2026-07-28 ExecPlan 19 focused validation: `uv run ruff check src/wikipediarag/eval/schemas.py src/wikipediarag/eval/runner.py src/wikipediarag/eval/retrieval_runner.py src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_runner.py` -> exit 0.
- 2026-07-28 ExecPlan 19 focused validation: `uv run ruff format --check src/wikipediarag/eval/schemas.py src/wikipediarag/eval/runner.py src/wikipediarag/eval/retrieval_runner.py src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_runner.py` -> exit 0 (`8 files already formatted`).
- 2026-07-28 ExecPlan 19 focused validation: `uv run mypy src/wikipediarag/eval/schemas.py src/wikipediarag/eval/runner.py src/wikipediarag/eval/retrieval_runner.py src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_runner.py` -> exit 0.
- 2026-07-28 ExecPlan 19 final validation: `uv run ruff check .` -> exit 0.
- 2026-07-28 ExecPlan 19 final validation: `uv run ruff format --check .` -> exit 0 (`70 files already formatted`).
- 2026-07-28 ExecPlan 19 final validation: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 65 source files`).
- 2026-07-28 ExecPlan 19 final validation: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`79 passed`).
- 2026-07-28 documentation cleanup validation: `uv run pytest tests/unit/test_eval_external.py -q` -> exit 0 (`4 passed`).
- 2026-07-28 documentation cleanup validation: `uv run pytest tests/unit/test_eval_review.py tests/integration/test_eval_runner.py -q` -> exit 0 (`6 passed`).
- 2026-07-28 documentation cleanup validation: `uv run ruff check .` -> exit 0.
- 2026-07-28 documentation cleanup validation: `uv run ruff format --check .` -> exit 0 (`69 files already formatted`).
- 2026-07-28 documentation cleanup validation: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 65 source files`).
- 2026-07-28 documentation cleanup validation: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`79 passed`).
- 2026-07-28 ExecPlan 17 focused validation: `uv run ruff check src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_review_workflow.py` -> exit 0.
- 2026-07-28 ExecPlan 17 focused validation: `uv run mypy src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_review_workflow.py` -> exit 0 (`Success: no issues found in 5 source files`).
- 2026-07-28 ExecPlan 17 focused validation: `uv run pytest tests/unit/test_eval_review.py tests/integration/test_eval_review_workflow.py -q` -> exit 0 (`4 passed`).
- 2026-07-28 ExecPlan 17 final validation: first `uv run ruff format --check .` found `src/wikipediarag/eval/review.py` required formatting; `uv run ruff format src/wikipediarag/eval/review.py` -> exit 0 (`1 file reformatted`).
- 2026-07-28 ExecPlan 17 final validation: `uv run ruff check .` -> exit 0.
- 2026-07-28 ExecPlan 17 final validation: `uv run ruff format --check .` -> exit 0 (`70 files already formatted`).
- 2026-07-28 ExecPlan 17 final validation: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 65 source files`).
- 2026-07-28 ExecPlan 17 final validation: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`77 passed`).
- 2026-07-27 ExecPlan 15 focused validation: `uv run pytest tests/unit/test_answerability.py tests/unit/test_retrieval_answering.py tests/unit/test_extended.py -q` -> exit 0 (`15 passed`).
- 2026-07-27 ExecPlan 15 focused validation: `uv run ruff check src/wikipediarag/answerability.py src/wikipediarag/answering.py src/wikipediarag/api_app.py src/wikipediarag/retrieval.py src/wikipediarag/extended.py tests/unit/test_answerability.py tests/unit/test_retrieval_answering.py` -> exit 0.
- 2026-07-27 ExecPlan 15 focused validation: `uv run mypy src/wikipediarag/answerability.py src/wikipediarag/answering.py src/wikipediarag/api_app.py src/wikipediarag/retrieval.py src/wikipediarag/extended.py tests/unit/test_answerability.py tests/unit/test_retrieval_answering.py tests/unit/test_extended.py` -> exit 0 (`Success: no issues found in 8 source files`).
- 2026-07-27 ExecPlan 16 focused validation: `uv run pytest tests/unit/test_eval_external.py -q` -> exit 0 (`4 passed`).
- 2026-07-27 ExecPlan 16 focused validation: `uv run ruff check src/wikipediarag/eval/external.py src/wikipediarag/eval/commands.py src/wikipediarag/eval/trusted.py tests/unit/test_eval_external.py` -> exit 0.
- 2026-07-27 ExecPlan 16 focused validation: `uv run mypy src/wikipediarag/eval/external.py src/wikipediarag/eval/commands.py src/wikipediarag/eval/trusted.py tests/unit/test_eval_external.py` -> exit 0 (`Success: no issues found in 4 source files`).
- 2026-07-27 ExecPlan 15/16 final validation: first `uv run ruff format --check .` found 2 files requiring formatting; `uv run ruff format .` -> exit 0 (`2 files reformatted, 65 files left unchanged`).
- 2026-07-27 ExecPlan 15/16 final validation: `uv run ruff check .` -> exit 0.
- 2026-07-27 ExecPlan 15/16 final validation: `uv run ruff format --check .` -> exit 0 (`67 files already formatted`).
- 2026-07-27 ExecPlan 15/16 final validation: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 62 source files`).
- 2026-07-27 ExecPlan 15/16 final validation: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`73 passed`).
- 2026-07-27 ExecPlan 14 focused validation: `uv run pytest tests/unit/test_retrieval_contract.py tests/unit/test_retrieval_answering.py tests/unit/test_extended.py tests/unit/test_eval_retrieval_runner.py tests/integration/test_eval_runner.py tests/integration/test_contracts.py -q` -> exit 0 (`23 passed`).
- 2026-07-27 ExecPlan 14 final validation: `uv run ruff check .` -> exit 0.
- 2026-07-27 ExecPlan 14 final validation: `uv run ruff format --check .` -> exit 0 (`63 files already formatted`).
- 2026-07-27 ExecPlan 14 final validation: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 58 source files`).
- 2026-07-27 ExecPlan 14 final validation: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`63 passed`).
- 2026-07-27 ExecPlan 13 targeted timing/scheduler validation: `uv run pytest tests/unit/test_retrieval_answering.py tests/unit/test_extended.py tests/unit/test_eval_retrieval_runner.py tests/unit/test_eval_generation.py::test_generate_family_refills_slot_when_attempt_completes tests/unit/test_eval_trusted.py::test_trusted_generate_family_refills_slot_when_attempt_completes tests/integration/test_eval_retrieval_runner_integration.py tests/integration/test_eval_runner.py -q` -> exit 0 (`18 passed`).
- 2026-07-27 ExecPlan 13 final validation: `uv run ruff check .` -> exit 0.
- 2026-07-27 ExecPlan 13 final validation: `uv run ruff format --check .` -> exit 0 (`61 files already formatted`).
- 2026-07-27 ExecPlan 13 final validation: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 56 source files`).
- 2026-07-27 ExecPlan 13 final validation: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`58 passed`).
- 2026-07-26 trusted dataset v2: `uv run pytest tests/unit/test_eval_trusted.py tests/unit/test_eval_generation.py tests/unit/test_eval_retrieval_runner.py -q` -> exit 0 (`14 passed`).
- 2026-07-26 trusted dataset v2: `uv run pytest tests/unit/test_eval_trusted.py tests/unit/test_eval_generation.py tests/integration/test_eval_runner.py -q` -> exit 0 (`11 passed`).
- 2026-07-26 trusted dataset v2: `uv run ruff check .` -> exit 0.
- 2026-07-26 trusted dataset v2: `uv run ruff format --check .` -> exit 0 (`61 files already formatted`).
- 2026-07-26 trusted dataset v2: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 56 source files`).
- 2026-07-26 trusted dataset v2: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`44 passed`).
- 2026-07-26 trusted v2 real 50-item run: `uv run python -m wikipediarag.cli eval-trusted-catalog` -> exit 0; 6,000 catalog items for ZIM snapshot `5e698f31-09c0-0346-b23f-8a943b6646ea`.
- 2026-07-26 trusted v2 real 50-item run: `uv run python -m wikipediarag.cli eval-trusted-generate --count 50 --resume-run-id trusted-v2-test-50-clean` -> exit 0; final dataset has 50 tasks, 0 generation errors, and all records are `train/unreviewed`.
- 2026-07-26 trusted v2 follow-up: `uv run pytest tests/unit/test_eval_trusted.py -q` -> exit 0 (`7 passed`); targeted Ruff and mypy checks passed.
- 2026-07-26 trusted v2 final validation: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests` and `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`47 passed`).
- 2026-07-26 trusted v2 reliability follow-up: `uv run pytest tests/unit/test_eval_trusted.py -q` -> exit 0 (`11 passed`).
- 2026-07-26 trusted v2 reliability follow-up: `uv run ruff check src/wikipediarag/model_client.py src/wikipediarag/eval/trusted.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_trusted.py` -> exit 0.
- 2026-07-26 trusted v2 reliability follow-up: `uv run mypy src/wikipediarag/model_client.py src/wikipediarag/eval/trusted.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_trusted.py` -> exit 0.
- 2026-07-26 trusted v2 reliability follow-up: `uv run ruff check .` -> exit 0; `uv run ruff format --check .` -> exit 0 (`61 files already formatted`); `uv run mypy src tests` -> exit 0 (`56 source files`); `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`51 passed`).
- 2026-07-26 trusted v2 real reliability run: `uv run python -m wikipediarag.cli eval-trusted-generate --count 50 --rejection-budget 30 --run-id trusted-v2-test-50-reliable` -> exit 0; final stdout `total=50/50 rejected=1/30 errors=0 retries=0`.
- 2026-07-26 trusted v2 real reliability run: `uv run python -m wikipediarag.cli eval-trusted-status --run-id trusted-v2-test-50-reliable` -> exit 0; state `completed`, family counts `15/7/6/6/4/4/4/4`.
- 2026-07-26 trusted v2 real reliability run: JSONL verification -> 50 lines, 50 unique task IDs, 50 `train`, 50 `unreviewed`, 4 `hard_negative`, no remaining `run.lock`.
- 2026-07-26 trusted v2 resume-without-count fix: `uv run ruff check .` -> exit 0; `uv run ruff format --check .` -> exit 0 (`61 files already formatted`); `uv run mypy src tests` -> exit 0 (`56 source files`); `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`51 passed`).
- 2026-07-27 trusted v2 retrieval pool: `uv run python -m wikipediarag.cli eval-trusted-pool --suite trusted-wikipedia-v2 --batch-size 10 --run-id trusted-v2-pool-50-20260727` -> exit 0; status `completed`, processed `250/250`, failed API runs `0`, elapsed `00:14:06`.
- 2026-07-27 trusted v2 retrieval report: `uv run python -m wikipediarag.cli eval-retrieval-report --latest` -> exit 0; wrote `artifacts/eval/retrieval-reports/trusted-v2-pool-50-20260727.md` and `.json`.
- 2026-07-27 trusted v2 `sota_mvp_normal` metrics: page recall `@1=0.8913`, `@5=0.9130`, `@10=0.9348`, `@20=0.9565`; chunk recall `@10=0.9130`; MRR@10 `0.8877`; nDCG@10 `0.8838`; path completion `0.9130`; p50 `1977 ms`; p95 `3848 ms`; error rate `0`.
- 2026-07-27 trusted v2 retrieval misses for `sota_mvp_normal`: `trusted-wiki-000013`, `trusted-wiki-000023`, `trusted-wiki-000042`; all had empty runtime `errors` arrays.
- 2026-07-27 trusted v3 filename artifact: `docker compose up -d` -> exit 0; `uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://localhost:8081` -> exit 0 with real OpenRouter aliases, 1024-dimensional embeddings, typed JSON and rerank response.
- 2026-07-27 trusted v3 filename artifact: `uv run python -m wikipediarag.cli eval-trusted-catalog` -> exit 0; catalog has 6,000 items for snapshot `5e698f31-09c0-0346-b23f-8a943b6646ea`.
- 2026-07-27 trusted v3 filename artifact: background `uv run python -m wikipediarag.cli eval-trusted-generate --count 300 --rejection-budget 30 --run-id trusted-v3-seed-300` completed by persisted status; `uv run python -m wikipediarag.cli eval-trusted-status --run-id trusted-v3-seed-300` -> exit 0, `state=completed`, `total=300/300`, `rejected=5/30`, `errors=4`, `retries=3`.
- 2026-07-27 trusted v3 filename artifact: JSONL validation -> 300 lines, 300 unique task IDs, all `train/unreviewed`, required parser-aware fields present, no remaining `run.lock`, dataset hash `8a1d43738f9bed78ea45eaa136fc16982e9a2000120adc9a7d977ba85f115751`.
- 2026-07-27 trusted v3 filename artifact: canonical files `artifacts/eval/datasets/trusted-wikipedia-v2/trusted-wikipedia-v2-5e698f31-09c0-0346-b23f-8a943b6646ea-8a1d43738f9b.jsonl` and `.manifest.json`; additional filename-only v3 copies `trusted-wikipedia-v3-5e698f31-09c0-0346-b23f-8a943b6646ea-8a1d43738f9b.jsonl` and `.manifest.json`.
- 2026-07-27 trusted v3 retrieval pool: first `eval-trusted-pool --suite trusted-wikipedia-v2 --batch-size 10 --run-id trusted-v3-pool-300` pass completed with 1,498/1,500 successful supported task-runs and 2 transient API failures after `05:28:16`; `uv run python -m wikipediarag.cli eval-trusted-pool --suite trusted-wikipedia-v2 --batch-size 10 --resume-run-id trusted-v3-pool-300 --rerun-failed` -> exit 0.
- 2026-07-27 trusted v3 retrieval pool: `uv run python -m wikipediarag.cli eval-retrieval-status --run-id trusted-v3-pool-300` -> exit 0; final status `completed`, processed `1500/1500`, failed API runs `0`, dataset hash `8a1d43738f9bed78ea45eaa136fc16982e9a2000120adc9a7d977ba85f115751`.
- 2026-07-27 trusted v3 reports: `uv run python -m wikipediarag.cli eval-retrieval-report --latest` -> exit 0; wrote `artifacts/eval/retrieval-reports/trusted-v3-pool-300.md` and `.json`. `uv run python -m wikipediarag.cli eval-trusted-report --suite trusted-wikipedia-v2` -> exit 0; wrote `artifacts/eval/trusted-reports/trusted-wikipedia-v2-8a1d43738f9b.md` and `.json`.
- 2026-07-27 trusted v3 `sota_mvp_normal` metrics: page recall `@1=0.9127`, `@5=0.9564`, `@10=0.9709`, `@20=0.9855`; chunk recall `@10=0.9200`; MRR@10 `0.8844`; nDCG@10 `0.8756`; path completion `0.8836`; p50 `1688 ms`; p95 `5927 ms`; error rate `0`; retrieval miss count `21`.
- `docker compose up -d --build`: exit 0; API, UI, worker, PostgreSQL, Redis, MinIO, OpenSearch, OTel collector, mock provider and Model Gateway started.
- `docker compose ps -a`: Kiwix service exists but is `Exited (0)` because `/data` is empty; Kiwix log: `Unable to add the ZIM file '*.zim'`.
- `uv run ruff check .`: exit 0, all checks passed.
- `uv run ruff format --check .`: exit 0, 37 files already formatted.
- `uv run mypy src tests`: exit 0, no issues in 32 source files.
- `uv run pytest tests/unit tests/integration tests/e2e -q`: exit 0, 19 passed.
- `cd services/ui && pnpm lint`: exit 0.
- `cd services/ui && pnpm typecheck`: exit 0.
- `cd services/ui && pnpm format:check`: exit 0.
- `cd services/ui && pnpm build`: exit 0; Vite production build succeeded.
- `uv run python -m wikipediarag.cli smoke`: exit 0; SSE events `run.started`, `message.delta`, `usage.updated`, `run.completed`.
- `uv run python -m wikipediarag.cli eval`: exit 0; `wiki-mini` returned evidence for both demo questions.
- `uv run python -m wikipediarag.cli smoke-models --provider mock`: exit 0; aliases listed, mock embedding dimensions `64`, typed JSON returned, rerank ordered.
- `uv run python -m wikipediarag.cli smoke-models --provider openrouter`: exit 1; no silent fallback, gateway returned 503 without an OpenRouter key.
- `uv run python -m wikipediarag.cli import-zim --limit 10000 --wait`: exit 1; job failed quickly with `no .zim file found in /zim`.
- `uv run python -m wikipediarag.cli demo-release-gate`: exit 1; blocked with `demo release gate requires a real ./zim/*.zim archive`.
- Override check: `POST /api/v1/search:debug` with `test_mock` BM25-only/rewrite-off/parent-expansion-off overrides returned stage payload showing `dense=false`, `fusion=none`, `rerank=false`, `parent_expansion=off`.
- 2026-07-26 real-run: `docker compose down -v --remove-orphans` removed the PostgreSQL, OpenSearch and MinIO volumes; `docker compose up -d --build` recreated the stack from clean state.
- 2026-07-26 real ZIM/Kiwix: root and `http://localhost:8083/content/wikipedia_ru_all_mini_2026-07/%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F` returned HTTP 200. Source URLs now use Kiwix's required `/content/<filename-stem>/<exact-entry-path>` contract.
- 2026-07-26 fresh schema bootstrap: fixed the DDL order so `knowledge_bases` exists before `index_versions`; clean PostgreSQL contains all required tables and zero documents, chunks and ingestion jobs.
- 2026-07-26 valid OpenRouter key: `smoke-models --provider openrouter` passed through Model Gateway; all required aliases were available, `embed_default` returned 1024 dimensions, `generator_fast` returned typed JSON and `rerank_default` ordering was verified.
- 2026-07-26 real ZIM job `be3f325a-a9f2-4a13-b83b-5e749de0148c` completed: 10,000 canonical pages, 14,281 chunks, 19,814 scanned entries, 9,759 redirects and 35 skipped service/assets. Checkpoint ended at entry 19,756 for archive `5e698f31-09c0-0346-b23f-8a943b6646ea`.
- 2026-07-26 restart/resume was proven with the same job: worker stopped at checkpoint 56 after 20 pages, then restarted and continued to 40 pages; later processing resumed from checkpoint 1,874 and finished without another job.
- 2026-07-26 PostgreSQL has 10,000 `wikipedia_zim` documents and 14,281 chunks; every chunk embedding has length 1024. OpenSearch `wiki-chunks-387df2fb225f794d` also has 14,281 documents.
- 2026-07-26 `sota_mvp` debugger for `Что такое 10-гигабитный Ethernet?` recorded BM25 rank 1, dense rank 1, RRF contributions and rerank score 0.9760768 for the canonical article. Its exact Kiwix URL returned HTTP 200.
- 2026-07-26 SSE chat completed in 61.7 seconds with `run.started`, `message.delta`, `usage.updated`, `run.completed`, validated citations and provider-returned cost. The stored run used `generator_main` through provider `Phala` and cost `0.00886215`.
- 2026-07-26 browser E2E rendered that Russian answer and source anchor `[S1]`; Kiwix showed heading `10-гигабитный Ethernet` at the anchor target URL.
- 2026-07-26 `demo-release-gate --job-id be3f325a-a9f2-4a13-b83b-5e749de0148c` passed inside the API container. It checks the same completed job rather than creating a duplicate and probes internal Kiwix while preserving public source URLs.
- 2026-07-26 observability change: `uv run pytest tests/unit/test_eval_generation.py tests/unit/test_eval_progress.py -q` -> exit 0 (`5 passed`).
- 2026-07-26 observability change: `uv run ruff check .` -> exit 0.
- 2026-07-26 observability change: `uv run ruff format --check .` -> exit 0 (`54 files already formatted`).
- 2026-07-26 observability change: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 49 source files`).
- 2026-07-26 observability change: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`28 passed`).
- 2026-07-26 flexible eval runtime/checkpoint change: `uv run pytest tests/unit/test_eval_generation.py tests/unit/test_eval_progress.py -q` -> exit 0 (`12 passed`).
- 2026-07-26 flexible eval runtime/checkpoint change: `uv run ruff check .` -> exit 0.
- 2026-07-26 flexible eval runtime/checkpoint change: `uv run ruff format --check .` -> exit 0 (`55 files already formatted`).
- 2026-07-26 flexible eval runtime/checkpoint change: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 50 source files`).
- 2026-07-26 flexible eval runtime/checkpoint change: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`35 passed`).
- 2026-07-26 retrieval-only eval runner change: `uv run ruff check .` -> exit 0.
- 2026-07-26 retrieval-only eval runner change: `uv run ruff format --check .` -> exit 0 (`59 files already formatted`).
- 2026-07-26 retrieval-only eval runner change: `uv run mypy src tests` -> exit 0 (`Success: no issues found in 54 source files`).
- 2026-07-26 retrieval-only eval runner change: `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`40 passed`).
- 2026-07-26 retrieval-only real run: `uv run python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10 --run-id retrieval-validation-150` completed via persisted status: 750/750 supported task-runs, 0 failed API runs, elapsed `01:52:47`.
- 2026-07-26 retrieval-only real status: `uv run python -m wikipediarag.cli eval-retrieval-status --latest` -> exit 0; reported `state=completed`, `processed=750/750`, `failed=0`.
- 2026-07-26 retrieval-only real report: `uv run python -m wikipediarag.cli eval-retrieval-report --latest` -> exit 0; wrote `artifacts/eval/retrieval-reports/retrieval-validation-150.md` and `.json`.
- 2026-07-26 retrieval-only resume: `uv run python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10 --resume-run-id retrieval-validation-150` -> exit 0 and reused completed result rows.

## Current local data state

- Real ZIM pages imported: `10,000` canonical non-redirect pages.
- Real ZIM chunks indexed: `14,281` child chunks in `wiki-chunks-387df2fb225f794d`.
- Real ZIM redirects persisted as provenance: `9,722` unique redirect documents (`9,759` redirect entries observed by the job).

## Real-demo commands

```bash
cp .env.example .env
# Set OPENROUTER_API_KEY and place one real Russian Wikipedia .zim in ./zim
docker compose up -d --build
make import-zim-small WIKI_LIMIT=10000
make smoke-models PROVIDER=openrouter
make demo-release-gate
```

In this Windows Codex environment `make` is not installed in `PATH`; equivalent `uv`/`pnpm` commands were run directly.

## Model aliases

- `embed_default` -> `openrouter:qwen/qwen3-embedding-8b`, `dimensions=1024`.
- `generator_fast` -> `openrouter:qwen/qwen3.5-9b`.
- `generator_main` -> `openrouter:qwen/qwen3.6-35b-a3b`.
- `verifier` -> `openrouter:qwen/qwen3.5-9b`.
- `rerank_default` -> `openrouter:cohere/rerank-v3.5`.
- Mock aliases remain explicit: `mock_embed_default`, `mock_generator_fast`, `mock_generator_main`, `mock_verifier`, `mock_rerank_default`.

In this Windows environment GNU Make is not on `PATH`; equivalent container commands are:

```bash
docker compose exec -T api python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://model-gateway:8080
docker compose exec -T api python -m wikipediarag.cli import-zim --limit 10000 --wait --api http://localhost:8000
docker compose exec -T api python -m wikipediarag.cli demo-release-gate --api http://localhost:8000 --job-id <job-id>
```

## Known limitations

- Full import consumes provider time and cost: OpenRouter embedding latency made the real 10,000-page run take roughly 73 minutes in this environment.
- A normal source link uses `target="_blank"`; the browser test verified the anchor target and opened the same URL in a controlled Kiwix tab because the test runtime did not retain a popup tab from `_blank`.
- A cancelled SSE client is now recorded as `ClientDisconnected`; existing query runs created before that fix may remain `running` and can be ignored for the demo data set.
- `make` targets exist but were not directly runnable on this host because GNU Make is not installed in Windows `PATH`.
- A fresh real `eval-smoke` / `eval-generate` / `eval-generate-status` / `eval-run` / `eval-report` cycle was not re-executed after the flexible runtime/checkpoint change because it depends on the current OpenRouter-backed corpus state and incurs provider time/cost.
