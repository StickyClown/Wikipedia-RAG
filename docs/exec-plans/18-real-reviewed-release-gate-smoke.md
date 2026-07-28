# ExecPlan 18 - Real reviewed release gate smoke

## 1. Outcome

ExecPlan 17 is functionally proven against a real trusted Wikipedia dataset by creating a reviewed smoke suite, freezing immutable reviewed `dev` and `test` slices, running the reviewed release gate through the local API, and recording exact command evidence.

## 2. Why this plan exists

`SPEC.md` and `docs/quality/EVALUATION_PLAN.md` require release gates to be exercised on reviewed evaluation slices. ExecPlan 17 added the review/freeze/release-gate commands and deterministic tests, but its final evidence did not include a real local API run over locked reviewed artifacts. This plan verifies that workflow end-to-end without changing existing trusted train artifacts.

## 3. In scope

- Use the existing latest `trusted-wikipedia-v2` dataset artifact when available.
- Create a separate smoke suite named `reviewed-wikipedia-smoke-v1`.
- Run `eval-review-candidates`, `eval-freeze-reviewed` with `dev-count=10` and `test-count=10`, and `eval-release-gate`.
- If the API is not running, start the existing Docker Compose stack without deleting volumes.
- Verify reviewed pool counts, locked manifest paths, locked row review statuses and repeated freeze reuse.
- Update `docs/STATUS.md` with exact commands, exit codes, hashes, paths and gate result.

## 4. Out of scope

- Generating a new trusted dataset if a real trusted JSONL artifact exists.
- Synthetic release-gate rows.
- Changing existing trusted train artifacts.
- Destructive Docker, database, volume or index commands.
- Overwriting locked artifacts with a different hash.
- Tuning retrieval, generation, citations or gate thresholds.

## 5. Preconditions

- ExecPlan 17 is complete.
- `trusted-wikipedia-v2/latest.json` points to a real local JSONL dataset.
- Local Docker Compose services can be started without removing volumes.
- OpenRouter/model gateway configuration is already present if answer-eval calls need real provider access.

## 6. Contracts and invariants

- `artifacts/eval/datasets/trusted-wikipedia-v2/*` is read-only for this plan.
- `reviewed-wikipedia-smoke-v1` is the only reviewed suite created or reused.
- Locked `test` is never overwritten with a different hash.
- All locked dev/test rows must have `review_status=reviewed`.
- A non-zero `eval-release-gate` exit is acceptable smoke evidence if the command emits machine-readable blocking reasons.

## 7. Milestones

### M18.1 Artifact and stack readiness

- Confirm latest trusted dataset path, count and hash.
- Confirm smoke suite has no conflicting locked artifacts.
- Start Docker Compose stack if API is down.

### M18.2 Reviewed smoke artifacts

- Run candidate review conversion.
- Run freeze once and then rerun freeze to prove reuse.
- Verify locked manifests and row statuses.

### M18.3 Release gate smoke

- Run `eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000`.
- Capture exit code, pass/fail result, blocking failures and run artifact paths.

### M18.4 Final validation and docs

- Run regression checks.
- Update `docs/STATUS.md`.
- Record final evidence in this ExecPlan.

## 8. Acceptance criteria

- Reviewed pool exists and reports 300 reviewed rows from the latest trusted dataset.
- Locked `dev` and `test` manifests exist under `artifacts/eval/datasets/reviewed-wikipedia-smoke-v1/locked/`.
- Locked `dev` and `test` JSONL each contain 10 reviewed rows.
- Re-running freeze with the same input reports reuse for both locked splits.
- Release gate command completes with either exit 0 or a JSON result containing blocking failures.
- Final Ruff, format, mypy and pytest checks pass.
- `docs/STATUS.md` reflects the real smoke outcome.

## 9. Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## 10. Demo

```bash
uv run python -m wikipediarag.cli eval-review-candidates --input artifacts/eval/datasets/trusted-wikipedia-v2/trusted-wikipedia-v2-5e698f31-09c0-0346-b23f-8a943b6646ea-8a1d43738f9b.jsonl --output-suite reviewed-wikipedia-smoke-v1
uv run python -m wikipediarag.cli eval-freeze-reviewed --suite reviewed-wikipedia-smoke-v1 --dev-count 10 --test-count 10
uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000
```

## 11. Rollback and recovery

Revert documentation changes from this plan if needed. The smoke artifacts under `artifacts/eval/datasets/reviewed-wikipedia-smoke-v1/`, `artifacts/eval/runs/reviewed-wikipedia-smoke-v1-*` and `artifacts/eval/retrieval-runs/reviewed-wikipedia-smoke-v1-*` may be deleted manually if the suite is no longer needed. Do not delete trusted source datasets, ZIM files, database volumes, OpenSearch indices or MinIO objects.

## 12. Progress

- [x] 2026-07-28: Plan created after confirming latest trusted dataset exists and API is initially down.
- [x] 2026-07-28: Reviewed smoke artifacts created and verified.
- [ ] 2026-07-28: Release gate smoke run completed.
- [ ] 2026-07-28: Final validation and status update completed.

## 13. Discoveries

- `eval-release-gate` has no live stage progress. A 20-row smoke can run long enough to look hung, even while answer/retrieval artifacts are being appended.
- The first direct `eval-release-gate` run timed out after 30 minutes during answer eval. A second run continued and reached test answer plus dev retrieval artifacts, but still had no operator-visible progress and was manually stopped.
- After interruption, two `eval-release-gate` process trees remained active. They were identified by command line and stopped explicitly.

## 14. Decision log

- Use `reviewed-wikipedia-smoke-v1` as a disposable proof suite so future production reviewed suites remain clean.
- Stop the current release-gate smoke attempt until long-running eval commands expose realtime progress/status. This prevents silent provider-backed work from continuing without observability.

## 15. Final evidence

- `docker compose up -d --build` -> exit 0; stack started without destructive volume/index commands.
- `GET http://localhost:8000/ready` -> exit 0, `{"status":"ok","components":{"postgres":"ok","model_gateway":"ok"}}`.
- `uv run python -m wikipediarag.cli eval-review-candidates --input artifacts/eval/datasets/trusted-wikipedia-v2/trusted-wikipedia-v2-5e698f31-09c0-0346-b23f-8a943b6646ea-8a1d43738f9b.jsonl --output-suite reviewed-wikipedia-smoke-v1` -> exit 0; reviewed pool hash `2d767e8afbb2d152813bb267aa58b38cc60d90799e92c1ec10ec269560c06645`, `300` reviewed rows.
- `uv run python -m wikipediarag.cli eval-freeze-reviewed --suite reviewed-wikipedia-smoke-v1 --dev-count 10 --test-count 10` -> exit 0; dev hash `e3ada32289a78dc98a79f936d058ac5707e997c9aab611060d8c4f04f28ea5de`, test hash `3fc3c61f23a6ee16930163ae8ab3d53ac4880365f3cdcab72542823e368d07f4`.
- Repeated `eval-freeze-reviewed` with the same arguments -> exit 0, `reused_locked=["dev","test"]`.
- Locked row verification -> dev `10` rows, test `10` rows, all `review_status=reviewed`, split values match their manifests.
- `uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000` -> timed out after 30 minutes without realtime progress.
- A second `eval-release-gate` run was interrupted and its remaining `uv/python` processes were stopped. Partial artifacts exist under `artifacts/eval/runs/reviewed-wikipedia-smoke-v1-*` and `artifacts/eval/retrieval-runs/reviewed-wikipedia-smoke-v1-dev/`.
