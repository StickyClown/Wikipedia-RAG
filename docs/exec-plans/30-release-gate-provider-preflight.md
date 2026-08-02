# ExecPlan 30: Release Gate Provider Preflight

Status: implemented
Date: 2026-07-30

## Goal

Keep provider-backed release gates behind explicit runtime health checks. A gate must not start when the API readiness endpoint is degraded, and OpenRouter-backed gates must prove Model Gateway aliases are healthy before any reviewed gate rows run.

## Scope

- `eval-release-gate` keeps the existing API `/ready` preflight.
- When effective `MODEL_PROVIDER=openrouter`, `eval-release-gate` runs the strict OpenRouter `smoke-models` alias check through the configured Model Gateway URL before starting the gate.
- Mock/local non-OpenRouter gates skip the provider smoke and continue to use the existing release-gate checks.

## Non-Goals

- No release-gate scoring threshold changes.
- No direct OpenRouter calls from business code or eval orchestration.
- No long provider-backed gate rerun as part of this slice.

## Validation

```text
uv run pytest tests\unit\test_cli_release_gate.py -q
-> exit 0, 5 passed

uv run ruff check src\wikipediarag\cli.py tests\unit\test_cli_release_gate.py
-> exit 0, All checks passed!

uv run ruff format --check src\wikipediarag\cli.py tests\unit\test_cli_release_gate.py
-> exit 0, 2 files already formatted

uv run mypy src\wikipediarag\cli.py tests\unit\test_cli_release_gate.py
-> exit 0, Success: no issues found in 2 source files
```
