# ExecPlan 14 - Retrieval contract and KB readiness

## 1. Outcome

Every online retrieval run and evaluation result is tied to a deterministic index/run contract, and a knowledge base whose active alias cannot be matched to a compatible `index_versions` record fails safely with `KB_NOT_READY` instead of searching a silent fallback index.

## 2. Why this plan exists

`SPEC.md`, `docs/architecture.md` and `docs/quality/EVALUATION_PLAN.md` require reproducible retrieval and comparable evaluation results. ExecPlan 13 added stage timings and eval concurrency, but online search could still run against an active alias without an explicit compatibility passport. This plan closes that reproducibility gap before AnswerabilityGate or further Extended Search work.

## 3. In scope

- Deterministic `IndexContract` and `RunContract` payloads with `sha256:` IDs.
- Store `index_contract` and `index_contract_id` in `index_versions.metadata` for ZIM and XML ingestion.
- Validate active KB read alias, source/profile compatibility and embedding alias/dimensions before retrieval.
- Return safe `KB_NOT_READY` API errors for incompatible `/api/v1/search:debug` and pre-stream `/api/v1/chat`.
- Propagate contract IDs through retrieval events, `RetrievalResult`, chat usage, persisted `query_runs.usage`, eval result JSONL and eval summaries/reports.
- Focused deterministic tests for contract hashing, validation and eval propagation.

## 4. Out of scope

- AnswerabilityGate statuses and generation behavior changes.
- MIRACL/RusBEIR/HotpotQA dataset transfer.
- Reviewed dev/test locking and release gate policy.
- Database schema migrations or new tables.
- Reindexing existing local data or mutating Docker/OpenSearch volumes.

## 5. Preconditions

- ExecPlan 13 is complete.
- `index_versions`, `knowledge_bases.active_index`, `config/retrieval.yaml` and `config/models.yaml` exist.
- ZIM ingestion already creates versioned aliases and index version records.

## 6. Contracts and invariants

- `IndexContract` contains only index compatibility inputs; reranker/generator/verifier remain in `RunContract`.
- `RunContract` includes effective retrieval profile hash, overrides hash, model alias refs and `answerability_gate_version`.
- Missing or incompatible active index metadata raises `KB_NOT_READY`; no mock/hash fallback is allowed for real profiles.
- Contract payloads contain model aliases/provider/model names and dimensions, but no prompts, document text, provider bodies or secrets.
- Existing eval JSONL remains readable because new fields are additive with defaults.

## 7. Milestones

### M14.1 Contract model and ingestion metadata

- Add retrieval contract models and stable IDs.
- Save index contract metadata during XML and ZIM ingestion.
- Validation: unit contract hash tests.

### M14.2 Runtime KB readiness validation

- Validate active KB contract in retrieval and pre-stream chat/debug API paths.
- Add contract IDs to retrieval events, results and query usage.
- Validation: retrieval/answering/extended tests.

### M14.3 Eval propagation and documentation

- Carry contract IDs into eval task results, summaries and Markdown/JSON reports.
- Update public contracts and status.
- Validation: focused eval runner tests, then full lint/type/test commands.

## 8. Acceptance criteria

- New ZIM/XML index versions store `metadata.index_contract_id` and `metadata.index_contract`.
- Retrieval fails with `KB_NOT_READY` when the active alias has no registered compatible index version.
- `/api/v1/search:debug` response includes top-level `index_contract_id` and `run_contract_id`.
- `/api/v1/chat` usage and persisted `query_runs.usage` include contract IDs.
- Retrieval events include contract IDs in the `profile` stage.
- Eval result rows and config summaries preserve contract IDs and flag mixed IDs in one config summary.

## 9. Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## 10. Demo

```bash
python -m wikipediarag.cli import-zim --limit 10000 --wait
python -m wikipediarag.cli eval-retrieval-run --suite trusted-wikipedia-v2 --batch-size 10 --run-id contract-demo
python -m wikipediarag.cli eval-retrieval-report --latest
```

Expected report output includes a `Contracts` section with one `index_contract_id` and one `run_contract_id` per compatible config.

## 11. Rollback and recovery

Revert code and documentation changes from this plan. Do not delete ZIM files, PostgreSQL/OpenSearch/MinIO volumes or existing eval artifacts. New eval artifacts under `artifacts/eval/*contract-demo*` may be removed if created.

## 12. Progress

- [x] 2026-07-27: Plan accepted by user for implementation as the next single milestone.
- [x] 2026-07-27: Contract models, KB readiness validation and runtime propagation implemented.
- [x] 2026-07-27: Eval propagation and focused tests implemented.
- [x] 2026-07-27: Final validation commands completed.

## 13. Discoveries

- ZIM ingestion already used versioned aliases and `index_versions`; XML fallback used the historical default alias and needed an explicit index version record.
- Existing real local ZIM data imported before this plan may not have stored contract metadata, so validation computes and propagates IDs from `index_versions` when metadata is absent.

## 14. Decision log

- `rerank`, `generator_fast`, `generator_main` and `verifier` are part of `RunContract`, not `IndexContract`, so changing them does not imply reindexing.
- Existing eval `retrieval_profile_hash` fields remain for compatibility; contract IDs are additive.
- `answerability_gate_version` is recorded as `none` until ExecPlan 15 implements the gate.

## 15. Final evidence

- `uv run pytest tests/unit/test_retrieval_contract.py tests/unit/test_retrieval_answering.py tests/unit/test_extended.py tests/unit/test_eval_retrieval_runner.py tests/integration/test_eval_runner.py tests/integration/test_contracts.py -q` -> exit 0 (`23 passed`).
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`63 files already formatted`).
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 58 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`63 passed`).
