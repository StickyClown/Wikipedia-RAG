# Domain and system invariants

## Tenancy

1. Client input never directly selects `tenant_id` for authorization.
2. Every search path applies the same server-owned access filters to BM25 and vector queries.
3. Caches, traces and exported reports include tenant/access scope in their key or authorization check.
4. Tests must prove that a document from tenant B cannot appear in tenant A results or traces.

## Documents and indexing

1. Original upload is immutable and content-addressed.
2. Canonical document and chunk manifest identify parser/template/model versions.
3. Chunk IDs are deterministic within a version.
4. Staging index is validated before alias publication.
5. Failed jobs do not publish partial content.
6. Reprocessing from a saved canonical artifact must not require repeating OCR unless explicitly requested.

## Wikipedia XML multistream

1. The local MVP treats Wikimedia XML `pages-articles` bzip2 multistream as the primary Wikipedia source.
2. The multistream index is valid only when every UTF-8 line follows `offset:page_id:title`.
3. Index offsets must be monotonic non-decreasing. Equal offsets are valid because many pages can belong to one bzip2 stream.
4. Import work is grouped by unique bzip2 stream offsets after sampled offsets are verified against the compressed XML file signature.
5. Workers must not load the full multi-gigabyte dump or full decompressed XML into memory.
6. Checkpoints advance only after a stream's durable DB/object-storage/search writes complete.
7. Imported Wikipedia content preserves namespace, title, page ID, revision ID, timestamp, redirect target and source wikitext provenance.

## Models

1. Application services use logical aliases.
2. Alias health is based on capability smoke tests.
3. Provider retries are bounded and operation-aware.
4. Usage and latency are recorded without logging prompts by default in production.

## Answers and citations

1. The generator may cite only evidence IDs present in the supplied context manifest.
2. Citation IDs are resolved deterministically after generation.
3. Unknown or malformed IDs cause validation failure or answer repair, never silent acceptance.
4. Insufficient evidence produces an explicit qualified answer/refusal.

## Agents

1. Extended Search is not the default.
2. Hard budgets exist for wall time, steps, subqueries and model calls.
3. Normalized duplicate tool calls are rejected or served from run-local cache.
4. The stop reason and final evidence coverage are persisted.
