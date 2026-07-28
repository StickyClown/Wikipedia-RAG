# Project Status

Last updated: 2026-07-28

## Current Phase

ExecPlan 22 Model Gateway readiness diagnostics are implemented and deterministic validation passed. In local compose `warn` mode, Model Gateway stays inspectable after OpenRouter startup smoke failure:

- `GET /health` is liveness-only and returns ok.
- `GET /ready` returns degraded with safe reason `provider_http_403`.
- `GET /v1/models` marks OpenRouter aliases unhealthy.
- API `/ready` propagates the degraded gateway as `components.model_gateway=failed`.

The provider-backed reviewed release gate was not rerun because strict OpenRouter smoke still fails on unhealthy OpenRouter aliases. Latest completed reviewed gate remains the ExecPlan 18/19 run that completed but did not pass quality gates.

## Active Blocker

OpenRouter provider/account access returns `403 Forbidden`. Until strict OpenRouter smoke passes, do not start `eval-release-gate` against the reviewed suite.

Required proof before rerun:

```bash
uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://localhost:8081
```

## Latest Deterministic Validation

ExecPlan 22 deterministic checks:

```text
uv run pytest tests/unit/test_gateway_app.py tests/unit/test_api_readiness.py tests/unit/test_cli_release_gate.py -q
-> exit 0, 7 passed

uv run ruff check .
-> exit 0

uv run ruff format --check .
-> exit 0, 73 files already formatted

uv run mypy src tests
-> exit 0, no issues in 69 source files

uv run pytest tests/unit tests/integration tests/e2e -q
-> exit 0, 98 passed
```

Runtime checks after rebuild:

```text
GET http://localhost:8081/health
-> exit 0, {"status":"ok"}

GET http://localhost:8081/ready
-> exit 0, {"status":"degraded","checks":[{"component":"openrouter.startup_smoke","status":"failed","reason":"provider_http_403"}]}

GET http://localhost:8000/ready
-> exit 0, {"status":"degraded","components":{"postgres":"ok","model_gateway":"failed"}}

uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://localhost:8081
-> exit 1, OpenRouter aliases unhealthy

uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000
-> exit 1 quickly, API is not ready; no provider-backed gate rerun started
```

## Latest Reviewed Gate

Suite: `reviewed-wikipedia-smoke-v1`

Latest status path:

```text
artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/reviewed-wikipedia-smoke-v1-release-gate/status.json
```

Latest completed status:

- state: `completed`
- passed: `false`
- blocking failures: `2`
- blocking test findings for `sota_mvp_normal`: `unanswerable_accuracy < 1.0`, `false_positive_evidence_rate > 0`

ExecPlan 22 added a readiness guard so this gate now refuses to start while API `/ready` is degraded.

## Local Data State

- Real ZIM pages imported: `10,000` canonical non-redirect pages.
- Real ZIM chunks indexed: `14,281`.
- OpenSearch index: `wiki-chunks-387df2fb225f794d`.
- Redirect provenance persisted for the local ZIM snapshot.

## Known Limitations

- OpenRouter-backed imports/evals incur provider time and cost.
- This Windows environment may not have GNU Make in `PATH`; use exact equivalent `uv`, `pnpm` and `docker compose` commands when needed.
- Production auth/tenant onboarding is not implemented; local MVP uses seeded/default tenant context.
- Universal PDF/Office/image ingestion is not production-ready.
- Local llama.cpp profile requires model artifacts, hardware sizing, licenses, checksums and quality gates.

## Next Step

Resolve OpenRouter provider/account access, rerun strict `smoke-models --provider openrouter`, confirm API `/ready` is ok, then rerun the reviewed release gate. If the gate still fails on quality metrics, create a narrow follow-up implementation plan instead of expanding the readiness work.
