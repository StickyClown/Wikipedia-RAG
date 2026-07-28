# ExecPlan 19 - Observable release gate runs

## 1. Outcome

Long-running reviewed release gate runs expose realtime stage progress, child runner progress, elapsed timings and inspectable status artifacts so operators can tell whether the command is active, failed or stuck.

## 2. Why this plan exists

ExecPlan 18 proved reviewed smoke artifact creation but stopped before release-gate completion because `eval-release-gate` ran provider-backed answer evaluation without live progress. `SPEC.md` and `docs/quality/EVALUATION_PLAN.md` require machine-readable evaluation gates; this plan adds the missing operational observability without changing release-gate scoring.

## 3. In scope

- Add answer eval status/progress/events for `run_suite`.
- Add top-level release gate status/progress/timings for `eval-release-gate`.
- Reuse retrieval runner progress inside release gate.
- Add `eval-release-gate-status --suite <name>`.
- Update evaluation contract and project status.

## 4. Out of scope

- Changing release gate thresholds or metrics.
- Re-running the real reviewed smoke gate before observability exists.
- Generating or modifying trusted train datasets.
- Adding a human review UI.
- Destructive Docker, database, volume or index commands.

## 5. Preconditions

- ExecPlan 17 reviewed workflow exists.
- ExecPlan 18 smoke suite artifacts exist or can be created independently.
- Existing retrieval runner status/progress remains compatible.

## 6. Contracts and invariants

- Progress output must be flushed immediately.
- Progress/status must not print full questions, prompts, evidence packets, provider responses or document text.
- Status artifacts live under `artifacts/eval/` and are not committed.
- Existing callers of `run_suite` remain source-compatible.
- Release gate pass/fail semantics remain unchanged.

## 7. Milestones

### M19.1 Answer runner observability

- Add answer run `status.json`, `logs/events.jsonl` and progress callback.
- Include run/config/task counters, elapsed time, ETA, last latency and failure state.
- Validate with deterministic unit/integration tests.

### M19.2 Release gate observability

- Add top-level release gate status, events, stage timings and CLI reporter.
- Wire answer and retrieval child callbacks into release-gate progress.
- Add status loader command.

### M19.3 Documentation and validation

- Update `docs/contracts/EVALUATION_CONTRACT.md` and `docs/STATUS.md`.
- Run focused tests, Ruff, format check, mypy and full pytest.

## 8. Acceptance criteria

- `run_suite` writes answer run `status.json` and `logs/events.jsonl`.
- `eval-release-gate` prints stage progress before and during child runs.
- `eval-release-gate-status --suite <name>` returns latest machine-readable status.
- Final release-gate JSON contains `timings_ms` and run paths.
- Tests cover completed and failed status paths.
- Required validation commands pass.

## 9. Validation commands

```bash
uv run pytest tests/unit/test_eval_review.py tests/integration/test_eval_runner.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## 10. Demo

```bash
uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000
uv run python -m wikipediarag.cli eval-release-gate-status --suite reviewed-wikipedia-smoke-v1 --json
```

## 11. Rollback and recovery

Revert code and docs from this plan. Status artifacts under `artifacts/eval/runs/*/status.json`, `artifacts/eval/runs/*/logs/` and `artifacts/eval/release-gates/` may be deleted manually. Do not delete locked reviewed datasets, trusted datasets, ZIM files or service volumes.

## 12. Progress

- [x] 2026-07-28: Plan created and implementation started.
- [x] 2026-07-28: Answer runner observability implemented with status, events and callback progress.
- [x] 2026-07-28: Release gate observability implemented with top-level status, events, child progress and timings.
- [x] 2026-07-28: Validation and docs completed.

## 13. Discoveries

- Retrieval runner already had persisted status and CLI reporting, but its internal event label included the full question text. ExecPlan 19 changed that event label to task-only progress so release-gate progress does not expose raw questions.
- Answer `run_suite` reused completed result rows but had no persisted runtime state; the new status derives initial processed/completed counts from existing result JSONL rows.

## 14. Decision log

- Use task IDs, config IDs and counters in progress output; omit raw questions to avoid leaking prompt/data content.
- Keep release gate run ID deterministic as `<suite>-release-gate` so `eval-release-gate-status --suite` has a stable latest status path.
- Preserve release gate scoring exactly and add `timings_ms` / `release_gate_run` as additive final JSON fields.

## 15. Final evidence

- `uv run pytest tests/unit/test_eval_review.py tests/integration/test_eval_runner.py -q` -> exit 0 (`6 passed`).
- `uv run ruff check src/wikipediarag/eval/schemas.py src/wikipediarag/eval/runner.py src/wikipediarag/eval/retrieval_runner.py src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_runner.py` -> exit 0.
- `uv run ruff format --check src/wikipediarag/eval/schemas.py src/wikipediarag/eval/runner.py src/wikipediarag/eval/retrieval_runner.py src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_runner.py` -> exit 0 (`8 files already formatted`).
- `uv run mypy src/wikipediarag/eval/schemas.py src/wikipediarag/eval/runner.py src/wikipediarag/eval/retrieval_runner.py src/wikipediarag/eval/review.py src/wikipediarag/eval/commands.py src/wikipediarag/cli.py tests/unit/test_eval_review.py tests/integration/test_eval_runner.py` -> exit 0.
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`70 files already formatted`).
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 65 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`79 passed`).
