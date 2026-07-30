# ExecPlan 23 - Reviewed Gate Quality And Eval Semantics Closure

Status: implementation validated; provider-backed gate rerun is blocked by OpenRouter readiness.

## Summary

ExecPlan 23 fixes the reviewed-gate false negatives found after ExecPlan 22:

- stale answer summaries and mixed answer artifacts;
- brittle answerability hard stops and `и`-based question splitting;
- soft unanswerable scoring;
- bridge/deep routing for `trusted-wiki-000201`;
- hard-negative metrics that blocked on unused low-risk negatives;
- answer eval bounded backfill concurrency and transient provider retry.

The implementation is complete in deterministic validation. The final provider-backed `eval-release-gate` rerun was not started after the latest rebuild because readiness preflight failed: Model Gateway `/ready` reported `provider_http_402` for OpenRouter, API `/ready` was degraded, and `smoke-models --provider openrouter` failed.

## Progress

- Answer eval summary now aggregates latest result per `task_id`.
- Answer eval config hash includes answerability/eval semantics versions to avoid stale JSONL reuse.
- Answer eval uses bounded backfill concurrency, default `batch_size=6`.
- Answer eval retries transient `run.failed` / client exceptions up to 3 attempts per task.
- Release gate answer/retrieval stages run only `sota_mvp_normal`.
- `sota_mvp_normal` uses conditional extended search.
- Answerability is now `answerability_gate_v4`.
- Answerability no longer marks non-empty strong evidence as hard `UNANSWERABLE` for literal missing terms.
- Answerability no longer splits required parts on every Russian `и`.
- Bridge routing builds multi-hop subqueries for the `1040 -> 104 -> Рига` pattern.
- Extended search final evidence is selected round-robin across subquery steps, so later hops are not truncated by the first hop.
- Unanswerable scoring accepts clear soft refusals with contextual citations.
- `unanswerable_accuracy` is emitted only when the split contains unanswerable tasks.
- Legacy `false_positive_evidence_rate@20` is diagnostic-only.
- Blocking retrieval metric is now `dangerous_false_positive_evidence_rate`.
- Answer-side hard-negative usage is blocking only when the answer cites/uses hard-negative evidence.
- Short CLI diagnostics exist for reviewed task IDs.

## Discoveries

- `trusted-wiki-000006`: old failure was caused by `answerability_gate_v2` hard `UNANSWERABLE`; v4 reaches generation when evidence is non-empty and strong.
- `trusted-wiki-000201`: needs bridge/deep retrieval: identify film `1040`, map to series `104`, then answer `Рига`.
- `trusted-wiki-000297`: old retrieval metric was too harsh; hard-negative rank 4 with low rerank score is diagnostic-only unless cited.
- `trusted-wiki-000251`: soft refusal is correct when the source lacks commander info and nearby facts are labeled as contextual.
- Latest answer artifacts showed false zero recall for extended answers because answer eval parsed only top-level `rrf/rerank` events. Extended payloads can expose final sources via `retrieval.evidence`, so runner now falls back to `context/evidence`.
- Provider chat has produced transient `502 Bad Gateway`; eval runner retries answer tasks rather than treating a single provider failure as a permanent task failure.
- After the final rebuild, OpenRouter readiness changed from earlier `403` to `402`, which indicates account billing/quota/credits/access rather than a code path regression.

## Decision Log

- Keep `/ready` and `smoke-models --provider openrouter` as strict pre-gate requirements.
- Do not rerun provider-backed release gate while API `/ready` is degraded.
- Do not change reviewed datasets, model aliases, retrieval thresholds, or generation prompts in ExecPlan 23.
- Keep old `false_positive_evidence_rate` for diagnostics, but do not block on it.
- Count extra non-hard-negative bridge/context citations as supported when all gold chunks are cited.

## Validation

Focused deterministic tests:

```text
uv run pytest tests/integration/test_eval_runner.py tests/unit/test_eval_review.py tests/unit/test_eval_metrics.py tests/unit/test_answerability.py tests/unit/test_extended.py -q
-> exit 0, 33 passed
```

Full deterministic validation:

```text
uv run ruff check .
-> exit 0, All checks passed!

uv run ruff format --check .
-> exit 0, 71 files already formatted

uv run mypy src tests
-> exit 0, Success: no issues found in 69 source files

uv run pytest tests/unit tests/integration tests/e2e -q
-> exit 0, 113 passed, 4 warnings
```

Runtime rebuild:

```text
docker compose up -d --build model-gateway api worker
-> exit 0, model-gateway/api/worker rebuilt and started
```

Provider readiness after rebuild:

```text
curl.exe -sS http://localhost:8000/ready
-> exit 0, {"status":"degraded","components":{"postgres":"ok","model_gateway":"failed"}}

curl.exe -sS http://127.0.0.1:8081/ready
-> exit 0, {"status":"degraded","checks":[{"component":"openrouter.startup_smoke","status":"failed","reason":"provider_http_402"}]}

uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://127.0.0.1:8081
-> exit 1, model gateway aliases are unhealthy: ['embed_default', 'generator_fast', 'generator_main', 'rerank_default', 'verifier']
```

## Current Blocker

Provider-backed release gate is blocked by OpenRouter `provider_http_402`. The operator must check/restore the OpenRouter account billing/quota/credits/access for the configured key, then restart Model Gateway so startup smoke can refresh readiness.

Required rerun after provider fix:

```text
docker compose up -d --build model-gateway api worker
curl.exe -sS http://localhost:8000/ready
uv run python -m wikipediarag.cli smoke-models --provider openrouter --gateway http://127.0.0.1:8081
uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000
uv run python -m wikipediarag.cli eval-release-gate-status --suite reviewed-wikipedia-smoke-v1 --json
```

## Acceptance Status

- Implementation and deterministic tests: complete.
- Provider-backed gate: blocked by OpenRouter `402`.
- ExecPlan 21 cannot be marked complete until the latest gate status is `completed`, `passed=true`, `blocking_failures=0`.
