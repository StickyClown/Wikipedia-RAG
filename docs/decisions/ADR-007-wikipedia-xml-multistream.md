# ADR-007 — Wikipedia source adapter

Status: accepted for local MVP

## Context

The available Russian Wikipedia data asset is not a ZIM snapshot. The repository has local Wikimedia dump files:

- `zip/ruwiki-20260701-pages-articles-multistream.xml.bz2`
- `zip/ruwiki-20260701-pages-articles-multistream-index.txt.bz2`

The index format is `offset:page_id:title`; offsets are monotonic non-decreasing because multiple pages can belong to the same bzip2 stream.

## Decision

Use Wikimedia XML `pages-articles` bzip2 multistream as the primary Wikipedia ingestion source for the local MVP. ZIM/libzim remains a future specialized adapter.

## Consequences

- The Wikipedia worker validates the compressed XML and compressed index before importing.
- Index validation checks UTF-8 lines, `offset:page_id:title`, monotonic non-decreasing offsets, and sampled unique offsets pointing to bzip2 stream signatures.
- Compressed dumps are processed by unique stream offsets with durable checkpoints and resumable jobs.
- Uncompressed XML dumps, if provided later, are processed with a streaming XML parser and never loaded fully into memory.
- Imported pages preserve MediaWiki metadata: namespace, title, page ID, revision ID, timestamp, redirect target and source wikitext provenance.
