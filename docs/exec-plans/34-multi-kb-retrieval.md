# ExecPlan 34 - Multi-KB Retrieval

Status: implemented 2026-07-30

## Scope

Replace the hard `MULTI_KB_UNSUPPORTED` API gate with a validated KB scope list for normal chat and debug retrieval.

## Implemented

- `POST /api/v1/chat` and `POST /api/v1/search:debug` accept multiple `knowledge_base_ids`.
- Chat requires `VIEWER` on every requested KB; debug retrieval requires `EDITOR` on every requested KB.
- Every requested KB is checked for an active compatible retrieval contract before retrieval starts. If any KB is not ready, the API returns safe `KB_NOT_READY` with the problematic KB id and no partial retrieval.
- Retrieval runs BM25/dense per KB using server-owned tenant/KB filters and fuses candidates in a shared pool.
- A per-KB cap is applied before rerank to prevent one KB from dominating the candidate pool.
- Evidence/citations and debug candidates include `knowledge_base_id`.
- Search plans and answer/failure artifacts include `knowledge_base_ids`.
- Extended-search harness remains single-KB in v1; multi-KB chat uses direct retrieval even when extended mode would otherwise be selected.

## Validation

```text
uv run pytest tests\unit\test_multi_kb_retrieval.py -q
-> exit 0, 2 passed
```
