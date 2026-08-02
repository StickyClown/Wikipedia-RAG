# ExecPlan 31: Warm Retrieval P95 Profile

Status: implemented
Date: 2026-07-30

## Goal

Make warm retrieval latency measurable before expanding the retrieval surface. The profiler warms reviewed tasks, runs measured retrieval-only `search:debug` requests and reports p50/p95 for BM25, dense, fusion, rerank, context, retrieval total and total latency.

## Scope

- Added `eval-profile-retrieval`.
- The command uses locked reviewed dev/test splits, optional explicit task IDs, bounded batch size, warmup iterations and measured iterations.
- Reports are written under `artifacts/eval/retrieval-profiles/<suite>/<run_id>/report.json`, with latest pointers.
- Eval local auth now initializes thread-safely so concurrent retrieval profiling and release-gate retrieval runs reuse one cookie/CSRF session.

## Validation

```text
uv run pytest tests\unit\test_eval_retrieval_profile.py tests\unit\test_eval_api_client_auth.py -q
-> exit 0, 4 passed

uv run python -m wikipediarag.cli eval-profile-retrieval --suite reviewed-wikipedia-smoke-v1 --split dev --config-id sota_mvp_normal --limit 2 --warmup-iterations 1 --measured-iterations 1 --batch-size 2 --api http://localhost:8000
-> exit 0, measured completed=2 failed=0, retrieval_total p95=2798ms
```
