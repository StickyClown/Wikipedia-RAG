# ExecPlan 26 - Eval Root-Cause Diagnostics

Status: completed
Closed: 2026-07-30

## Goal

Add deterministic Legal RAG Bench-style root-cause diagnostics to the eval pipeline without adding LLM judges and without changing the runtime chat API.

## Delivered

- Added eval-only root-cause taxonomy:
  - `passed`
  - `retrieval_error`
  - `hallucination_or_unsupported`
  - `reasoning_error`
  - `hard_negative_attribution`
  - `unanswerable_false_positive`
  - `execution_error`
  - `not_evaluated`
- Added `diagnosis` to answer and retrieval task JSONL result rows.
- Added `root_cause_*_count` metrics to config summaries and per-family summaries.
- Added diagnosis payloads to `eval-reviewed-short`, `eval-task-diagnostics --json` and release-gate blocker task details.
- Kept the classifier deterministic: it uses existing recall, citation, unsupported-claim, unanswerable, hard-negative and exact-match/token-F1 signals only.

## Validation

```text
uv run ruff check src\wikipediarag\eval\diagnostics.py src\wikipediarag\eval\schemas.py src\wikipediarag\eval\runner.py src\wikipediarag\eval\retrieval_runner.py src\wikipediarag\eval\commands.py src\wikipediarag\eval\review.py tests\unit\test_eval_diagnostics.py tests\integration\test_eval_runner.py tests\unit\test_eval_retrieval_runner.py
-> exit 0

uv run mypy src\wikipediarag\eval\diagnostics.py src\wikipediarag\eval\schemas.py src\wikipediarag\eval\runner.py src\wikipediarag\eval\retrieval_runner.py src\wikipediarag\eval\commands.py src\wikipediarag\eval\review.py tests\unit\test_eval_diagnostics.py
-> exit 0

uv run pytest tests\unit\test_eval_diagnostics.py tests\unit\test_eval_metrics.py tests\integration\test_eval_runner.py tests\unit\test_eval_review.py -q
-> exit 0, 31 passed

uv run pytest tests\unit\test_eval_retrieval_runner.py -q
-> exit 0, 6 passed

uv run python -m wikipediarag.cli smoke-models --provider openrouter
-> exit 0

uv run python -m wikipediarag.cli eval-reviewed-short --suite reviewed-wikipedia-smoke-v1 --split dev --config-id sota_mvp_normal --task-id trusted-wiki-000018 --task-id trusted-wiki-000217 --task-id trusted-wiki-000251 --task-id trusted-wiki-000295 --batch-size 2 --retrieval-batch-size 2 --api http://localhost:8000
-> exit 0, all 4 answer tasks and all 4 retrieval tasks completed
```

Live short-run root causes:

```text
trusted-wiki-000018: answer=passed, retrieval=passed
trusted-wiki-000217: answer=passed, retrieval=passed
trusted-wiki-000251: answer=passed, retrieval=retrieval_error
trusted-wiki-000295: answer=reasoning_error, retrieval=passed
```

## Notes

- The live short run used the documented local/demo `AUTH_DISABLED=true` bypass because the eval HTTP client does not yet create a local-auth session. The API was restored to `AUTH_DISABLED=false` after validation.
- `reasoning_error` is diagnostic only and is not a release blocker by itself.
