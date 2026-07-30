# ExecPlan 22 - Model Gateway Readiness And Gate Closure

Status: completed for infrastructure readiness and provider-backed gate rerun.

## Summary

Model Gateway startup provider failures are now diagnosable without hiding the process in local `warn` mode, API readiness depends on gateway readiness, and the reviewed release gate was rerun after OpenRouter smoke passed.

The infrastructure blocker is closed. The latest gate result is a completed quality failure, not an OpenRouter/readiness failure, so ExecPlan 21 still cannot be marked complete.

## Implementation Changes

- Added Model Gateway liveness/readiness separation: `/health` remains liveness-only and `/ready` reports provider readiness with safe reasons.
- Added `MODEL_GATEWAY_STARTUP_SMOKE=required|warn|off`; local compose defaults to `warn`.
- API `/ready` now uses Model Gateway `/ready`, and release gate refuses to start while API readiness is degraded.
- `smoke-models --provider openrouter` remains the required pre-gate provider proof.
- Answer eval now uses bounded backfill concurrency with default `batch_size=6`; release-gate answer stages use `6`, retrieval stages remain `10`.
- Progress reporters tolerate closed stdout/stderr pipes so long provider-backed runs do not fail from terminal timeout.

## Commands And Results

Preflight before runtime gate:

```text
GET http://localhost:8000/ready
-> exit 0, {"status":"ok","components":{"postgres":"ok","model_gateway":"ok"}}

GET http://localhost:8081/ready
-> exit 0, {"status":"ok","checks":[]}

Open eval-release-gate process check
-> exit 0, no existing eval-release-gate process for reviewed-wikipedia-smoke-v1

uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://localhost:8081
-> exit 0, aliases returned, embedding_dimensions=1024, typed_json.ok=true, rerank returned 2 results
```

Validation:

```text
uv run ruff check .
-> exit 0, All checks passed!

uv run ruff format --check .
-> exit 0, 71 files already formatted

uv run mypy src tests
-> exit 0, Success: no issues found in 69 source files

uv run pytest tests/unit tests/integration tests/e2e -q
-> exit 0, 99 passed, 4 warnings
```

Runtime gate:

```text
uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000
-> completed provider-backed run, status completed, passed=false, blocking_failures=4
```

## Gate Result

Latest status:

```text
state=completed
passed=false
blocking_failures=4
started_at=2026-07-28T18:59:33Z
updated_at=2026-07-28T19:16:14Z
elapsed=00:16:41
```

Stage timings:

```text
dev_answer: 190112 ms
dev_retrieval: 867 ms
test_answer: 644897 ms
test_retrieval: 165039 ms
gate_evaluation: 2 ms
total: 1001003 ms
```

Blocking findings:

```text
test answer sota_mvp_normal citation_precision=0.805 -> citation_precision < 1.0
test answer sota_mvp_normal unsupported_claim_rate=0.195 -> unsupported_claim_rate > 0
test answer sota_mvp_normal unanswerable_accuracy=0.0 -> unanswerable_accuracy < 1.0
test retrieval sota_mvp_normal false_positive_evidence_rate=0.005 -> false positive evidence > 0
```

## Decision Log

- OpenRouter `403` is treated as provider/account access failure, not as a reason to hide gateway diagnostics.
- Release gate must only start after API `/ready` is `ok` and OpenRouter smoke passes.
- ExecPlan 22 stops at readiness and gate rerun. Remaining failures are quality metrics and belong to a narrow follow-up plan.

## Follow-Up

Create and execute a separate quality-focused ExecPlan for the remaining reviewed gate metrics. ExecPlan 21 can be marked complete only after a later gate status is `completed`, `passed=true`, and `blocking_failures=0`.
