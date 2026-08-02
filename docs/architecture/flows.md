# Main Flows

These flows show durable writes, transient calls, publication points, failure
boundaries and user-visible results.

## Local Login And Application Session

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant UI as Web UI
    participant API as API
    participant DB as PostgreSQL

    B->>UI: Submit username and password
    UI->>API: POST /api/v1/auth/local/login
    API->>DB: Verify Argon2id password hash
    API->>DB: Insert auth_session with hashed session and CSRF tokens
    API-->>B: Set opaque HttpOnly session cookie
    API-->>UI: Auth session without CSRF token
    UI->>API: GET /api/v1/auth/session with cookie
    API->>DB: Rotate CSRF hash
    API-->>UI: Session, active tenant, CSRF token
    UI-->>B: Show authenticated screen
```

Failure boundary: invalid credentials return a safe auth error; no session is
created.

## OIDC Authorization Code And PKCE

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant UI as Web UI
    participant API as API
    participant IdP as OIDC Provider
    participant DB as PostgreSQL

    B->>UI: Click OIDC
    UI->>API: POST /api/v1/auth/oidc/start
    API->>DB: Store OIDC state, nonce and PKCE verifier
    API-->>UI: Authorization URL
    UI-->>B: Redirect to provider
    B->>IdP: Authenticate and consent
    IdP-->>API: GET /api/v1/auth/oidc/callback with code and state
    API->>DB: Consume flow state
    API->>IdP: Exchange code and validate ID token via JWKS
    API->>DB: Upsert identity by issuer plus subject
    API->>DB: Store encrypted provider tokens server-side
    API->>DB: Insert app auth_session
    API-->>B: Set opaque HttpOnly app session cookie
```

Failure boundary: issuer, audience, nonce, signature or state mismatch aborts
login before app session creation.

## Tenant And KB Selection

```mermaid
sequenceDiagram
    autonumber
    participant UI as Web UI
    participant API as API
    participant DB as PostgreSQL

    UI->>API: GET /api/v1/auth/session
    API->>DB: Load session active_tenant_id and tenant role
    API-->>UI: Active tenant and CSRF token
    UI->>API: GET /api/v1/knowledge-bases
    API->>DB: List KBs for active tenant
    API-->>UI: KB list
    UI-->>UI: Store selected primary KB and retrieval scope in memory
```

Failure boundary: no active tenant returns a server-side authorization error for
tenant-scoped operations.

## Document Upload

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant UI as Web UI
    participant API as API
    participant DB as PostgreSQL
    participant MinIO as MinIO

    B->>UI: Select one or more files
    UI-->>UI: Compute SHA-256 in browser memory
    UI->>API: POST /api/v1/uploads/batches
    API->>DB: Create upload_batch and upload_sessions
    API-->>UI: Presigned URLs and required headers
    UI->>MinIO: PUT file bytes to presigned URL
    UI->>API: POST /api/v1/uploads/sessions/{id}:complete
    API->>DB: Create document, version, artifact metadata, job and job item
    API-->>UI: document_id, version_id and job_id
    UI->>API: GET /api/v1/uploads/batches/{batch_id}
    API->>DB: Read safe aggregate progress
    API-->>UI: Per-file status without object keys
```

Failure boundary: unsafe filename, duplicate batch item, checksum mismatch or
missing object fails with a safe error before publication.

## Parsing, Chunking, Embedding And Publication

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker
    participant DB as PostgreSQL
    participant MinIO as MinIO
    participant Meta as Metadata Service
    participant Parser as Xberg or Docling
    participant GW as Model Gateway
    participant OS as OpenSearch

    W->>DB: Claim ingestion_job_item with FOR UPDATE SKIP LOCKED
    W->>MinIO: Read original upload bytes
    W-->>W: Validate size, checksum, MIME and safety
    W->>Meta: Extract language/date metadata
    W->>Parser: Parse bytes or temp file
    Parser-->>W: Parsed text and parser metadata
    W-->>W: Normalize to app-owned contract
    W->>MinIO: Write normalized.json and parser-report.json
    W->>DB: Record normalized/parser artifacts and version metadata
    W-->>W: Chunk with locators
    W->>GW: Request embeddings
    W->>DB: Stage chunks with publication_status staged
    W->>OS: Bulk index published chunk documents
    W->>DB: Publish chunks, document sections and document version
    W-->>DB: Mark item/job completed
```

Publication point: chunks are queryable only after OpenSearch bulk index
succeeds and DB chunks/version are updated to published. Document sections are
written from the same published chunk set; failed or cancelled ingestion does
not publish document navigation state.

## Chat Retrieval And Generation

```mermaid
sequenceDiagram
    autonumber
    participant UI as Web UI
    participant API as API
    participant DB as PostgreSQL
    participant OS as OpenSearch
    participant GW as Model Gateway

    UI->>API: POST /api/v1/chat with cookie, CSRF and KB scope
    API->>DB: Resolve ActorContext and require VIEWER on every KB
    API->>DB: Check active compatible index for every requested KB
    API-->>UI: SSE run.started with safe search_plan
    API->>OS: BM25 and vector searches with tenant and KB filters
    API->>GW: Embedding and rerank alias calls
    API-->>API: Fuse, rerank, dedup, expand parents and assess answerability
    API->>GW: Chat completion alias call
    API->>DB: Insert query_run and retrieval_events
    API-->>UI: SSE message.delta with answer and evidence
    API-->>UI: SSE usage.updated with safe diagnostics
    API-->>UI: SSE run.completed
```

Failure boundary: missing role or not-ready KB fails safely before partial
retrieval. User sees stream failure only where the UI renders it.

## Extended Search

```mermaid
sequenceDiagram
    autonumber
    participant API as API
    participant OS as OpenSearch
    participant GW as Model Gateway
    participant DB as PostgreSQL

    API-->>API: Initial retrieval is PARTIAL or UNANSWERABLE
    API-->>API: Confirm profile policy allows Extended Search
    API->>GW: Generate bounded follow-up queries
    loop Bounded subqueries
        API->>OS: Retrieve additional evidence for the primary KB
    end
    API-->>API: Select final evidence and answerability
    API->>DB: Persist retrieval events and diagnostics
```

Extended Search is implemented as single-KB in this slice. Multi-KB direct
retrieval bypasses Extended Search.

## Document Delete And Deferred Purge

```mermaid
sequenceDiagram
    autonumber
    participant UI as Client
    participant API as API
    participant DB as PostgreSQL
    participant OS as OpenSearch
    participant W as Worker
    participant MinIO as MinIO

    UI->>API: DELETE /api/v1/documents/{document_id}
    API->>DB: Require KB OWNER and load lifecycle
    API->>DB: Mark document/version deleting and DB chunks deleted
    API->>OS: Delete derived chunks by tenant, KB and document
    API->>DB: Schedule document_delete job with purge_after
    API-->>UI: lifecycle_state deleting and optional job_id
    W->>DB: Claim due document_delete job
    W->>DB: List artifact keys
    W->>MinIO: Delete artifact objects
    W->>OS: Repeat derived chunk deletion idempotently
    W->>DB: Delete DB chunks/artifact rows and mark lifecycle deleted
```

Failure boundary: purge failure records `purge_failed` and a safe error code so
the job can be retried.

## Reprocess And Reindex

```mermaid
sequenceDiagram
    autonumber
    participant Client as Client
    participant API as API
    participant DB as PostgreSQL
    participant W as Worker
    participant MinIO as MinIO
    participant OS as OpenSearch

    Client->>API: POST /api/v1/documents/{document_id}:reprocess
    API->>DB: Require KB EDITOR and enqueue document_upload job
    API-->>Client: New job_id
    W->>DB: Claim job item
    W->>MinIO: Read original artifact
    W-->>W: Re-run validation, parsing, normalization, chunking and embedding
    W->>OS: Write derived search docs
    W->>DB: Publish updated chunk/version state
```

Reindexing uses the active KB index contract. OpenSearch can be rebuilt from
PostgreSQL and MinIO; it is not the backup authority.

## Readiness And Degraded Dependencies

```mermaid
sequenceDiagram
    autonumber
    participant Client as Client
    participant API as API
    participant DB as PostgreSQL
    participant GW as Model Gateway
    participant Provider as Model Provider

    Client->>API: GET /health
    API-->>Client: Liveness after startup
    Client->>API: GET /ready
    API->>DB: SELECT 1
    API->>GW: GET /ready
    GW->>Provider: Startup smoke when required or warned
    GW-->>API: ok or degraded with safe reason
    API-->>Client: ok or degraded
```

API `/ready` currently checks PostgreSQL and Model Gateway readiness. Redis,
MinIO and OpenSearch readiness checks were not confirmed in the API
implementation.
