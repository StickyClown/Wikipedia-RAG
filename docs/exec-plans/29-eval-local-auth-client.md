# ExecPlan 29 - Eval Local Auth Client

Status: implemented
Updated: 2026-07-30

## Goal

Let local eval and reviewed short runs authenticate through normal local auth instead of requiring `AUTH_DISABLED=true`.

## Delivered

- Added eval auth settings:
  - `EVAL_AUTH_MODE=local|none`
  - `EVAL_AUTH_USERNAME`
  - `EVAL_AUTH_PASSWORD`
- `HttpEvalApiClient` now keeps one persistent `httpx.Client`, performs lazy local login, fetches CSRF from `/api/v1/auth/session`, and reuses cookies/CSRF for chat and debug search.
- Answer eval, retrieval eval, eval smoke and reviewed short commands construct the HTTP client from `Settings`.
- `auth_mode=none` remains available for explicitly unauthenticated/local-bypass scenarios.

## Validation

```text
uv run pytest tests\unit\test_eval_api_client_auth.py tests\unit\test_acl_mirroring_metadata.py -q
-> exit 0, 5 passed

uv run ruff check src\wikipediarag\eval\api_client.py src\wikipediarag\eval\runner.py src\wikipediarag\eval\retrieval_runner.py src\wikipediarag\eval\commands.py src\wikipediarag\config.py tests\unit\test_eval_api_client_auth.py
-> exit 0

uv run mypy src\wikipediarag\eval\api_client.py src\wikipediarag\eval\runner.py src\wikipediarag\eval\retrieval_runner.py src\wikipediarag\eval\commands.py src\wikipediarag\config.py tests\unit\test_eval_api_client_auth.py
-> exit 0
```

## Remaining

- Live reviewed-short smoke in normal `AUTH_DISABLED=false` mode.
- Release-gate OpenRouter alias smoke as a strict preflight.
