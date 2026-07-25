# ExecPlan 09 — Evaluation platform and release gates

## Outcome

Pipeline, model, template and index changes are compared reproducibly and blocked from publication when critical quality, security or SLO thresholds regress.

## In scope

- evaluation dataset/version model;
- machine-readable runner and reports;
- retrieval, answer and operational metrics;
- ablation configuration;
- regression budgets;
- release-gate command in CI;
- report export and status UI/API.

## Out of scope

Automatically treating a same-model LLM judge as the only quality authority.

## Acceptance criteria

- reports identify dataset, snapshot, pipeline, model, template and index versions;
- deterministic metrics are reproducible;
- critical regression causes non-zero CI exit;
- human-reviewed cases can override only with recorded justification;
- rollback target is named before new index/model publication.

## Validation

```bash
make eval EVAL_SET=release
make release-gate
```

## Progress

- [x] Plan refined for local MVP.
- [x] Implemented mini eval and release-gate command.
- [x] Reviewed with local checks.

## MVP status

- Implemented `make eval` and `make release-gate` wrappers backed by deterministic local API checks.
- Verified `uv run python -m wikipediarag.cli release-gate` exits 0 after import.

## Remaining production work

- Versioned datasets, regression budgets, ablations, persisted evaluation runs and CI publication gates remain future hardening.
