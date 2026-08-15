# Project Status

Last updated: 2026-08-15

Detailed history through this date is archived in
[STATUS-2026-08-15.md](history/STATUS-2026-08-15.md).

## Current Goal

P0.1 search-quality workflow is closed. Its controlled RRNCB result is retained
as a document-retrieval reference baseline; evidence-level retrieval evaluation
and changes to the regression policy are deferred to a future task. The
configured OpenRouter aliases are valid model endpoints; a local model runtime
is not a prerequisite.

## Current State

- API, worker, PostgreSQL, OpenSearch, MinIO, parser services and Model Gateway
  returned `ok`; the active Gateway revision resolves the required operations.
- Direct upload accepts the typed `source_ref_v1` identity contract. Server-owned
  `(tenant, KB, namespace, external_id)` identity controls document reuse and
  version creation.
- `source_provenance_v1` is projected through document, search, answer and
  Extended/Deep Research evidence paths. User attributes cannot override
  identity, ACL, storage, checksum or filename fields.
- The signed runtime-binding v2 builder rejects stale, tampered, unresolved and
  ambiguous evidence. Creating a real binding requires
  `WIKIPEDIARAG_EVAL_BINDING_SIGNING_KEY`.
- The P0.1 quality workflow is complete: prepare, review, freeze, ingest,
  resumable dev/test runs, compatibility grouping and safe reports are covered.
  Its 220-row synthetic fixture validates workflow execution, not production
  retrieval quality.
- The controlled RRNCB suite is frozen as `p0-search-quality-v2` at dataset hash
  `40b9cf9b6b4c359d3f8cd1b95006be48f05741f419b2406b1ba28d15399f34b4`:
  65 pinned PDFs, 200 base questions and 1,000 `ru/en/uk/de/ko` retrieval tasks
  split into 200 dev and 800 test tasks.
- Retrieval-only RRNCB execution now emits overall, per-language and paired-to-
  Russian Recall@10, MRR@10, nDCG@10 and latency metrics under one immutable
  dev/test run contract. The execution and acceptance contract is documented in
  [p0-search-quality-v2.md](p0-search-quality-v2.md).

## Active Execution

The two historical degraded reconciliation rows belonged to one disposable
upload-verification knowledge base whose active index version no longer had an
authoritative database record. Reconciliation now persists the safe terminal
code `SEARCH_PROJECTION_INDEX_UNAVAILABLE`; the disposable KB was removed via
the public lifecycle API. Reconciliation is clean: zero pending and zero
degraded rows.

RRNCB ingestion run `p0-search-quality-v2-ingest-real2-40b9cf9b6b4c` completed
with the real `sota_mvp`/`upload_sota_mvp` embedding contract: 65/65 documents,
14,396 chunks, failed=0. All documents used the Xberg parser route; the
average document time was 46.4 s (maximum 332.9 s) under concurrency=2. The
first run used a stale `upload_mock` worker container and is isolated as an
invalid attempt; its results are not used. Readiness remains clean with zero
pending/degraded search-projection rows.

The dev and test retrieval slices are complete (200/200 and 800/800,
failed=0). The full 1,000-task report is compatible with one immutable index
contract: Recall@10=0.949, MRR@10=0.877, nDCG@10=0.895, latency p50=2,345 ms
and p95=9,441 ms. Per-language Recall@10 is RU 0.985, EN/UK 0.965, DE 0.935
and KO 0.895; paired deltas versus RU are EN -0.020, UK -0.020, DE -0.050
and KO -0.090. The reference report is stored under
`artifacts/eval/rrncb-public/p0-search-quality-v2/runs/p0-search-quality-v2-retrieval-real-40b9cf9b6b4c/retrieval-report.json`.

RRNCB has `evaluation_granularity=document`: its source rows do not provide
`gold_section_ids` or `gold_chunk_ids`. Therefore chunk/section Recall, chunk
MRR/nDCG and chunk-based root-cause diagnostics are **N/A** for this run, not
zero-valued quality results or retrieval failures. The baseline does not claim
answer correctness, citation quality or exact evidence-chunk retrieval.

P0.1 is closed. Human translation sign-off, translation cleanup and any
evidence-level RRNCB benchmark are future work; they do not block this closure.

## Latest Relevant Validation

- Provenance/upload/chunker/eval-binding and quality tests: 29 passed.
- Search service tests: 7 passed; document viewer tests: 10 passed.
- Full unit suite: 539 passed with 2 deprecation warnings.
- Full-worktree Ruff check and format check passed; Mypy `src tests` passed.
- Functional retrieval equivalent on Windows: 1 passed.
- Synthetic P0.1 run completed 44 dev and 176 test questions with no execution
  errors. Results correctly stayed separated across five incompatible index
  revisions; they are not a production quality baseline.
- Documentation cleanup: relative Markdown links passed across 13 active/archive
  files and `git diff --check` passed.
- RRNCB reconciliation, multilingual freeze, retrieval reporting and readiness
  coverage: 23 focused tests passed; Ruff and Mypy passed for the changed CLI
  and benchmark modules.
- Translation generation completed 800/800 records. Full-matrix script QC found
  and repaired two Korean-script violations before freeze.
- Real RRNCB retrieval report: 1,000/1,000 completed with five languages and
  200 paired RU comparisons per non-RU language; `compatible=true` and
  document-level Recall@10/MRR@10/nDCG@10 of 0.949/0.877/0.895.
- RRNCB metric coverage was reviewed: its task manifest has no section or chunk
  evidence anchors, so those metric families are intentionally N/A.

The focused retrieval-runner checks remain 31 passed; Ruff, Mypy and
`git diff --check` pass for the changed execution and status files.

## Deferred Follow-up

1. Create an evidence-level RRNCB subset only after adding reviewed section and
   chunk anchors; then measure exact context recall and chunk ranking.
2. Review the 17/200 Korean questions containing mixed Han characters and the
   source questions that are too broad to identify one gold document.
3. Revisit the regression gate when a future retrieval task defines accepted
   metric scope and thresholds.
