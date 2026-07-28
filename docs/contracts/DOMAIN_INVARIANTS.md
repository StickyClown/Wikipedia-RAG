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

1. Wikimedia XML `pages-articles` bzip2 multistream remains a supported regression/local fallback source.
2. The multistream index is valid only when every UTF-8 line follows `offset:page_id:title`.
3. Index offsets must be monotonic non-decreasing. Equal offsets are valid because many pages can belong to one bzip2 stream.
4. Import work is grouped by unique bzip2 stream offsets after sampled offsets are verified against the compressed XML file signature.
5. Workers must not load the full multi-gigabyte dump or full decompressed XML into memory.
6. Checkpoints advance only after a stream's durable DB/object-storage/search writes complete.
7. Imported Wikipedia content preserves namespace, title, page ID, revision ID, timestamp, redirect target and source wikitext provenance.

## Wikipedia ZIM/Kiwix

1. The real demo MVP treats ZIM/libzim + Kiwix as the primary Wikipedia source.
2. Kiwix serves the full ZIM mounted read-only from `./zim`; RAG imports a bounded canonical article subset from the same archive.
3. Redirect entries do not count toward `WIKI_LIMIT`; they are stored as redirect provenance and are not chunked.
4. Source URLs are built from `KIWIX_PUBLIC_BASE_URL`, Kiwix's archive identifier and exact libzim `zim_entry_path`, never from a reconstructed title. The archive identifier is `KIWIX_BOOK_NAME` when set, otherwise the ZIM filename stem used by `kiwix-serve`.
5. Checkpoints advance only after durable DB, object-storage and OpenSearch writes for the processed batch complete.
6. Embedding alias or dimension changes produce a new index version and require reindex before publication.
7. Online retrieval validates the knowledge base active alias against a compatible `index_versions` contract before searching. Missing or incompatible alias/index/model metadata fails as `KB_NOT_READY`, never as silent fallback retrieval.

## Models

1. Application services use logical aliases.
2. Alias health is based on capability smoke tests.
3. Provider retries are bounded and operation-aware.
4. Usage and latency are recorded without logging prompts by default in production.
5. `sota_mvp` cannot silently fall back to mock aliases or hash embeddings.

## Answers and citations

1. The generator may cite only evidence IDs present in the supplied context manifest.
2. Citation IDs are resolved deterministically after generation.
3. Unknown or malformed IDs cause validation failure or answer repair, never silent acceptance.
4. Answerability is decided by a deterministic post-retrieval gate, not by evidence count alone.
5. `UNANSWERABLE` and `CONFLICTING` gate decisions produce explicit local refusals/caveats when Extended Search cannot improve coverage.
6. `PARTIAL` answers must explicitly state the coverage gap and must not claim uncovered parts.

## Agents

1. Extended Search is not the default.
2. Hard budgets exist for wall time, steps, subqueries and model calls.
3. Normalized duplicate tool calls are rejected or served from run-local cache.
4. The stop reason and final evidence coverage are persisted.
5. Conditional Extended Search starts only from `PARTIAL` or `UNANSWERABLE` answerability decisions.
