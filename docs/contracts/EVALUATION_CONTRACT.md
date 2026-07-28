# Evaluation contract

Status: local JSONL evaluation v1

## Commands

The local ZIM evaluation surface is CLI-first:

```bash
python -m wikipediarag.cli eval-smoke --count 10
python -m wikipediarag.cli eval-generate --count 150
python -m wikipediarag.cli eval-generate --count 20 --concurrency 10 --generator-alias generator_main --verifier-alias verifier --family-weight comparison_multi_hop=1 --run-id demo-run
python -m wikipediarag.cli eval-generate-status --latest
python -m wikipediarag.cli eval-generate-status --run-id demo-run --json
python -m wikipediarag.cli eval-generate --resume-run-id demo-run
python -m wikipediarag.cli eval-run --suite generated-wikipedia-v1
python -m wikipediarag.cli eval-report --latest
python -m wikipediarag.cli eval-retrieval-run --suite generated-wikipedia-v1 --batch-size 10
python -m wikipediarag.cli eval-retrieval-status --latest
python -m wikipediarag.cli eval-retrieval-report --latest
python -m wikipediarag.cli eval-trusted-catalog
python -m wikipediarag.cli eval-trusted-generate --count 300 --rejection-budget 30 --run-id trusted-v2-seed
python -m wikipediarag.cli eval-trusted-status --run-id trusted-v2-seed
python -m wikipediarag.cli eval-trusted-generate --resume-run-id trusted-v2-seed
python -m wikipediarag.cli eval-trusted-pool --suite trusted-wikipedia-v2 --batch-size 10
python -m wikipediarag.cli eval-trusted-report --suite trusted-wikipedia-v2
python -m wikipediarag.cli eval-miracl-map --input <miracl_ru_path>
python -m wikipediarag.cli eval-review-candidates --input <candidate_jsonl> --output-suite <suite>
python -m wikipediarag.cli eval-freeze-reviewed --suite <suite> --dev-count 20 --test-count 20
python -m wikipediarag.cli eval-release-gate --suite <suite> --api http://localhost:8000
python -m wikipediarag.cli eval-release-gate-status --suite <suite> --json
```

`eval-smoke --count 10` is the gate for generation. `eval-generate` refuses to run unless the latest successful smoke marker has the same `snapshot_id`, `index_version`, ZIM checksum and retrieval profile hash, and was produced with at least 10 tasks.

`eval-generate` and the generation stage inside `eval-full` accept runtime overrides from CLI:

- `--count N`
- `--concurrency N`
- `--generator-alias <alias>`
- `--verifier-alias <alias>`
- repeatable `--family-weight family=weight`
- `--run-id <id>`
- `--resume-run-id <id>`

Generator and verifier aliases must already exist in `config/models.yaml` and must resolve through the model registry as `operation=chat`. Concurrency is validated explicitly and is not silently clamped.

If no `--family-weight` flags are provided, generation uses the default mix. If at least one `--family-weight` is provided, omitted families are treated as weight `0`. The normalized `family_targets` always sum exactly to `--count`.

`eval-generate` and `eval-full` stream operator-visible progress to stdout with immediate flush. Each emitted line includes elapsed time, task family, family `accepted/target`, total `accepted/target`, attempt number and the safe outcome for that attempt. Every successfully parsed candidate also prints its generated question; accepted tasks are marked separately.

Only safe rejection reasons are emitted: `invalid_generator_json`, `verifier_rejected`, `invalid_verifier_json`, `local_validation_rejected` and `provider_error`. Prompts, evidence packets, raw provider bodies and exception text are not written to progress output.

`eval-trusted-generate` also streams operator-visible progress to stdout with immediate flush. Each line includes elapsed time, active trusted family, family `accepted/target`, total `accepted/target`, `rejected/rejection_budget`, attempt number and safe reason/question metadata when applicable. The default rejection budget is `30` for the whole run, including resume. When `--resume-run-id` is used without `--count`, the stored checkpoint count is reused. The final trusted dataset JSONL is published only when exactly `--count` tasks have been accepted; rejected candidates are not counted as tasks. If the budget is exhausted first, the run remains checkpointed and fails with `rejection_budget_exhausted`.

Trusted generation uses a run lock under `trusted-runs/<run_id>/run.lock`. A second generate/resume for an active run fails with the owner PID. A stale lock can be taken only with `--takeover-stale-run` after the operator confirms no generator process is active.

`eval-retrieval-run` is retrieval-only. It calls `POST /api/v1/search:debug` with `top_k=20`, stores one JSONL row per task/config and does not call `/api/v1/chat`, answer generation, generator aliases, verifier aliases or citation validation. `--batch-size N` is a bounded in-flight concurrency limit for each supported config: at most `N` search requests run at once, and when one finishes the next eligible task is scheduled immediately. Stdout progress includes elapsed time, config ID, batch `N/M`, task `i/total`, current task ID, last latency, rolling average and ETA.

Evaluation result `latency_ms` dictionaries include `total`, legacy `retrieval` where available and any safe timing keys returned by the API, such as `retrieval_total`, `bm25`, `dense_total`, `dense_embedding`, `dense_search`, `fusion`, `rerank`, `context`, `extended_search_total`, `generation_total`, `model_chat`, `answer_parse` and `citation_validation`. Reports aggregate observed stage timing keys as `stage_latency_<key>_p50_ms` and `stage_latency_<key>_p95_ms`, and render them in a separate Stage Timings section.

Evaluation task result rows preserve API-returned `index_contract_id` and `run_contract_id` under additive `contract_ids`. Config summaries aggregate the unique contract IDs seen for that config and report `mixed_contract_ids:<key>` if one config result set contains more than one ID. Markdown and JSON reports include a Contracts section/payload for operator review.

API-returned `answerability` decisions are additive evaluation metadata. Retrieval-only results preserve the decision returned by `/api/v1/search:debug`; answer runs preserve the decision in the chat `usage.updated.data.retrieval` payload and the derived generation validation field `answerability_status`. Evaluators must not treat evidence count alone as answerability.

`eval-retrieval-status` reads persisted retrieval status while a run is active or after it completes. `eval-retrieval-run --resume-run-id <id>` skips completed rows for the same run; `--rerun-failed` retries failed rows while preserving prior JSONL history.

`eval-run` and the answer-evaluation stages inside `eval-release-gate` write answer run status under `artifacts/eval/runs/<suite>/<run_id>/status.json` and append lifecycle events under `logs/events.jsonl`. Stdout progress includes elapsed time, config ID, task ID, processed/total counts, failures, last latency, rolling average and ETA. It does not print full questions, prompts, evidence packets or provider responses.

`eval-miracl-map` is the only external dataset transfer command in v1 and is limited to MIRACL Russian. It parses JSONL or TSV rows into `ExternalQuestion`, binds external gold titles to the local ZIM corpus, and writes `CorpusBoundCandidate` rows. Binding statuses are:

- `EXACT`: all gold titles bind directly to one local canonical title.
- `REDIRECT`: at least one gold title binds through a single local redirect alias and no title is missing or ambiguous.
- `AMBIGUOUS`: a title maps to more than one local document and requires review.
- `MISSING`: no usable local title binding exists or one required gold title is missing.

Decision statuses are `AUTO_ACCEPT`, `REVIEW` and `REJECT`. Only `EXACT` and confident `REDIRECT` are `AUTO_ACCEPT`; `AMBIGUOUS` is `REVIEW`; `MISSING` is `REJECT`. Output rows are candidates only: auto-accepted rows use `split=train`, review rows use `split=dev`, every row has `review_status=unreviewed`, and no row may use `split=test`.

`eval-review-candidates` is the first file-based review workflow. It reads trusted or external candidate JSONL and writes a reviewed pool under `artifacts/eval/datasets/<suite>/`. `AUTO_ACCEPT` rows become `review_status=reviewed`; `REVIEW` rows remain `review_status=unreviewed` unless a manually edited file already marks them `reviewed`; `REJECT` rows become `review_status=rejected`. Reviewed rows preserve provenance fields including `snapshot_id`, `index_version`, `zim_checksum`, `retrieval_profile_hash`, optional `index_contract_id`, optional `run_contract_id` and a source dataset hash. Existing trusted train artifacts are not rewritten.

`eval-freeze-reviewed` selects only `review_status=reviewed` rows from the reviewed pool. Selection is deterministic for the same pool hash, task ID and question. The command fails if there are fewer reviewed rows than `dev-count + test-count`; it never fills from unreviewed rows. It writes immutable manifests and JSONL files under:

```text
artifacts/eval/datasets/<suite>/locked/dev.jsonl
artifacts/eval/datasets/<suite>/locked/dev.manifest.json
artifacts/eval/datasets/<suite>/locked/test.jsonl
artifacts/eval/datasets/<suite>/locked/test.manifest.json
```

If a locked manifest already exists with the same suite, split, snapshot and dataset hash, freeze reuses it. If the locked path exists with another hash or snapshot, freeze fails without overwrite.

`eval-release-gate` runs answer and retrieval evaluation only on locked reviewed `dev` and `test` rows. `test` failures are blocking; `dev` failures are diagnostic. Default v1 checks fail on mixed contract IDs, `citation_precision < 1.0`, `unsupported_claim_rate > 0`, `unanswerable_accuracy < 1.0`, retrieval false-positive evidence, recall/MRR/nDCG regression greater than `0.03` by family when a locked dev baseline is present, and p95 latency growth greater than `25%` when a locked dev baseline is present.

`eval-release-gate` writes top-level status under `artifacts/eval/release-gates/<suite>/<suite>-release-gate/status.json`, updates `artifacts/eval/release-gates/<suite>/latest-status.json`, appends release-gate events under `logs/events.jsonl`, and streams flushed stage progress for `dev_answer`, `dev_retrieval`, `test_answer`, `test_retrieval` and `gate_evaluation`. The final JSON includes `timings_ms`, child run manifests and `release_gate_run`. `eval-release-gate-status --suite <suite>` reads the latest top-level status without starting evaluation or modifying locked datasets.

## Storage

Evaluation artifacts are stored under `artifacts/eval/`, which is ignored by Git:

- `smoke/` stores smoke tasks, results, reports and latest successful marker;
- `datasets/generated-wikipedia-v1/` stores versioned dataset JSONL and manifest;
- `generate-runs/<run_id>/status.json` stores the atomically updated runtime snapshot for an active or finished generation run;
- `generate-runs/<run_id>/accepted.partial.jsonl` stores accepted tasks for status inspection and resume;
- `runs/generated-wikipedia-v1/<run_id>/` stores one result JSONL per config hash;
- `reports/` stores generated Markdown and JSON answer-eval reports;
- `retrieval-runs/<suite>/<run_id>/status.json` stores the atomically updated retrieval-only runtime snapshot;
- `retrieval-runs/<suite>/<run_id>/results/<config_id>-<config_hash>.jsonl` stores retrieval-only task results;
- `retrieval-runs/<suite>/<run_id>/logs/events.jsonl` stores retrieval run, batch and task lifecycle events;
- `retrieval-reports/` stores retrieval-only Markdown and JSON reports.
- `release-gates/<suite>/<run_id>/status.json` stores the top-level reviewed release gate runtime snapshot;
- `release-gates/<suite>/<run_id>/logs/events.jsonl` stores release gate stage and child-run lifecycle events;
- `trusted-catalog/` stores parser-aware ZIM/chunk catalog JSONL and manifest artifacts;
- `trusted-runs/<run_id>/status.json` stores trusted generation state;
- `trusted-runs/<run_id>/run.lock` stores the active trusted generation owner PID;
- `trusted-runs/<run_id>/events.jsonl` stores trusted generation lifecycle events;
- `trusted-runs/<run_id>/accepted.partial.jsonl` stores accepted trusted tasks immediately after acceptance;
- `trusted-runs/<run_id>/rejected.jsonl` stores safe rejection reasons without prompts or raw provider payloads;
- `datasets/trusted-wikipedia-v2/` stores final train-only trusted task JSONL and manifest;
- `trusted-reports/` stores trusted dataset coverage reports.
- `external/miracl-ru/` stores MIRACL Russian transfer candidate JSONL artifacts, manifests and latest pointers. These are not locked dev/test artifacts.
- `datasets/<suite>/reviewed-pool-*.jsonl` and matching manifests store file-reviewed candidate pools.
- `datasets/<suite>/locked/` stores immutable reviewed `dev` and `test` JSONL/manifests used by release gates.

No evaluation tables are added in v1. PostgreSQL is read only for corpus/gold catalog construction and candidate enrichment.

The persisted generation status contains the resolved generator/verifier metadata, runtime targets, rejection budget, snapshot/index/profile identifiers, counters, current phase, current attempt, accepted question summaries, safe rejection summaries and the latest update timestamp. `eval-generate-status` and `eval-trusted-status` read only these persisted artifacts, so they can be called safely while another process is still generating.

Resume is allowed only when the current corpus snapshot matches the stored `snapshot_id`, `index_version`, ZIM checksum and retrieval profile hash, and the requested runtime overrides resolve to the same model aliases, rejection budget and `family_targets`.

Generation still writes the canonical dataset JSONL and manifest only after the full target count succeeds. The partial checkpoint is operational state for observability and resume, not a published dataset.

## Dataset JSONL

Each task stores:

```text
task_id
question
task_family
reference_answer
accepted_answers
unanswerable
expected_mode
gold_page_ids
gold_section_ids
gold_chunk_ids
gold_evidence
reasoning_path
generator_alias
verifier_alias
zim_checksum
snapshot_id
index_version
retrieval_profile_hash
```

Gold page IDs are canonical ZIM document IDs. Gold section IDs are `parent_chunk_id` values. Gold chunk IDs are child chunk IDs.

## Trusted Wikipedia v2 JSONL

`trusted-wikipedia-v2` rows keep the base task fields above for compatibility with existing runners and add parser-aware fields:

```text
trusted_family
source_spans
structural_element
answer_type
verification_results
negative_candidates
provenance
split
review_status
```

All records in this iteration are `split=train` and `review_status=unreviewed`. Human review, dev splits and locked tests are explicitly deferred. Local deterministic checks verify span presence, span/chunk binding, hard-negative/gold disjointness and multi-hop coverage before final dataset publication. Production trusted candidates, including hard-negative tasks, are created through the configured Model Gateway generator alias; deterministic candidates are only for explicit mock aliases in tests/local demos.

`eval-trusted-generate` writes partial state incrementally. It updates `status.json` atomically, appends lifecycle events to `events.jsonl`, appends accepted tasks to `accepted.partial.jsonl` immediately and writes rejected attempts to `rejected.jsonl` with safe reasons only. Trusted safe reasons include `invalid_generator_json`, `provider_error`, `negative_gold_overlap`, `answer_leak`, `duplicate_question`, `missing_source_span`, `invalid_multi_hop` and fallback `local_validation_rejected`. `--resume-run-id` is accepted only when the current snapshot ID, index version, ZIM checksum, retrieval profile hash, model aliases, count, rejection budget and family targets match the stored run.

## Run Isolation

`eval-run` compares six configs on the same dataset. Each config writes a separate JSONL keyed by `config_hash`; metrics are never mixed across configs. Re-running the same dataset/config reuses completed task results.

`eval-retrieval-run` compares the retrieval-only subset on the same dataset and writes separate JSONL files keyed by `config_hash`. `sota_mvp_conditional_harness` is marked `unsupported` for retrieval-only runs because `search:debug` does not execute the conditional chat harness; use `eval-run` for harness evaluation.

## API Boundary

The evaluator calls the existing RAG API for answer runs and does not call retrieval internals for evaluated answers. Direct DB reads are allowed only for:

- selecting/generating gold tasks;
- resolving candidate chunk IDs to document/section IDs for scoring.

The retrieval-only evaluator calls the existing `/api/v1/search:debug` API and reads PostgreSQL only to enrich returned `chunk_id` values with document and section IDs for gold matching.

The existing RAG pipeline is not modified by this contract.
