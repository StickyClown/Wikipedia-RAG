# Web Architecture

The web UI is implemented in `services/ui/src/App.tsx` as a single React/Vite
screen. No frontend router was found.

## Screens And Entry Points

- Header: product name, API readiness status, current session user and logout.
- Sign in: local username/password form and OIDC start button.
- Knowledge-base toolbar: primary KB selector, retrieval-scope checkboxes and create-KB form.
- Wikipedia Import: bounded ZIM import trigger and polled ingestion job progress.
- Upload: multi-file picker, batch upload status, per-file progress, retry for failed ingestion jobs and public metadata for the first completed document.
- Search: ordinary viewer-scoped search with metadata filters, result snippets and a document viewer open action.
- Document viewer: inline text viewer opened from search results, with document TOC, in-document search and chunk/section context.
- Chat: question input, mode selector, retrieval profile and advanced retrieval override controls.
- Answer and sources: generated answer plus cited evidence returned by chat SSE events.
- Retrieval Debugger: query-run retrieval events loaded after a chat run.

Backend admin APIs exist for users, tenants, groups and KB grants. No dedicated
frontend admin pages for those APIs were found.

## Login And Session

The browser starts by calling `GET /api/v1/auth/session` with
`credentials: "include"`. If authenticated, the UI loads
`GET /api/v1/knowledge-bases` and selects the first KB in React state.

Local login calls `POST /api/v1/auth/local/login`. OIDC login calls
`POST /api/v1/auth/oidc/start` and then redirects the browser to the returned
authorization URL. The backend callback creates the application session.

The browser receives only an opaque HttpOnly application session cookie. It
does not receive provider access or refresh tokens.

## Tenant And Knowledge-Base Selection

Active tenant is stored server-side in the app session. The UI displays
`active_tenant_id` from the session response but does not choose tenants in the
current screen. Knowledge bases are selected in browser memory:

- `selectedKnowledgeBaseId` is the primary KB for import/upload.
- `selectedRetrievalKnowledgeBaseIds` is the chat/debug retrieval scope.

The backend still enforces tenant and KB access through `ActorContext`; UI
choices are not trusted authority.

## Chat Screen

The chat form posts to `POST /api/v1/chat` with:

- `message`;
- `knowledge_base_ids`;
- `mode`;
- `stream: true`;
- `retrieval_profile`;
- retrieval override settings for BM25, dense, fusion, rerank, parent expansion, Extended Search and top K.

The client reads the SSE response using `fetch(...).body.getReader()` and a
local `parseSse` function. Browser `EventSource` is not used.

Handled chat events in the UI:

- `message.delta`: updates answer text, evidence and query run id.
- `run.completed`: updates query run id.

Other SSE events are emitted by the backend but are not rendered by the current
UI.

## Upload And Ingestion Progress

The UI accepts multiple files, computes SHA-256 with `crypto.subtle.digest`,
then calls `POST /api/v1/uploads/batches`. For each returned item, the browser:

1. uploads bytes directly to the presigned MinIO URL with `PUT`;
2. calls `POST /api/v1/uploads/sessions/{upload_session_id}:complete`;
3. polls `GET /api/v1/uploads/batches/{batch_id}`;
4. optionally calls `POST /api/v1/ingestion-jobs/{job_id}:resume` for failed items.

Batch status exposes safe per-file progress only. Object keys are not returned.

## Search And Document Viewer

Ordinary search calls `POST /api/v1/search` with selected KB ids, query, paging
and metadata filters. Results include `chunk_id`, `document_id`,
`section_path`, safe snippet metadata and locator data. The UI opens the inline
document viewer by calling:

- `GET /api/v1/documents/{document_id}/structure` for title, source metadata and TOC sections;
- `GET /api/v1/documents/{document_id}/context?chunk_id=...` for the precise hit plus neighboring chunks;
- `GET /api/v1/documents/{document_id}/context?section_id=...` when the user selects a TOC entry;
- `POST /api/v1/documents/{document_id}/search` for search within one active document.

The viewer renders extracted text chunks, not native PDF/Office pages. The API
continues to resolve tenant and KB authority server-side from the authenticated
actor and active document.

## Client State

State is held in React `useState` and `useMemo`. Confirmed state includes
session, KB list, selected KB ids, import job, question, retrieval settings,
answer, evidence, query run id, retrieval events, upload batch, upload items,
upload document metadata, ordinary search results, document viewer state and
errors.

No `localStorage`, `sessionStorage` or IndexedDB usage was found in the UI.
Selected files exist only as browser `File` objects during the upload function.

## API Client Behavior

The UI uses a local `apiFetch` helper:

- prefixes paths with `VITE_API_BASE_URL` or `http://localhost:8000`;
- sends `credentials: "include"`;
- adds `X-CSRF-Token` for non-GET requests when the session response has a token.

Direct presigned MinIO `PUT` calls do not use the app API cookie or CSRF token.

## UI States

- Loading: readiness starts as `checking`; upload items move through preparing, hashing, uploading, completing, queued and polled states.
- Empty: if no authenticated session exists, only the sign-in band is shown; if no KB exists, selectors render with no options.
- Degraded: `/ready` status is displayed in the header as returned by the API.
- Forbidden: failed API calls usually surface as raw response text in auth/upload error areas; some KB operations fail silently by returning early.
- Failed: upload item and batch errors show safe error code/message when provided; chat stream failure events are not fully rendered in the current UI.

## Data The Browser Must Never Receive

- MinIO access keys or server-side object keys.
- Provider access or refresh tokens.
- Raw provider payloads or prompts.
- Parser stderr, stack traces or arbitrary parser logs.
- Cross-tenant tenant IDs, KB IDs, document IDs or query-run data outside the authenticated actor's scope.
- Secrets from `.env`, mounted secret files or smoke fixtures.

## User Scenarios

| Scenario | UI entry point | API or backend interaction | Client state | Errors |
| --- | --- | --- | --- | --- |
| Local login | Sign in form | `POST /api/v1/auth/local/login`, then `GET /api/v1/auth/session` and `GET /api/v1/knowledge-bases` | `session`, `knowledgeBases`, selected KB ids | Raw auth response text in `authError` |
| OIDC login | OIDC button | `POST /api/v1/auth/oidc/start`, browser redirect, backend callback sets app session | Browser leaves app, then session reloads on return | Raw start response text in `authError` |
| Select KB | KB toolbar | Existing loaded KB list; backend enforces chosen KB on later calls | `selectedKnowledgeBaseId`, `selectedRetrievalKnowledgeBaseIds` | Empty selector if no KBs are loaded |
| Create KB | KB create form | `POST /api/v1/knowledge-bases`, then reload list | `knowledgeBases`, `newKnowledgeBaseName` | No explicit render for failed create |
| Import Wikipedia subset | Wikipedia Import panel | `POST /api/v1/wikipedia/zim-imports`, poll `GET /api/v1/ingestion-jobs/{job_id}` | `job` | Job `error_message` displayed |
| Multi-file upload | Upload panel | `POST /api/v1/uploads/batches`, presigned MinIO `PUT`, complete sessions, poll batch | `uploadBatch`, `uploadItems`, `uploadDocument` | Upload/batch errors in item or panel |
| Retry failed ingestion | Upload item Retry button | `POST /api/v1/ingestion-jobs/{job_id}:resume`, poll batch | Item status and batch status | Response text on item |
| Ordinary search | Search form | `POST /api/v1/search` with selected KB scope and filters | `searchResults`, `searchHasMore` | Response text in `searchError` |
| Open document hit | Search result button | `GET /api/v1/documents/{document_id}/structure`, then context by `chunk_id` | `viewerStructure`, `viewerContext` | Response text in `viewerError` |
| Search inside document | Document viewer form | `POST /api/v1/documents/{document_id}/search` | `viewerSearchResults` | Response text in `viewerError` |
| Chat | Chat form | `POST /api/v1/chat` SSE; backend calls retrieval and Model Gateway | `answer`, `evidence`, `queryRunId` | Stream failure event not fully rendered |
| Retrieval debug | Debug button | `GET /api/v1/query-runs/{query_run_id}/retrieval` | `events` | Not displayed when request fails |

## Main Web Interaction

```mermaid
flowchart LR
    Browser["Browser"]
    UI["React Web UI"]
    API["FastAPI API"]
    Postgres["PostgreSQL"]
    MinIO["MinIO"]
    Search["OpenSearch"]
    Gateway["Model Gateway"]
    Worker["Worker"]
    Parsers["Xberg, Docling, Metadata Service"]

    Browser -->|"loads app and keeps File objects in memory"| UI
    UI -->|"credentials include, X-CSRF-Token, JSON"| API
    UI -->|"presigned PUT for upload bytes"| MinIO
    API -->|"sessions, KBs, jobs, query runs"| Postgres
    API -->|"chat retrieval"| Search
    API -->|"generation aliases"| Gateway
    API -->|"job status"| Postgres
    Worker -->|"claims jobs"| Postgres
    Worker -->|"reads and writes artifacts"| MinIO
    Worker -->|"parser HTTP calls"| Parsers
    Worker -->|"publishes derived chunks"| Search
```
