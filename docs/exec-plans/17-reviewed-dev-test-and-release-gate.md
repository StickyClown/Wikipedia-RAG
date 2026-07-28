# ExecPlan 17 - Reviewed dev/test and release gate

## 1. Outcome

Evaluation candidates can be converted into a reviewed pool, small immutable dev/test manifests can be frozen from reviewed rows only, and the release gate can run against those reviewed slices with blocking `test` failures and diagnostic `dev` findings.

## 2. Why this plan exists

`SPEC.md` and `docs/quality/EVALUATION_PLAN.md` require regression gates over reviewed evaluation slices. ExecPlan 16 produced candidate artifacts for trusted and MIRACL-derived questions but intentionally left them as candidate-only `train/unreviewed` data. This plan adds the missing reviewed layer without changing existing train artifacts or publishing external rows directly as locked tests.

## 3. In scope

- Add a file-based review model for evaluation candidates.
- Convert `AUTO_ACCEPT` rows into `reviewed` pool rows while leaving `REVIEW` rows unreviewed and `REJECT` rows rejected.
- Preserve provenance including snapshot, index version, contract IDs when available and source dataset hash.
- Add `eval-review-candidates --input <jsonl> --output-suite <name>`.
- Add `eval-freeze-reviewed --suite <name> --dev-count N --test-count N`.
- Write immutable locked dev/test manifests under `artifacts/eval/datasets/<suite>/locked/`.
- Add `eval-release-gate --suite <name> --api <url>`.
- Gate only reviewed locked dev/test rows.
- Add deterministic unit/integration tests for transitions, freeze behavior and gate failures.

## 4. Out of scope

- Human review UI.
- New external datasets beyond existing candidate JSONL inputs.
- Conflict resolver.
- GraphRAG, swarm, ColBERT, learned sparse or proposition indexing.
- Overwriting existing train artifacts or replacing locked test with a different hash.

## 5. Preconditions

- ExecPlan 16 is complete.
- Existing eval JSONL artifacts can be read as `EvalTask`-compatible rows or external `trusted_task` candidates.
- `artifacts/eval/` remains ignored and stores generated pool, locked and run artifacts.

## 6. Contracts and invariants

- `AUTO_ACCEPT` maps to `review_status=reviewed`.
- `REVIEW` maps to `review_status=unreviewed` unless a file-based manual edit already marks it `reviewed`.
- `REJECT` maps to `review_status=rejected`.
- Freeze selects only `review_status=reviewed`; it never backfills from unreviewed rows.
- Locked manifests are immutable: same suite/snapshot/hash can be reused, different hash fails without overwrite.
- `eval-release-gate` loads locked dev/test manifests and refuses rows that are not reviewed or not in the requested split.
- Mixed contract IDs are reported as release-gate findings.

## 7. Milestones

### M17.1 Review pool

- Add reviewed task model and review-pool writer.
- Validation: transition and conversion unit tests.

### M17.2 Frozen manifests

- Add deterministic reviewed split selection and immutable locked dev/test artifacts.
- Validation: deterministic split and locked overwrite tests.

### M17.3 Release gate

- Add reviewed release-gate runner and default v1 checks.
- Validation: gate failure tests for mixed contracts, citations, no-answer, retrieval and latency regressions.

### M17.4 Docs and final validation

- Update evaluation contract and project status.
- Run final lint, format, typecheck and tests.

## 8. Acceptance criteria

- `eval-review-candidates` writes a reviewed pool manifest with counts by decision and review status.
- `eval-freeze-reviewed` fails when reviewed rows are insufficient.
- Re-running freeze with the same locked suite/snapshot/hash reuses existing locked manifests.
- Re-running freeze with a different locked hash fails without overwrite.
- Locked dev/test JSONL rows are all `review_status=reviewed`.
- `eval-release-gate` treats `test` findings as blocking and `dev` findings as diagnostic.
- Release gate fails on mixed contract IDs, citation precision below 1.0, unsupported claim rate above 0, unanswerable accuracy below 1.0, false positive evidence, quality regression above 0.03 and p95 latency regression above 25%.

## 9. Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## 10. Demo

```bash
python -m wikipediarag.cli eval-review-candidates --input artifacts/eval/datasets/trusted-wikipedia-v2/<dataset>.jsonl --output-suite reviewed-wikipedia-v1
python -m wikipediarag.cli eval-freeze-reviewed --suite reviewed-wikipedia-v1 --dev-count 20 --test-count 20
python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-v1 --api http://localhost:8000
```

## 11. Rollback and recovery

Revert code and documentation changes from this plan. Artifacts under `artifacts/eval/datasets/<suite>/reviewed-pool*` and `artifacts/eval/datasets/<suite>/locked/` may be deleted only for suites created during this plan. Do not delete existing trusted train datasets, MIRACL transfer candidates, retrieval runs, reports, ZIM files, database volumes, OpenSearch indices or MinIO objects.

## 12. Progress

- [x] 2026-07-28: Plan created for reviewed dev/test and release gate.
- [x] 2026-07-28: Review pool, freeze and release-gate code implemented.
- [x] 2026-07-28: Focused unit/integration tests added.
- [x] 2026-07-28: Final validation commands completed.

## 13. Discoveries

- Existing trusted rows are generated as `train/unreviewed`; the reviewed layer must be separate to avoid changing historical train artifacts.
- Existing external candidate rows contain a lightweight `trusted_task`, so review conversion synthesizes only the `EvalTask` fields needed by current runners when full task fields are absent.

## 14. Decision log

- Locked dev and test manifests use stable paths `locked/dev.*` and `locked/test.*` to make overwrite protection explicit.
- `dev` gate findings are diagnostic; `test` gate findings are blocking.
- Baseline comparison reads optional `baseline` or `release_gate_baseline` from the locked dev manifest and skips regression checks when no baseline is present.

## 15. Final evidence

- `uv run ruff check src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_review_workflow.py` -> exit 0.
- `uv run mypy src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_review_workflow.py` -> exit 0 (`Success: no issues found in 5 source files`).
- `uv run pytest tests/unit/test_eval_review.py tests/integration/test_eval_review_workflow.py -q` -> exit 0 (`4 passed`).
- First `uv run ruff format --check .` -> exit 1 because `src/wikipediarag/eval/review.py` required formatting; `uv run ruff format src/wikipediarag/eval/review.py` -> exit 0 (`1 file reformatted`).
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`70 files already formatted`).
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 65 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`77 passed`).
