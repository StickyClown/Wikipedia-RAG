# ExecPlan 02 — Deterministic Wikipedia XML multistream ingestion

## Outcome

A small fixed Wikimedia XML `pages-articles` bzip2 multistream fixture is imported asynchronously into versioned canonical artifacts and a staging OpenSearch index with deterministic page/chunk IDs and atomic publication. The real Russian dump can run in limited development mode or resumable full mode.

## In scope

- register local Wikimedia XML dump and multistream index metadata;
- XML bzip2 multistream adapter;
- index validation with monotonic non-decreasing offsets and unique stream grouping;
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
- no full XML dump is loaded into RAM;
- repeated offsets in the index are accepted as pages in the same bzip2 stream.

## Validation

```bash
make test-unit TEST=wiki
make test-integration TEST=wiki-ingestion
make test-e2e TEST=wiki-publish
```

## Progress

- [x] Plan refined.
- [x] Implemented for local MVP.
- [x] Reviewed with local checks.

## Discoveries

- Local dump is `zip/ruwiki-20260701-pages-articles-multistream.xml.bz2`, 6,135,514,301 bytes, bzip2 signature `BZh9`.
- Local index is `zip/ruwiki-20260701-pages-articles-multistream-index.txt.bz2`, 65,980,533 bytes, bzip2 signature `BZh9`.
- Index lines are UTF-8 `offset:page_id:title`.
- Offsets are monotonic non-decreasing. Repeated offsets are expected because many pages belong to the same bzip2 stream.
- Full index validation observed about 6.36M page rows and about 63.6K unique bzip2 stream offsets.

## Decision log

- Primary Wikipedia source for local MVP is Wikimedia XML `pages-articles` bzip2 multistream.
- ZIM/libzim remains a future specialized adapter.
- Checkpoints advance by unique bzip2 stream offset after DB/object-storage/OpenSearch writes complete.
- Page/chunk IDs are deterministic from snapshot, page/revision, section path, sequence and content hash.

## Final evidence

- `uv run pytest tests/unit/test_wiki_dump.py`: covered index validation, non-decreasing repeated offsets, stream grouping, XML parsing and namespace filtering.
- `uv run python -m wikipediarag.cli import-wiki --limit 10 --wait`: job `c7f5cf7a-5da2-48b3-bd6f-3f715f97a330`, 10 pages imported, 473 chunks indexed.
- Restart/resume check: job `553d6ac3-3d86-477b-85d3-dcdb69b053da`, worker restarted at 100 pages/3991 chunks, completed with 497 pages imported and 10262 chunks indexed.
- Full import command exists as `make import-wiki-full`; full import has not been started.
