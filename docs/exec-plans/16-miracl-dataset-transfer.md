# ExecPlan 16 - MIRACL Russian dataset transfer

## 1. Outcome

MIRACL Russian retrieval questions can be converted into local-corpus-bound candidate artifacts with explicit binding and review status, without publishing them as locked tests or relying on Wikidata/langlink metadata.

## 2. Why this plan exists

`SPEC.md` and `docs/quality/EVALUATION_PLAN.md` call for external retrieval datasets, but the local ZIM import currently guarantees only Wikipedia title/redirect metadata. ExecPlan 15 added AnswerabilityGate v1. This plan adds a conservative transfer pipeline for MIRACL Russian only, so external questions can be reviewed against the exact local corpus before they become dev/test material in a later milestone.

## 3. In scope

- Introduce shared transfer schemas: `ExternalQuestion`, `CorpusBoundCandidate` and `TrustedTask`.
- Implement MIRACL Russian JSONL/TSV parsing into `ExternalQuestion`.
- Bind external gold titles to the current local corpus using exact title and confident redirect alias matches.
- Add binding statuses `EXACT`, `REDIRECT`, `AMBIGUOUS` and `MISSING`.
- Add decision statuses `AUTO_ACCEPT`, `REVIEW` and `REJECT`.
- Automatically accept only `EXACT` and confident `REDIRECT`; route `AMBIGUOUS` to `REVIEW` and `MISSING` to `REJECT`.
- Publish artifacts as train/dev candidates under ignored `artifacts/eval/`, not locked test data.

## 4. Out of scope

- Wikidata/QID/langlink matching.
- RusBEIR, HotpotQA or other external datasets.
- Human review UI or locked reviewed dev/test manifests.
- Release gate metrics over transferred datasets.
- Mutating existing trusted Wikipedia v2 locked artifacts.

## 5. Preconditions

- ExecPlan 15 is implemented.
- `eval-miracl-map` exists as the CLI entry point.
- Local corpus catalog can load canonical chunks and redirect aliases.

## 6. Contracts and invariants

- External transfer artifacts are candidates only: default `split=train` for auto-accepted rows, optional `split=dev` for review rows, and never `split=test`.
- `review_status` is `unreviewed` for candidates. Reviewed/rejected workflow is deferred to ExecPlan 17.
- Binding decisions must include the original external ID/query/title and local document/chunk IDs when present.
- No provider calls, Wikidata requests, or network downloads are allowed in this transfer step.
- Missing or ambiguous bindings must not be silently accepted.

## 7. Milestones

### M16.1 Transfer schemas and parser

- Add external dataset transfer models and MIRACL parser.
- Validation: unit tests for JSONL and TSV parsing.

### M16.2 Local corpus binding

- Bind exact and redirect titles from loaded corpus/catalog candidates.
- Validation: unit tests for `EXACT`, `REDIRECT`, `AMBIGUOUS` and `MISSING` decisions.

### M16.3 CLI artifact publishing and docs

- Update `eval-miracl-map` to write candidate JSONL plus manifest.
- Update contracts/status.
- Validation: focused CLI/pipeline tests, then final repo checks.

## 8. Acceptance criteria

- `eval-miracl-map --input <path>` writes JSONL rows with `external_question`, `binding_status`, `decision_status`, `split`, `review_status` and `trusted_task` when auto-accepted.
- `EXACT` and confident `REDIRECT` rows are `AUTO_ACCEPT`.
- `AMBIGUOUS` rows are `REVIEW`.
- `MISSING` rows are `REJECT`.
- No output row has `split=test`.
- The manifest records dataset source `miracl-ru`, snapshot, index version, retrieval profile hash and counts by binding/decision.

## 9. Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## 10. Demo

```bash
python -m wikipediarag.cli eval-miracl-map --input ./artifacts/eval/external-samples/miracl-ru.jsonl
```

Expected output points to an ignored candidate JSONL artifact and a manifest under `artifacts/eval/external/miracl-ru/`.

## 11. Rollback and recovery

Revert code and documentation changes from this plan. Candidate artifacts under `artifacts/eval/external/miracl-ru/` may be deleted if created. Do not delete ZIM data, PostgreSQL/OpenSearch/MinIO volumes, existing trusted datasets or retrieval reports.

## 12. Progress

- [x] 2026-07-27: Plan created after ExecPlan 15 focused validation.
- [x] 2026-07-27: Transfer schemas, parser and binding implemented.
- [x] 2026-07-27: CLI artifact publishing and tests implemented.
- [x] 2026-07-27: Final validation commands completed.

## 13. Discoveries

- Existing `eval-miracl-map` accepted MIRACL-like input but only emitted `needs_review` and `not_found`, without redirect/ambiguous status semantics.
- MIRACL rows in the wild can expose titles via `title`, `doc_title`, `positive_passages`, `relevant_docs` or simple TSV; the parser handles these forms without assuming QID metadata.

## 14. Decision log

- MIRACL transfer remains candidate-only; no locked `test` output is created in this milestone.
- Redirect acceptance requires a single redirect alias match to a local canonical chunk.
- A row with any missing required gold title is rejected instead of partially accepted; reviewed partial acceptance is deferred to ExecPlan 17.

## 15. Final evidence

- `uv run pytest tests/unit/test_eval_external.py -q` -> exit 0 (`4 passed`).
- `uv run ruff check src/wikipediarag/eval/external.py src/wikipediarag/eval/commands.py src/wikipediarag/eval/trusted.py tests/unit/test_eval_external.py` -> exit 0.
- `uv run mypy src/wikipediarag/eval/external.py src/wikipediarag/eval/commands.py src/wikipediarag/eval/trusted.py tests/unit/test_eval_external.py` -> exit 0.
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`67 files already formatted`) after `uv run ruff format .` reformatted 2 files.
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 62 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`73 passed`).
