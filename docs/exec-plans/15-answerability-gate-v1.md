# ExecPlan 15 - AnswerabilityGate v1

## 1. Outcome

Online chat and debug retrieval expose a deterministic answerability decision for the packed context, and chat generation uses that decision to answer, partially answer, or refuse without relying on a raw evidence-count threshold.

## 2. Why this plan exists

`SPEC.md` and `docs/architecture.md` require grounded answers with safe no-answer behavior. ExecPlan 14 made retrieval runs reproducible through index and run contracts, but the runtime still treated "enough chunks" as a proxy for answerability. This plan adds the explicit gate required before further Extended Search and external dataset transfer work.

## 3. In scope

- Add answer statuses `ANSWERABLE`, `PARTIAL`, `UNANSWERABLE` and `CONFLICTING`.
- Add a deterministic `AnswerabilityDecision` to retrieval results.
- Evaluate the gate after retrieval/context packing and after Extended Search context assembly.
- Use cheap v1 signals: title match, query key numbers/dates/names, top score, page diversity and decomposed part coverage.
- Trigger Extended Search only for `PARTIAL` or `UNANSWERABLE` decisions when the profile allows conditional/always Extended Search.
- Refuse without a provider call for `UNANSWERABLE` when Extended Search is unavailable, and handle `CONFLICTING` with a safe refusal/caveat.
- Update API, evaluation and domain contracts for additive answerability metadata.

## 4. Out of scope

- Full conflict resolver or cross-source truth arbitration.
- MIRACL/external dataset transfer.
- Reviewed dev/test locking and release gates.
- New database tables or migrations.
- Learned classifiers, provider calls or model-based answerability scoring.

## 5. Preconditions

- ExecPlan 14 is complete.
- Retrieval results already carry `index_contract_id` and `run_contract_id`.
- Active retrieval profiles include `answerability_gate_version` in the run contract.

## 6. Contracts and invariants

- The gate is deterministic and depends only on the normalized query, packed evidence and retrieval profile settings.
- `insufficient_evidence` remains as an additive compatibility field derived from the answerability status.
- `UNANSWERABLE` with no Extended Search must not call the generator provider.
- Answerability metadata must not include raw prompts, provider responses, secrets or unbounded document text.
- Tenant and KB filtering remain enforced by retrieval before the gate receives evidence.

## 7. Milestones

### M15.1 Gate model and deterministic evaluator

- Add answerability schemas and pure evaluator.
- Validation: unit tests for answerable, partial, unanswerable and conflicting fixtures.

### M15.2 Runtime integration

- Attach gate decisions in direct and Extended Search retrieval results.
- Switch Extended Search fallback from chunk-count to answerability status.
- Update generation refusal/caveat behavior.
- Validation: focused retrieval/answering/extended tests.

### M15.3 Contract documentation

- Update API/evaluation/domain/status docs with exact command results.
- Validation: lint, format, mypy and focused tests.

## 8. Acceptance criteria

- `/api/v1/search:debug` returns `answerability.status` and gate signals.
- `/api/v1/chat` usage includes the same answerability payload.
- `ANSWERABLE` proceeds to normal generation.
- `PARTIAL` proceeds to generation with an explicit partial-answer instruction.
- `UNANSWERABLE` refuses without a provider call when Extended Search is off.
- `CONFLICTING` returns a safe refusal/caveat in v1.
- Extended Search fallback runs only for `PARTIAL` or `UNANSWERABLE` when profile policy is `conditional` or `always`.

## 9. Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/unit tests/integration tests/e2e -q
```

## 10. Demo

```bash
python -m wikipediarag.cli smoke --question "Что такое Россия?"
curl -s http://localhost:8000/api/v1/search:debug -H "content-type: application/json" -d "{\"message\":\"Что такое Россия?\"}"
```

Expected debug output includes top-level `answerability.status`.

## 11. Rollback and recovery

Revert code and documentation changes from this plan. Do not delete Docker volumes, imported ZIM/XML data, OpenSearch indices or eval artifacts. No data migration is introduced by this plan.

## 12. Progress

- [x] 2026-07-27: Plan created after container restart.
- [x] 2026-07-27: Gate model and evaluator implemented.
- [x] 2026-07-27: Runtime generation and Extended Search behavior updated.
- [x] 2026-07-27: Final validation commands completed.

## 13. Discoveries

- Existing runtime computed `insufficient_evidence` in `retrieval.py` and `extended.py` from `len(evidence) < final_evidence_min`.
- The comparison fixture exposed a morphology/attribute pitfall: `Канаду по площади` must not count as covered only because evidence contains `площадь`; a named part requires name coverage.

## 14. Decision log

- Keep `insufficient_evidence` for compatibility, but derive it from gate status instead of evidence count.
- `CONFLICTING` v1 is conservative: it requires explicit conflict wording plus divergent numeric/date values across evidence.

## 15. Final evidence

- `uv run pytest tests/unit/test_answerability.py tests/unit/test_retrieval_answering.py tests/unit/test_extended.py -q` -> exit 0 (`15 passed`).
- `uv run ruff check src/wikipediarag/answerability.py src/wikipediarag/answering.py src/wikipediarag/api_app.py src/wikipediarag/retrieval.py src/wikipediarag/extended.py tests/unit/test_answerability.py tests/unit/test_retrieval_answering.py` -> exit 0.
- `uv run mypy src/wikipediarag/answerability.py src/wikipediarag/answering.py src/wikipediarag/api_app.py src/wikipediarag/retrieval.py src/wikipediarag/extended.py tests/unit/test_answerability.py tests/unit/test_retrieval_answering.py tests/unit/test_extended.py` -> exit 0.
- `uv run ruff check .` -> exit 0.
- `uv run ruff format --check .` -> exit 0 (`67 files already formatted`) after `uv run ruff format .` reformatted 2 files.
- `uv run mypy src tests` -> exit 0 (`Success: no issues found in 62 source files`).
- `uv run pytest tests/unit tests/integration tests/e2e -q` -> exit 0 (`73 passed`).
