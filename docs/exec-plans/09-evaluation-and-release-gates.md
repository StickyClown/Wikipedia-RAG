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

- [ ] Plan refined.
- [ ] Implemented.
- [ ] Reviewed.
