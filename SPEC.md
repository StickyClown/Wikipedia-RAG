# Product specification — Production RAG Platform

Status: baseline specification  
Source of detail: `docs/architecture.md`

## Product outcome

Создать Docker-first платформу, которая отвечает на вопросы по локальной Wikipedia и пользовательским документам, прикладывает проверяемые ссылки на evidence, показывает полный retrieval trace и имеет ограниченный Extended Search для сложных запросов.

## Primary users

- оператор платформы;
- администратор tenant;
- редактор knowledge base;
- конечный пользователь чата;
- инженер, исследующий retrieval quality.

## MVP sequence

### Release A — Foundation vertical slice

FastAPI request проходит через Model Gateway к OpenRouter-compatible provider, стримится клиенту, сохраняет `query_run` и создаёт trace.

### Release B — Wikipedia RAG

ZIM импортируется детерминированно; BM25 и dense retrieval работают параллельно; RRF и reranker формируют evidence; ответ содержит валидные citations; retrieval debugger показывает причины выбора.

### Release C — Universal documents

Upload, Docling/Tika routing, canonical document model, versioned templates, chunk preview и atomic reindex.

### Release D — Local models

Model Gateway переключает chat/embeddings/rerank с OpenRouter на отдельные `llama-server` без изменений business code.

### Release E — Extended Search

Bounded orchestrator выполняет decomposition/iterative retrieval только для сложных или evidence-starved запросов и сохраняет evidence ledger.

## Functional requirements

1. Multi-tenant users, memberships and knowledge bases.
2. Wikipedia ZIM and uploaded-document ingestion.
3. Versioned canonical artifacts and deterministic chunk IDs.
4. BM25 + dense retrieval, RRF, cross-encoder reranking.
5. Dedup, source/page quotas, selective neighbor/parent expansion and token-budget packing.
6. Generated answers with source references and deterministic citation validation.
7. No-answer/insufficient-evidence behavior.
8. Retrieval and agent trace inspection.
9. Background job states, retry policy, idempotency and atomic publication.
10. Evaluation datasets, regression gates and operational metrics.

## Non-functional requirements

- Docker Compose first, Kubernetes-compatible service contracts.
- Stateless online services and separately scalable workers.
- CUDA only in model/OCR containers that need it.
- Structured logs, metrics and OpenTelemetry spans.
- No cross-tenant retrieval incidents.
- Reproducible builds and pinned production images.
- Secret redaction and untrusted-file parsing isolation.

## Explicit non-goals before measured evidence

- full GraphRAG;
- multi-agent swarm;
- always-on query rewrite;
- learned sparse as mandatory index;
- ColBERT/late interaction in main path;
- proposition-level chunking for all documents;
- synchronous large-file indexing in HTTP requests.

## Initial SLO targets

Normal RAG:

- API availability target: 99.5%;
- warm retrieval p95 under 800 ms;
- remote-provider time to first streamed token under 3–5 seconds;
- trace coverage 100% of query runs;
- deterministic citation validator execution 100%;
- cross-tenant retrieval incidents: zero.

Ingestion:

- durable job state and crash-safe resume;
- complete source/version provenance;
- failed document never publishes a partial index;
- staging validation before atomic alias switch.

Extended Search:

- hard wall-time, model-call and tool-step budgets;
- duplicate-call loop prevention;
- explicit partial/insufficient answer on missing evidence.

## Global release gate

A release cannot be accepted until the relevant tests, evaluation slices, migration rollback, security checks and operational smoke tests pass as defined in `docs/quality/`.
