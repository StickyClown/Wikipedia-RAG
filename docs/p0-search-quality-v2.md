# P0 Search Quality v2 — RRNCB baseline

## Scope

`p0-search-quality-v2` is a document-retrieval baseline over the pinned public
RRNCB revision. It evaluates one immutable set of 200 Russian legal questions
against 65 uploaded PDFs. Each question has query variants in Russian, English,
Ukrainian, German and Korean, for 1,000 retrieval tasks in total.

The baseline measures retrieval independently from answer generation. It does
not introduce an absolute quality threshold: its accepted results become the
reference point for later regression gates.

## Metric boundary

RRNCB task rows declare `evaluation_granularity=document`. The pinned source
identifies the expected PDF but supplies no reviewed `gold_section_ids` or
`gold_chunk_ids`; all 1,000 frozen tasks therefore have empty section and chunk
gold sets. The report's Recall@10, MRR@10 and nDCG@10 are document-level
metrics, and p50/p95 measure retrieval request latency.

Section Recall, chunk Recall, chunk MRR/nDCG, citation correctness and answer
quality are **N/A** for this baseline. A chunk-based diagnostic must not turn
their absent gold anchors into a zero score or a retrieval failure. Those
metrics require a separately reviewed evidence-level dataset.

The completed reference run has 1,000/1,000 successful tasks and
`compatible=true`: document Recall@10=0.949, MRR@10=0.877, nDCG@10=0.895,
latency p50=2,345 ms and p95=9,441 ms. It is a reference measurement, not a
claim about generated answers or exact evidence selection.

## Frozen contract

- Source dataset: `FractalGPT/RRNCBPublic` at revision
  `a88b57f29165650f47d21e551fb683063acac166`.
- Corpus: exactly 65 PDFs; every PDF is pinned by SHA-256.
- Questions: exactly 200 base rows; 15 are source-labelled no-answer rows.
- Query languages: `ru`, `en`, `uk`, `de`, `ko`; exactly 200 tasks per language.
- Split: 40 base questions in dev and 160 in test, producing 200 dev and 800
  test language tasks. Language variants of one base question always share the
  same split.
- Upload identity: `source_ref_v1` namespace
  `eval:<suite>:<dataset_hash>`, filename as `external_id`, PDF SHA-256 as
  `source_version`.
- Run identity: dataset, source manifest, ingestion mapping, knowledge base and
  retrieval configuration hashes are immutable between dev and test.

## Execution

1. Generate the four translated variants for each Russian question. Generation
   is resumable and every record retains its source-question hash, model alias,
   timestamp and review method.
2. Run full-matrix validation: identity completeness, source-hash match,
   non-empty text, non-copy of the Russian source and expected writing system.
3. For a future production-quality gate, perform a stratified human review of
   at least 20 questions per translated language (80 translations total).
   Review all automatically flagged records. Any critical semantic error expands
   review to the affected language and class before freeze.
4. Freeze `tasks.jsonl`, the dataset manifest and the 65-document source
   manifest. Do not edit them after ingestion begins.
5. Upload all 65 PDFs into a fresh knowledge base and wait for every ingestion
   job to publish. Failed or cancelled items invalidate the ingestion run.
6. Run retrieval-only dev. Inspect failures and language/paired metrics; fixes
   require a new dataset or configuration identity, never mutation of a run.
7. Unlock test only from the completed dev run and execute the remaining 800
   tasks under the same run contract.
8. Publish the aggregate, per-language and paired-to-Russian metrics together
   with latency percentiles and immutable artifact hashes.

## Definition of done

- Search-projection reconciliation is clean: no pending or degraded rows.
- Translation matrix contains exactly 800 valid translated records. Human
  review is required only before using a future run as a production-quality
  acceptance gate.
- Frozen suite contains exactly 1,000 tasks with 200 per language, 200 dev and
  800 test tasks.
- Fresh ingestion contains exactly 65 completed, searchable documents and a
  complete filename-to-document/version mapping.
- Dev and test have no failed or incomplete retrieval tasks.
- All completed rows share one comparison key and the same frozen run contract.
- Final report contains Recall@10, MRR@10, nDCG@10 and p50/p95 latency overall,
  for every language, and paired deltas of `en`, `uk`, `de`, `ko` against `ru`.
- Artifacts and exact validation commands are recorded in `docs/STATUS.md`.

Any future regression gate must use these document-level metrics only until a
reviewed evidence-level dataset exists. The proposed 3-percentage-point
Recall@10/MRR@10 and 25% p95 latency policy is deferred, not enabled by P0.1.
An explicitly reviewed future baseline may replace this reference.
