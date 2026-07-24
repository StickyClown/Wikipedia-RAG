# ExecPlan 02 — Deterministic Wikipedia ZIM ingestion

## Outcome

A small fixed ZIM fixture is imported asynchronously into versioned canonical artifacts and a staging OpenSearch index with deterministic page/chunk IDs and atomic publication.

## In scope

- upload/register local ZIM snapshot metadata;
- libzim adapter;
- redirects, canonical title/URL and section hierarchy;
- Wikipedia template with section-aware child chunks and parent links;
- artifact manifests in MinIO/S3;
- durable ingestion state/checkpoints;
- staging validation and alias switch;
- job API and tests.

## Out of scope

Generic PDF/Office parsers, embeddings and answer generation.

## Acceptance criteria

- repeat import produces identical IDs and no duplicates;
- worker restart resumes safely;
- redirect resolution is tested;
- failed validation leaves read alias unchanged;
- chunks preserve source/section/snapshot provenance;
- no full ZIM is loaded into RAM.

## Validation

```bash
make test-unit TEST=zim
make test-integration TEST=zim-ingestion
make test-e2e TEST=zim-publish
```

## Progress

- [ ] Plan refined.
- [ ] Implemented.
- [ ] Reviewed.
