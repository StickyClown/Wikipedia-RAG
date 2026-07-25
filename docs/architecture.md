---
title: "Production RAG Platform: архитектура, ingestion, retrieval и agentic search"
version: "0.2"
date: "2026-07-24"
language: "ru"
status: "Architecture baseline"
---

# Production RAG Platform

Единый архитектурный документ для локальной production-ready RAG-платформы с Wikimedia XML dump как основным доступным источником Wikipedia, будущим ZIM-адаптером, загрузкой произвольных документов, гибридным поиском, прозрачной диагностикой retrieval и отдельным режимом расширенного агентного поиска.

> **Главное решение:** весь продукт работает в Docker. Сейчас все модели вызываются через OpenRouter. Целевая локальная схема — отдельные `llama-server` из `llama.cpp` для генерации, embeddings и reranking. Прикладной код не знает, где физически запущена модель: он обращается только к внутреннему Model Gateway по OpenAI-совместимому контракту.

---

## 1. Цели проекта

### 1.1. Функциональные цели

1. Отвечать на вопросы по локальной Wikipedia и пользовательским базам знаний с обязательными проверяемыми ссылками на источники.
2. Поддерживать быстрый обычный RAG для большинства запросов.
3. Иметь переключаемый режим **Extended Search** для сложных, многошаговых и конфликтных вопросов.
4. Позволять загружать произвольные документы через UI/API и индексировать их с помощью Docling, Apache Tika или специализированного парсера.
5. Поддерживать разные шаблоны разбора и chunking, похожие по пользовательской модели на RAGFlow.
6. С первого дня подробно сохранять, почему retrieval выбрал или отбросил каждый кандидат.
7. Давать возможность человеку просматривать исходный документ, структуру, chunks, scoring и итоговые ссылки.
8. Масштабироваться горизонтально минимум на десятки активных пользователей без переписывания архитектуры.

### 1.2. Нефункциональные цели

- Docker-first и воспроизводимые сборки.
- CUDA только в контейнерах, которым она действительно нужна.
- Stateless API и горизонтальное масштабирование.
- Версионирование документов, parser templates, embeddings и индексов.
- Наблюдаемость: traces, metrics, structured logs, retrieval events.
- Отделение online-query path от тяжёлого ingestion path.
- Возможность заменить OpenRouter на локальный `llama.cpp` изменением конфигурации, а не бизнес-логики.
- Возможность перейти с Docker Compose на Kubernetes без изменения контрактов сервисов.

### 1.3. Не-цели первой версии

В первой production-итерации не нужны:

- полноценный GraphRAG;
- multi-agent swarm;
- LLM-rewrite каждого запроса;
- learned sparse retrieval как обязательный индекс;
- ColBERT/late interaction в основном контуре;
- proposition-level chunking всех документов;
- универсальная автономная обработка любых нестандартных файлов без ручной проверки;
- синхронная индексация больших документов внутри HTTP-запроса.

Эти функции должны добавляться только после измеримой пользы на собственном evaluation set.

---

## 2. Ключевые архитектурные решения

| Область | Решение |
|---|---|
| Контейнеризация | Docker Compose в начале; Kubernetes-ready контейнеры и stateless-сервисы |
| API | Python, FastAPI, Pydantic, async I/O, SSE для стриминга |
| UI | React + Vite; минимальный MVP допустимо начать с HTMX/Jinja |
| Model serving сейчас | OpenRouter |
| Model serving целевой | `llama.cpp` / `llama-server`, отдельный сервер на роль модели |
| Универсальный доступ к моделям | Внутренний Model Gateway с OpenAI-совместимыми endpoint-ами |
| Поисковый движок | OpenSearch: BM25 + `knn_vector`/HNSW + filters |
| Fusion | RRF по умолчанию |
| Reranking | Cross-encoder; сейчас OpenRouter rerank, позже локальный llama.cpp reranker |
| Metadata DB | PostgreSQL |
| Object storage | MinIO/S3 |
| Cache/queue/limits | Redis или Valkey |
| Документы | Docling как структурный parser; Apache Tika как broad-format detector/fallback |
| Wikipedia | Wikimedia XML pages-articles bzip2 multistream для local MVP; ZIM + libzim как будущий специализированный adapter |
| Оркестрация обычного RAG | Собная typed state machine на Python |
| Extended Search | LangGraph или собственный bounded agent loop; один orchestrator |
| Observability | OpenTelemetry + Phoenix + Prometheus/Grafana + собственные retrieval events |
| Background jobs | Celery, Dramatiq, Arq или Temporal позже; для MVP предпочтительно Dramatiq/Arq |

---

## 3. Архитектура верхнего уровня

```mermaid
flowchart TB
    U[Web UI / API clients]
    G[API Gateway / FastAPI]
    A[Application Orchestrator]
    MG[Model Gateway]
    R[Retrieval Service]
    AG[Extended Search Agent]
    OS[(OpenSearch)]
    PG[(PostgreSQL)]
    RD[(Redis / Valkey)]
    S3[(MinIO / S3)]
    IQ[Ingestion Queue]
    IW[Ingestion Workers]
    D[Docling]
    T[Apache Tika]
    Z[Wikipedia XML / future ZIM]
    OR[OpenRouter]
    LC1[llama-server: chat]
    LC2[llama-server: embeddings]
    LC3[llama-server: reranker]
    OT[OTel Collector]
    PX[Phoenix]
    PM[Prometheus / Grafana]

    U --> G
    G --> A
    A --> R
    A --> AG
    A --> MG
    R --> OS
    R --> MG
    A --> PG
    A --> RD

    G --> IQ
    IQ --> IW
    IW --> D
    IW --> T
    IW --> Z
    IW --> MG
    IW --> OS
    IW --> PG
    IW --> S3

    MG --> OR
    MG -. target local mode .-> LC1
    MG -. target local mode .-> LC2
    MG -. target local mode .-> LC3

    G --> OT
    A --> OT
    R --> OT
    AG --> OT
    IW --> OT
    MG --> OT
    OT --> PX
    OT --> PM
```

### 3.1. Основной принцип разделения

Платформа разделена на четыре независимых контура:

1. **Application/API contour** — пользователи, аутентификация, knowledge bases, chat sessions, jobs, streaming.
2. **Retrieval contour** — lexical, dense, fusion, reranking, context assembly, citation mapping.
3. **Ingestion contour** — upload, virus/safety checks, parsing, normalization, template execution, chunking, embeddings, index publication.
4. **Model contour** — единый Model Gateway, OpenRouter сейчас и `llama.cpp` позже.

Это позволяет:

- не блокировать query path тяжёлыми PDF/OCR задачами;
- масштабировать retrieval отдельно от генерации;
- обновлять embeddings или parser без остановки чата;
- использовать разные GPU для chat, embeddings и reranker;
- держать API-контейнер без CUDA и тяжёлых ML-зависимостей.

---

## 4. Model Gateway и модельный слой

## 4.1. Зачем нужен собственный gateway

OpenRouter и `llama-server` уже предлагают OpenAI-подобные API, но между провайдерами различаются:

- model slug;
- набор поддерживаемых параметров;
- формат provider-specific ошибок;
- retry/fallback semantics;
- rate limits;
- telemetry headers;
- наличие embeddings/rerank;
- tool-calling и structured output нюансы;
- длина контекста и tokenizer.

Поэтому прикладной код должен использовать **логические aliases**, а не физические имена моделей.

```yaml
models:
  generator_fast:
    provider: openrouter
    model: qwen/qwen3.6-35b-a3b

  generator_deep:
    provider: openrouter
    model: qwen/qwen3.6-35b-a3b

  verifier:
    provider: openrouter
    model: qwen/qwen3.6-35b-a3b

  embed_default:
    provider: openrouter
    model: qwen/qwen3-embedding-4b
    dimensions: 1024

  embed_quality:
    provider: openrouter
    model: qwen/qwen3-embedding-8b
    dimensions: 1024

  rerank_default:
    provider: openrouter
    model: cohere/rerank-v3.5
```

После перехода на локальный режим меняется только registry:

```yaml
models:
  generator_fast:
    provider: llamacpp
    base_url: http://llama-chat:8080/v1
    model: generator-fast

  embed_default:
    provider: llamacpp
    base_url: http://llama-embed:8080/v1
    model: embed-default

  rerank_default:
    provider: llamacpp
    base_url: http://llama-rerank:8080/v1
    model: rerank-default
```

## 4.2. Внутренний API-контракт

Gateway должен предоставлять:

```text
POST /v1/chat/completions
POST /v1/responses
POST /v1/embeddings
POST /v1/rerank
GET  /v1/models
GET  /health
GET  /metrics
```

`/v1/rerank` является фактическим индустриальным расширением: OpenAI не задаёт универсальный rerank API, но этот endpoint уже поддерживается и OpenRouter, и актуальным `llama-server`.

### 4.2.1. Обязательные возможности gateway

- model alias resolution;
- таймауты по типу операции;
- retry только для безопасных/idempotent случаев;
- circuit breaker;
- provider fallback по явной политике;
- request/response size limits;
- redaction секретов и пользовательских данных в логах;
- token/usage accounting;
- OpenTelemetry spans;
- нормализация ошибок;
- capability registry;
- startup smoke tests;
- per-tenant budgets;
- deterministic request IDs.

### 4.2.2. Capability registry

Нельзя предполагать, что любая модель поддерживает всё. При старте выполняются проверки:

```yaml
capabilities:
  qwen/qwen3.6-35b-a3b:
    chat: true
    streaming: true
    tools: smoke_test
    json_schema: smoke_test
    vision: disabled_initially

  qwen/qwen3-embedding-4b:
    embeddings: true
    configurable_dimensions: smoke_test

  cohere/rerank-v3.5:
    rerank: true
```

Если capability test не прошёл, соответствующий alias не публикуется как healthy.

## 4.3. Проверенные модели OpenRouter на 2026-07-24

Доступность проверена по официальным страницам OpenRouter. Цены не фиксируются в архитектуре, потому что они могут изменяться.

| Роль | Model slug | Статус | Примечание |
|---|---|---:|---|
| Генератор / временный judge | `qwen/qwen3.6-35b-a3b` | подтверждён | 35B total / 3B active, большой контекст, tool use и structured output заявлены провайдером |
| Embeddings, дешёвый baseline | `qwen/qwen3-embedding-0.6b` | подтверждён | быстрый кандидат для latency/cost baseline |
| Embeddings, default | `qwen/qwen3-embedding-4b` | подтверждён | рекомендуемый стартовый баланс качества и стоимости |
| Embeddings, quality challenger | `qwen/qwen3-embedding-8b` | подтверждён | использовать в ablation и для сложных multilingual корпусов |
| Remote reranker | `cohere/rerank-v3.5` | подтверждён | временная удалённая реализация `/v1/rerank` |

Прямые ссылки:

- https://openrouter.ai/qwen/qwen3.6-35b-a3b
- https://openrouter.ai/qwen/qwen3-embedding-0.6b
- https://openrouter.ai/qwen/qwen3-embedding-4b
- https://openrouter.ai/qwen/qwen3-embedding-8b
- https://openrouter.ai/cohere/rerank-v3.5
- https://openrouter.ai/docs/api/reference/embeddings
- https://openrouter.ai/docs/api/api-reference/rerank/create-rerank

### Решение для текущей фазы

- `generator_fast`, `generator_deep`, `verifier` временно могут указывать на один Qwen3.6 endpoint, но использовать разные prompts, budgets и temperatures.
- Для финальной оценки качества нужен независимый judge или human review, потому что генератор и judge одной модели имеют коррелированные ошибки.
- Default embeddings: `qwen/qwen3-embedding-4b`, output dimension 1024, если endpoint подтверждает параметр `dimensions`.
- Challenger: `qwen/qwen3-embedding-8b` в той же размерности, чтобы не менять OpenSearch schema.
- Remote reranker: `cohere/rerank-v3.5`.
- Qwen3 Reranker не является обязательной удалённой зависимостью, пока конкретный slug и `/rerank` capability не подтверждены автоматическим smoke test.

## 4.4. Целевой локальный serving через llama.cpp

Рекомендуются **три отдельных контейнера**, а не один универсальный процесс:

```text
llama-chat      -> /v1/chat/completions, /v1/responses
llama-embed     -> /v1/embeddings
llama-rerank    -> /v1/rerank
```

Причины:

- разные модели и memory profiles;
- независимые restart/upgrade;
- разные batch sizes;
- отдельные GPU или GPU fractions;
- reranking и embeddings не должны занимать chat slots;
- проще контролировать latency SLO.

Актуальный `llama-server` поддерживает OpenAI-совместимые chat, responses и embeddings routes, endpoint reranking, parallel decoding, continuous batching, metrics и multi-user режим.

Пример целевого запуска chat-сервера:

```bash
docker run --rm --gpus all \
  --name llama-chat \
  -p 8081:8080 \
  -v ./models:/models:ro \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/generator.gguf \
  --alias generator-fast \
  --host 0.0.0.0 \
  --port 8080 \
  --n-gpu-layers 99 \
  --parallel 8 \
  --ctx-size 65536 \
  --metrics \
  --api-key-file /run/secrets/llama_api_keys
```

Embedding server:

```bash
docker run --rm --gpus all \
  --name llama-embed \
  -p 8082:8080 \
  -v ./models:/models:ro \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/embedding.gguf \
  --alias embed-default \
  --embedding \
  --pooling last \
  --host 0.0.0.0 \
  --port 8080 \
  --metrics
```

Reranker server:

```bash
docker run --rm --gpus all \
  --name llama-rerank \
  -p 8083:8080 \
  -v ./models:/models:ro \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/bge-reranker-v2-m3.gguf \
  --alias rerank-default \
  --reranking \
  --host 0.0.0.0 \
  --port 8080 \
  --metrics
```

Конкретные pooling mode, chat template, quantization и GGUF должны пройти отдельный compatibility test для каждого артефакта. Не следует считать, что Hugging Face checkpoint автоматически корректно работает после конвертации.

### 4.4.1. Требования к локальному model qualification

Каждая версия GGUF проходит:

1. checksum и provenance verification;
2. tokenizer parity test;
3. chat-template test;
4. multilingual Russian/English smoke set;
5. JSON schema test;
6. tool-call test;
7. citation-format test;
8. context-length degradation test;
9. concurrency test;
10. GPU memory and throughput benchmark;
11. embeddings normalization test;
12. reranker monotonicity/sanity test.

### 4.4.2. Docker image policy

- pin exact image tag и digest;
- не использовать mutable `latest` в production;
- выбирать `server`, `server-cuda` или `server-cuda13` по реальному driver/runtime;
- CUDA присутствует только в `llama-*` и optional Docling VLM workers;
- API, retrieval, Tika, libzim, PostgreSQL и OpenSearch не должны зависеть от CUDA;
- model files монтируются read-only;
- model server не публикуется наружу, доступ только из internal network через Model Gateway.

Официальные ссылки:

- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md

---

## 5. Слой данных

## 5.1. PostgreSQL

PostgreSQL хранит control-plane данные:

- users, tenants, roles;
- knowledge bases;
- document metadata и версии;
- ingestion jobs;
- parser templates и их версии;
- model registry;
- index versions и aliases;
- chat sessions;
- query runs;
- evaluation datasets/runs;
- audit log;
- agent checkpoints;
- human feedback.

### Базовые таблицы

```text
tenants
users
tenant_memberships
knowledge_bases
knowledge_base_versions
documents
document_versions
document_artifacts
ingestion_jobs
parser_templates
parser_template_versions
chunk_manifests
index_versions
model_aliases
query_runs
retrieval_runs
agent_runs
agent_steps
citation_checks
evaluation_sets
evaluation_cases
evaluation_runs
feedback
```

## 5.2. MinIO/S3

Object storage хранит:

- оригинальный upload;
- Wikimedia XML dump snapshots;
- future ZIM snapshots;
- normalized Docling JSON;
- Tika text/metadata output;
- page images и extracted figures;
- OCR artifacts;
- chunk manifests JSONL;
- failed-parser diagnostics;
- exported evaluation reports;
- trace payloads, если они слишком велики для PostgreSQL.

Рекомендуемая структура ключей:

```text
s3://rag-artifacts/{tenant_id}/{knowledge_base_id}/{document_id}/{version_id}/
  original/{filename}
  parse/docling.json
  parse/tika.json
  normalized/document.json
  chunks/chunks.jsonl
  previews/page-0001.png
  figures/figure-0001.png
  logs/ingestion-report.json
```

## 5.3. OpenSearch

OpenSearch хранит online-search representation:

- chunk text;
- title/section text;
- lexical fields;
- dense vectors;
- tenant/KB/document filters;
- source pointers;
- parser/template/model versions;
- selected enrichment fields.

Не следует использовать OpenSearch как единственный источник истины. Chunks должны быть воспроизводимы из versioned artifacts.

## 5.4. Redis/Valkey

Используется для:

- queues;
- distributed rate limits;
- request deduplication;
- short-lived query cache;
- agent locks;
- session streaming state;
- health/circuit-breaker counters;
- concurrency semaphores.

Redis не хранит единственную копию критичных данных.

---

## 6. Multi-tenancy и изоляция

Каждая сущность несёт `tenant_id`. Для OpenSearch возможны две схемы:

### Вариант A: shared index + mandatory filters

Рекомендуется для первой версии и небольших/средних tenants.

Плюсы:

- меньше shards;
- проще migrations;
- выше utilization.

Требования:

- tenant filter добавляется внутри retrieval service, а не принимается из клиентского body;
- authorization test на каждый search path;
- document-level access filters включаются и в BM25, и в vector query;
- trace payload не должен раскрывать чужие документы.

### Вариант B: index per tenant/large knowledge base

Использовать для крупных клиентов, специальных retention policies или regulatory isolation.

Рекомендуемый компромисс:

- shared index для большинства tenants;
- dedicated index tier для крупных;
- routing через `index_alias` в PostgreSQL.

---

## 7. Универсальный ingestion произвольных документов

## 7.1. Общая схема

```mermaid
flowchart LR
    U[Upload / URL / Wikipedia XML / ZIM / connector]
    S[Security & MIME detection]
    R[Parser router]
    D[Docling]
    T[Apache Tika]
    Z[Wikipedia XML adapter / future libzim]
    N[Canonical normalization]
    E[Structural enrichment]
    C[Template-based chunking]
    Q[Chunk QA]
    B[Batch embeddings]
    I[Index staging]
    V[Validation]
    P[Atomic publish alias]

    U --> S --> R
    R --> D
    R --> T
    R --> Z
    D --> N
    T --> N
    Z --> N
    N --> E --> C --> Q --> B --> I --> V --> P
```

### 7.1.1. Состояния ingestion job

```text
RECEIVED
SECURITY_CHECK
STORED
TYPE_DETECTED
PARSING
NORMALIZING
ENRICHING
CHUNKING
EMBEDDING
INDEXING_STAGING
VALIDATING
PUBLISHED
FAILED
CANCELLED
```

Каждый переход идемпотентен. Job можно безопасно повторить с последнего checkpoint.

## 7.2. Parser routing

### 7.2.1. Docling — основной parser для структурных документов

Использовать для:

- PDF;
- DOCX/PPTX/XLSX;
- ODT/ODS/ODP;
- images;
- EPUB;
- HTML/Markdown/AsciiDoc/LaTeX;
- таблиц, layout и page-aware provenance;
- scanned documents с OCR;
- документов, где нужна реконструкция структуры.

Docling отдаёт единое document representation и поддерживает экспорт chunks JSONL, но собственный chunking layer всё равно должен оставаться нашим, чтобы шаблоны и эксперименты не зависели от parser implementation.

### 7.2.2. Apache Tika — detector, metadata extractor и fallback

Использовать для:

- MIME detection;
- metadata extraction;
- legacy Office;
- RTF, email, archives и широкий long-tail форматов;
- простого text fallback;
- быстрой preliminary extraction;
- случаев, когда layout не важен;
- диагностики расхождений с Docling.

Tika работает как отдельный CPU-сервис на порту 9998. Внешний доступ запрещён.

### 7.2.3. Wikimedia XML и future libzim — специализированный путь Wikipedia/Kiwix

Wikimedia XML dump и ZIM не следует прогонять через generic parser. Используется собственный adapter:

- потоковое чтение XML или перечисление ZIM entries;
- redirects;
- canonical title/URL;
- HTML extraction;
- section hierarchy;
- intra-wiki links;
- page revision/snapshot metadata;
- deterministic chunk IDs.

### 7.2.4. Parser decision tree

```text
Wikimedia XML multistream -----------> XML stream adapter
ZIM ---------------------------------> future libzim adapter
PDF / Office / image / EPUB ---------> Docling
Legacy/unknown/archive/email --------> Tika first
HTML/Markdown/plain text ------------> lightweight/Docling
Docling failure ---------------------> Tika fallback
Tika produced low-structure output --> mark degraded; optional manual review
Complex scan/table/formula ----------> Docling OCR/VLM profile
```

## 7.3. Security before parsing

Произвольные uploads считаются недоверенными.

Обязательные меры:

- maximum file size и maximum uncompressed size;
- archive depth/entry limits;
- zip bomb protection;
- MIME detection по содержимому, не только extension;
- filename normalization;
- path traversal protection;
- optional ClamAV scan;
- parser containers без host mounts и без Docker socket;
- read-only root filesystem, где возможно;
- no outbound network для parsing workers по умолчанию;
- CPU/memory/time limits;
- PDF password handling через explicit secret, не логировать пароль;
- HTML sanitization;
- запрет embedded scripts/macros;
- retention policy для failed uploads;
- audit trail.

## 7.4. Canonical document model

Все parsers преобразуются в одну внутреннюю схему.

```json
{
  "document_id": "uuid",
  "version_id": "uuid",
  "source_type": "pdf",
  "source_uri": "s3://...",
  "title": "...",
  "language": "ru",
  "metadata": {},
  "nodes": [
    {
      "node_id": "n-001",
      "type": "heading|paragraph|list|table|figure|code|formula",
      "text": "...",
      "level": 2,
      "section_path": ["Глава 1", "Архитектура"],
      "page": 12,
      "bbox": [0.1, 0.2, 0.8, 0.4],
      "parent_id": "n-000",
      "prev_id": null,
      "next_id": "n-002",
      "provenance": {
        "parser": "docling",
        "parser_version": "...",
        "artifact_pointer": "s3://..."
      }
    }
  ]
}
```

Canonical model должен сохранять структуру, а не только plain text. Это необходимо для:

- template-based chunking;
- таблиц;
- page citations;
- neighbor expansion;
- preview;
- повторного chunking без повторного OCR;
- migration между embedding models.

---

## 8. Шаблоны parsing/chunking в стиле RAGFlow

## 8.1. Концепция

Пользователь выбирает **Document Template** при создании knowledge base или для отдельного документа. Template — версионируемый declarative pipeline, а не жёстко зашитый Python class.

Template управляет:

- parser route;
- OCR/table/layout options;
- normalization;
- node filters;
- chunking strategy;
- max/min tokens;
- overlap policy;
- table policy;
- image caption policy;
- metadata extraction;
- enrichment;
- embedding model alias;
- index mapping/profile;
- retrieval defaults;
- validation rules.

## 8.2. Начальный набор шаблонов

| Template | Для чего | Основной parser | Chunking |
|---|---|---|---|
| `general` | универсальные документы | Docling → Tika fallback | section-aware, 300–450 tokens |
| `wikipedia` | Wikipedia XML/ZIM | XML stream adapter / future libzim | heading/paragraph aware |
| `book` | книги, manuals | Docling | chapter/section, neighbor links |
| `paper` | научные статьи | Docling | abstract/method/results/references aware |
| `legal` | законы, договоры | Docling/Tika | article/clause hierarchy, minimal overlap |
| `qa_faq` | готовые пары вопрос-ответ | Docling/Tika | one QA pair per chunk |
| `presentation` | PPTX/PDF slides | Docling | slide + notes + adjacent slide relation |
| `spreadsheet_table` | XLSX/CSV и таблицы | Docling/Tika | table-aware row groups + headers |
| `scanned_ocr` | scans/images | Docling OCR | page-aware, confidence thresholds |
| `code_docs` | docs/API/code text | lightweight/Docling | heading + code block aware |
| `web_html` | веб-страницы | Docling/Tika | boilerplate removal + sections |
| `one_chunk` | маленькие документы | любой | один документ = один chunk |
| `custom` | пользовательский | configurable | declarative pipeline |

## 8.3. Пример template YAML

```yaml
id: general
version: 3
parser:
  primary: docling
  fallback: tika
  docling:
    ocr: auto
    tables: true
    generate_page_images: false
normalization:
  remove_headers_footers: true
  dehyphenate: true
  preserve_lists: true
  preserve_tables: true
chunking:
  strategy: section_aware
  target_tokens: 360
  min_tokens: 100
  max_tokens: 520
  overlap:
    mode: structural
    max_tokens: 48
  keep_heading_prefix: true
  tables:
    mode: atomic_or_row_groups
  neighbor_links: true
enrichment:
  language_detection: true
  keywords: false
  summary: false
embedding:
  model_alias: embed_default
  batch_size: 64
index:
  profile: hybrid_v1
validation:
  max_empty_ratio: 0.05
  require_source_pointer: true
  fail_on_no_chunks: true
```

## 8.4. Template lifecycle

- Template version immutable после публикации.
- Изменение создаёт новую версию.
- Document version хранит exact template version.
- Re-index может использовать старый parse artifact и новый chunking template.
- UI показывает diff template versions.
- Knowledge base имеет default template, документ может override.
- Template может быть cloned пользователем, но system templates read-only.

## 8.5. Human-in-the-loop

UI должен позволять:

- preview parsed structure;
- видеть pages/sections/tables;
- видеть границы chunks;
- merge/split/edit chunks;
- исключать страницы/sections;
- добавлять tags/keywords;
- повторно запустить только chunking+indexing;
- сравнить templates на одном документе;
- отметить parser result как degraded.

Ручные изменения сохраняются как patch поверх canonical document, а не модифицируют исходный parser artifact.

---

## 9. Wikipedia ingestion

## 9.1. Источник

Для local MVP основным доступным источником является Wikimedia XML `pages-articles` bzip2 multistream dump и соответствующий multistream index. ZIM + libzim остаются будущим специализированным адаптером.

Хранить:

- XML dump checksum;
- multistream index checksum;
- snapshot date;
- language;
- source/catalog metadata;
- parser/adapter version;
- extraction config;
- index version.

Index validation:

- index is UTF-8 bzip2 text;
- each row has `offset:page_id:title`;
- offsets are monotonic non-decreasing, because multiple pages can share one compressed stream;
- unique offsets are grouped into stream jobs;
- sampled unique offsets point inside the compressed XML file and start with a bzip2 stream signature.

## 9.2. Page representation

```text
snapshot_id
language
page_id
namespace
canonical_title
canonical_url
redirect_target
section_tree
plain_text
normalized_html_hash
outgoing_links
categories (если доступны)
```

## 9.3. Chunk metadata

```text
chunk_id
document_id/page_id
section_path
paragraph_start/end
char_offsets
token_count
prev_chunk_id
next_chunk_id
source_url
snapshot_id
content_hash
```

`chunk_id` должен быть детерминированным, например hash от:

```text
snapshot_id + page_id + section_path + normalized_text_hash
```

Это упрощает incremental re-index и сравнение версий.

## 9.4. Chunking Wikipedia

Рекомендуемый default:

- section-aware;
- sentence boundaries;
- target 300–450 tokens;
- hard max около 550 tokens;
- маленький structural overlap только внутри section;
- heading path добавляется в retrieval text;
- infobox/table рассматривается отдельно;
- redirects индексируются как aliases, а не отдельный дублирующий текст;
- соседние chunks связываются, но не всегда сразу помещаются в контекст.

Почему не фиксированный overlap 50 tokens:

- он дублирует факты и искажает reranking;
- увеличивает индекс;
- ухудшает diversity;
- может разрывать структуру;
- structural overlap и neighbor expansion дают лучший контроль.

---

## 10. Индекс OpenSearch

## 10.1. Индексная стратегия

Использовать versioned physical indices и aliases:

```text
kb-chunks-v001
kb-chunks-v002
kb-chunks-read  -> активная версия
kb-chunks-write -> staging/current writer
```

Новая версия строится в staging index, валидируется, затем alias переключается атомарно.

## 10.2. Пример mapping

```json
{
  "settings": {
    "index": {
      "knn": true,
      "number_of_shards": 3,
      "number_of_replicas": 1
    }
  },
  "mappings": {
    "properties": {
      "tenant_id": { "type": "keyword" },
      "knowledge_base_id": { "type": "keyword" },
      "document_id": { "type": "keyword" },
      "document_version_id": { "type": "keyword" },
      "chunk_id": { "type": "keyword" },
      "language": { "type": "keyword" },
      "title": {
        "type": "text",
        "fields": { "keyword": { "type": "keyword" } }
      },
      "section_path_text": { "type": "text" },
      "content": { "type": "text" },
      "content_vector": {
        "type": "knn_vector",
        "dimension": 1024,
        "space_type": "cosinesimil",
        "method": {
          "name": "hnsw",
          "engine": "faiss",
          "parameters": {
            "ef_construction": 128,
            "m": 24
          }
        }
      },
      "page_start": { "type": "integer" },
      "page_end": { "type": "integer" },
      "source_uri": { "type": "keyword", "index": false },
      "source_url": { "type": "keyword", "index": false },
      "prev_chunk_id": { "type": "keyword" },
      "next_chunk_id": { "type": "keyword" },
      "template_id": { "type": "keyword" },
      "template_version": { "type": "integer" },
      "embedding_model": { "type": "keyword" },
      "embedding_version": { "type": "keyword" },
      "content_hash": { "type": "keyword" },
      "access_tags": { "type": "keyword" }
    }
  }
}
```

Параметры HNSW нельзя считать финальными без benchmark. `m`, `ef_construction`, `ef_search`, shard count и compression выбираются на реальном корпусе.

## 10.3. Page и chunk representations

Необходимо разделять:

- **page/document representation** — title, summary, entities, document-level embedding;
- **chunk representation** — точный evidence fragment.

В первой версии page-level vector можно не включать в основной запрос, но data model должен его позволять. Это пригодится для routing и multi-hop.

---

## 11. Обычный retrieval pipeline

```mermaid
flowchart LR
    Q[User query]
    N[Normalize + language/entities]
    B[BM25 top 100]
    V[Dense HNSW top 100]
    F[RRF fusion]
    R[Cross-encoder rerank top 40-60]
    D[Dedup + page quota]
    X[Neighbor expansion]
    C[Token-budget context pack]
    G[Generator]
    CV[Citation validator]
    A[Answer]

    Q --> N
    N --> B
    N --> V
    B --> F
    V --> F
    F --> R --> D --> X --> C --> G --> CV --> A
```

## 11.1. Query normalization

Без LLM по умолчанию:

- Unicode normalization;
- whitespace cleanup;
- language detection;
- basic typo-safe lexical form;
- entity hints;
- quoted phrases;
- date/number normalization;
- user filters;
- access filters.

LLM rewrite включается только при конкретном trigger:

- first retrieval coverage низкий;
- multi-hop intent;
- ambiguous entity;
- long conversational reference;
- explicit Extended Search.

## 11.2. Parallel first-stage retrieval

BM25 и dense search запускаются параллельно через `asyncio.gather`.

Рекомендуемые стартовые значения:

```yaml
bm25_top_k: 100
dense_top_k: 100
fusion_top_k: 60
rerank_top_k: 50
final_evidence_chunks: 8-12
```

Значения являются стартовыми, а не догмой.

## 11.3. BM25

BM25 search использует разные boosts:

```text
title^3.0
section_path_text^1.8
content^1.0
keywords^1.2
```

Следует хранить:

- raw `_score`;
- matched fields;
- matched terms, где возможно;
- rank;
- query clause;
- filters.

OpenSearch `_explain` слишком дорог для каждого кандидата. Использовать:

- только top-N в debug mode;
- sampling;
- evaluation runs;
- explicit retrieval debugger request.

## 11.4. Dense retrieval

- cosine similarity;
- HNSW;
- one fixed vector dimension per index version;
- query embedding и document embedding через один alias/version;
- batch indexing;
- normalized vectors, если это требует model contract;
- explicit `input_type`, если provider/model его поддерживает.

## 11.5. Fusion

RRF является default, потому что:

- BM25 и dense scores имеют разные шкалы;
- не требует score calibration;
- устойчив к отдельным выбросам;
- легко объясним по ranks.

Сохранять вклад каждого retriever:

```json
{
  "chunk_id": "...",
  "bm25_rank": 2,
  "dense_rank": 17,
  "rrf_bm25": 0.0161,
  "rrf_dense": 0.0130,
  "rrf_total": 0.0291
}
```

OpenSearch имеет native RRF/rank processor, но собственный fusion layer в retrieval service полезен для прозрачности, экспериментов и одинаковой логики между версиями. Native RRF можно сравнить в ablation.

## 11.6. Reranking

Reranker получает top 40–60 кандидатов. В input включаются:

```text
query
page title
section path
chunk content
```

Не включать лишнюю metadata, которая раздувает input и не помогает релевантности.

Remote phase:

- OpenRouter `/v1/rerank`;
- `cohere/rerank-v3.5` как baseline.

Local phase:

- отдельный `llama-server --reranking`;
- `bge-reranker-v2-m3` как первый практический кандидат;
- Qwen reranker как challenger после GGUF/capability validation.

## 11.7. Post-rerank policies

После reranker применяются детерминированные правила:

- near-duplicate removal;
- max 2–3 chunks с одной страницы по умолчанию;
- document diversity;
- section diversity;
- access validation;
- language preference;
- neighbor expansion;
- token budget;
- source availability check.

Важно логировать отдельно **model score** и **policy decision**, чтобы не путать релевантность с бизнес-правилами.

## 11.8. Neighbor expansion

Соседний chunk добавляется, если:

- selected chunk начинается/заканчивается незавершённой мыслью;
- вопрос требует списка или последовательности;
- сосед содержит heading context;
- reranker выбрал несколько смежных fragments;
- есть свободный token budget.

Neighbor не должен автоматически получать score selected chunk. В trace указывается причина `neighbor_of:<chunk_id>`.

## 11.9. Context packing

Не фиксированный top-k, а token-budget packing.

Приоритет:

1. answer coverage;
2. source diversity;
3. reranker score;
4. non-duplication;
5. logical adjacency;
6. stable citation IDs.

Каждый evidence block получает machine-readable ID:

```text
[S1] Wikipedia / Transformer / Architecture
[S2] Uploaded PDF / page 14 / 3.2 Attention
```

---

## 12. Генерация ответа и ссылки

## 12.1. Prompt contract

Генератор должен:

- использовать только provided evidence для factual claims;
- явно сообщать, когда evidence недостаточно;
- ставить citation ID после утверждения;
- не ссылаться на источник, который не подтверждает claim;
- отделять inference от extracted fact;
- не придумывать URL/page;
- не раскрывать hidden prompts или internal scores пользователю.

## 12.2. Structured answer draft

Лучше сначала получать JSON draft:

```json
{
  "answer_markdown": "...",
  "claims": [
    {
      "claim_id": "c1",
      "text": "...",
      "evidence_ids": ["S1", "S3"],
      "type": "fact|inference"
    }
  ],
  "insufficient_evidence": false
}
```

Затем приложение рендерит итоговый ответ и ссылки.

## 12.3. Deterministic citation validator

До отправки пользователю:

- все citation IDs существуют;
- каждый ID был включён в prompt;
- URL/page pointer валиден;
- нет claims без evidence, если claim factual;
- нет unused/phantom citations;
- source access разрешён пользователю;
- цитата ведёт на точную page/section/chunk.

## 12.4. Claim verifier

Verifier — второй model pass только когда:

- Extended Search;
- high-risk mode;
- длинный ответ;
- много источников;
- conflicting evidence;
- evaluation run.

В обычном low-latency режиме достаточно deterministic validator плюс optional sampled verifier.

---

## 13. Extended Search / agent mode

## 13.1. Когда включается

Ручной toggle пользователя или confidence gate:

- multi-hop вопрос;
- сравнение нескольких сущностей;
- aggregation/list completion;
- временная линия;
- conflicting sources;
- first-pass coverage низкий;
- требуется follow links;
- вопрос по нескольким knowledge bases;
- обычный retrieval не нашёл достаточных доказательств.

## 13.2. Инструменты агента

Минимальный whitelist:

```text
search
fetch_chunk
fetch_document
fetch_section
follow_links
search_within_document
compare_evidence
verify_claims
finish
```

Не давать агенту shell/filesystem/network general-purpose tools в production query path.

## 13.3. Agent loop

```text
classify intent
  -> create bounded plan/subqueries
  -> execute independent searches in parallel
  -> update evidence ledger
  -> measure coverage/conflicts
  -> rewrite or follow links if needed
  -> build claim-evidence graph
  -> verify
  -> finish
```

## 13.4. Evidence ledger

```json
{
  "questions": [
    {
      "subquery_id": "q1",
      "text": "...",
      "status": "covered|partial|missing",
      "evidence_ids": ["S1", "S2"]
    }
  ],
  "conflicts": [],
  "open_questions": [],
  "visited_pages": [],
  "tool_call_hashes": []
}
```

## 13.5. Жёсткие budgets

Стартовые ограничения:

```yaml
max_steps: 8
max_subqueries: 6
max_rewrites_per_subquery: 2
max_parallel_tool_calls: 4
max_unique_documents: 20
max_total_retrieved_chunks: 300
max_context_tokens: 30000
max_wall_time_seconds: 90
```

Budgets должны быть tenant/config specific.

## 13.6. Loop breaker

Остановить agent loop, если:

- повторяется одинаковый tool call;
- новых evidence chunks нет;
- coverage не улучшается два шага;
- reached budget;
- evidence sufficient;
- источник недоступен;
- conflict нельзя разрешить локальными источниками.

## 13.7. Почему один orchestrator

Multi-agent swarm пока не нужен:

- дороже;
- труднее отлаживать;
- больше correlated loops;
- сложнее attribution;
- хуже latency predictability.

Паттерны из `deepagents-gigachat` полезны как источник идей для tool contracts, state, middleware, loop breakers и harness evaluation, но его нельзя переносить напрямую как готового Wikipedia-агента.

Ссылка:

- https://github.com/ai-forever/deepagents-gigachat

---

## 14. Наблюдаемость и explainability

## 14.1. Один trace на запрос

Каждый пользовательский запрос получает:

```text
trace_id
query_run_id
tenant_id
session_id
knowledge_base_ids
mode: normal|extended
```

## 14.2. Spans

Пример hierarchy:

```text
query
  auth
  query_normalization
  embedding_query
  retrieve_bm25
  retrieve_dense
  fuse_rrf
  rerank
  postprocess
  context_pack
  generate
  citation_validate
  verify_optional
```

Для agent mode:

```text
agent_run
  classify
  plan
  step_1
    tool_search_q1
    tool_search_q2
  coverage_check
  step_2
  final_verify
```

## 14.3. Retrieval event schema

Generic LLM tracing недостаточно. Нужна своя append-only схема.

```json
{
  "event_type": "retrieval_candidate",
  "trace_id": "...",
  "stage": "bm25|dense|rrf|rerank|policy|context",
  "query_variant_id": "q0",
  "chunk_id": "...",
  "rank_before": 17,
  "rank_after": 4,
  "scores": {
    "bm25": 8.43,
    "cosine": 0.782,
    "rrf": 0.028,
    "rerank": 0.941
  },
  "contributions": {
    "bm25_rank": 2,
    "dense_rank": 17
  },
  "decision": "selected|dropped|neighbor_added",
  "reason_codes": ["PAGE_QUOTA", "NEAR_DUPLICATE"],
  "model_versions": {},
  "latency_ms": 12
}
```

## 14.4. Что сохранять обязательно

- original query;
- conversation-derived query;
- detected language;
- normalized query;
- rewritten/decomposed queries;
- filters;
- retriever config/version;
- BM25 candidates/scores/ranks;
- dense candidates/similarity/ranks;
- RRF contribution;
- reranker input order/output;
- duplicates и причины удаления;
- page/document quotas;
- neighbor expansion;
- final context order;
- model alias/provider/slug/version;
- prompts version;
- generation parameters;
- citations;
- claim-to-evidence mapping;
- agent state transitions;
- tool inputs/outputs;
- token usage;
- queue wait и stage latency;
- error/retry/fallback.

## 14.5. Human-readable retrieval debugger

UI вкладка показывает:

1. Query transformations.
2. BM25 и dense lists рядом.
3. RRF table.
4. Reranker score delta.
5. Dropped candidates с reason code.
6. Final context order.
7. Claim → source mapping.
8. Полный trace timeline.
9. Сравнение двух pipeline versions.

## 14.6. Stack

- OpenTelemetry SDK/Collector;
- Phoenix self-hosted для LLM/retrieval traces;
- Prometheus для metrics;
- Grafana dashboards;
- structured JSON logs в Loki/OpenSearch optional;
- PostgreSQL/MinIO для durable query artifacts.

---

## 15. API design

## 15.1. Public application API

```text
POST   /api/v1/chat
GET    /api/v1/chat/{run_id}/events
GET    /api/v1/chat/{run_id}

POST   /api/v1/knowledge-bases
GET    /api/v1/knowledge-bases
GET    /api/v1/knowledge-bases/{id}
PATCH  /api/v1/knowledge-bases/{id}
DELETE /api/v1/knowledge-bases/{id}

POST   /api/v1/knowledge-bases/{id}/documents
POST   /api/v1/knowledge-bases/{id}/documents:from-url
GET    /api/v1/documents/{id}
GET    /api/v1/documents/{id}/versions
POST   /api/v1/documents/{id}:reprocess
DELETE /api/v1/documents/{id}

GET    /api/v1/ingestion-jobs/{id}
GET    /api/v1/ingestion-jobs/{id}/events
POST   /api/v1/ingestion-jobs/{id}:cancel

GET    /api/v1/templates
POST   /api/v1/templates
POST   /api/v1/templates/{id}:clone
GET    /api/v1/templates/{id}/versions/{version}

POST   /api/v1/search
POST   /api/v1/search:debug
GET    /api/v1/traces/{trace_id}

POST   /api/v1/evaluations/runs
GET    /api/v1/evaluations/runs/{id}
```

## 15.2. Upload flow

Для больших файлов:

1. API создаёт upload session.
2. Клиент загружает напрямую в MinIO через presigned URL.
3. Клиент завершает upload.
4. API создаёт ingestion job.
5. SSE/WebSocket показывает progress.

Нельзя передавать гигабайтный файл через FastAPI worker целиком в память.

## 15.3. Chat request

```json
{
  "message": "...",
  "knowledge_base_ids": ["..."],
  "mode": "normal|extended|auto",
  "filters": {
    "document_ids": [],
    "tags": [],
    "languages": ["ru", "en"]
  },
  "debug": false
}
```

`debug=true` разрешается только пользователям с соответствующей ролью.

---

## 16. UI

## 16.1. Основные экраны

1. **Chat** — citations, mode toggle, source drawer.
2. **Knowledge Bases** — список, размер, health, active index version.
3. **Document Upload** — drag-and-drop, URL, Wikipedia XML/ZIM, template selector.
4. **Ingestion Jobs** — progress, stages, warnings, retries.
5. **Document Preview** — pages, canonical structure, chunks.
6. **Template Studio** — clone/edit/version/compare.
7. **Retrieval Debugger** — BM25/dense/RRF/rerank/context.
8. **Trace Viewer** — stage timeline и model calls.
9. **Evaluation** — datasets, runs, regressions.
10. **Models** — aliases, provider, capability/health.

## 16.2. MVP границы

Первая UI-версия может включать только:

- login;
- KB CRUD;
- upload;
- job status;
- document/chunk preview;
- chat;
- source panel;
- debug trace JSON.

Template Studio и визуальное сравнение retrieval можно добавить следующим этапом.

---

## 17. Docker topology

## 17.1. CPU profile

```text
api x2
ui
model-gateway
retrieval-worker x2+
ingestion-orchestrator
docling-standard-worker
tika-server
zim-worker
postgres
redis
minio
opensearch-node1
opensearch-node2
opensearch-dashboards optional
otel-collector
phoenix
prometheus
grafana
```

## 17.2. GPU profile — целевой local mode

```text
llama-chat
llama-embed
llama-rerank
docling-vlm-worker optional
```

Только эти контейнеры получают NVIDIA device reservation.

## 17.3. Compose profiles

```yaml
profiles:
  remote-models:
    - model-gateway

  local-models:
    - llama-chat
    - llama-embed
    - llama-rerank

  ingest-basic:
    - docling-standard-worker
    - tika-server
    - zim-worker

  ingest-vlm:
    - docling-vlm-worker
```

## 17.4. Упрощённый compose fragment

```yaml
services:
  api:
    build: ./services/api
    environment:
      DATABASE_URL: postgresql+asyncpg://rag:${POSTGRES_PASSWORD}@postgres/rag
      REDIS_URL: redis://redis:6379/0
      MODEL_GATEWAY_URL: http://model-gateway:4000/v1
      OPENSEARCH_URL: http://opensearch:9200
      S3_ENDPOINT: http://minio:9000
    depends_on:
      - postgres
      - redis
      - opensearch
      - minio

  model-gateway:
    build: ./services/model-gateway
    environment:
      MODEL_PROVIDER: ${MODEL_PROVIDER:-openrouter}
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
      MODEL_REGISTRY_PATH: /config/models.yaml
    volumes:
      - ./config/models.yaml:/config/models.yaml:ro

  tika:
    image: apache/tika:${TIKA_IMAGE_TAG}
    expose:
      - "9998"
    networks:
      - backend

  opensearch:
    image: opensearchproject/opensearch:${OPENSEARCH_VERSION}
    environment:
      discovery.type: single-node
      plugins.security.disabled: "true"
    volumes:
      - opensearch-data:/usr/share/opensearch/data

  llama-chat:
    profiles: ["local-models"]
    image: ghcr.io/ggml-org/llama.cpp:${LLAMA_SERVER_IMAGE}
    command:
      - -m
      - /models/generator.gguf
      - --alias
      - generator-fast
      - --host
      - 0.0.0.0
      - --port
      - "8080"
      - --n-gpu-layers
      - "99"
      - --parallel
      - "8"
      - --metrics
    volumes:
      - ./models:/models:ro
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Это skeleton, не полный production compose. В production нужны secrets, healthchecks, resource limits, TLS boundary, backups и pinned digests.

---

## 18. Масштабирование и производительность

## 18.1. Более 10 пользователей

Архитектура должна различать:

- connected users;
- simultaneous retrieval requests;
- simultaneous generation slots;
- simultaneous extended searches;
- ingestion concurrency.

Один GPU не обязан одновременно генерировать десять длинных ответов без очереди. Production support означает:

- API принимает и стримит запросы;
- retrieval масштабируется горизонтально;
- model server использует continuous batching/parallel slots;
- очередь и backpressure предсказуемы;
- extended mode имеет отдельный лимит;
- пользователь видит queue state;
- p95 не деградирует неконтролируемо.

## 18.2. Рекомендуемые ограничения

```yaml
normal_rag:
  per_tenant_concurrency: 8
  global_retrieval_concurrency: 64
  generation_queue_timeout_s: 30

extended_search:
  per_tenant_concurrency: 2
  global_concurrency: 8
  max_parallel_tools_per_run: 4

ingestion:
  per_tenant_active_jobs: 3
  ocr_concurrency: 2
  embedding_batches_in_flight: 4
```

Значения уточняются load test.

## 18.3. Кэширование

Безопасные caches:

- query embedding cache по normalized query + model version;
- document embedding никогда не пересчитывается при неизменном content hash;
- parser output cache по source hash + parser config;
- retrieval result cache только с tenant/access/filter/index-version key;
- prompt prefix/KV cache в llama.cpp;
- negative cache для недоступных моделей.

Не кэшировать пользовательский ответ без учёта access scope и index version.

## 18.4. Batch processing

- embeddings batch на ingestion;
- rerank batch документов в одном request;
- OpenSearch bulk indexing;
- Wikipedia XML/ZIM pages потоково, без загрузки snapshot в RAM;
- async I/O для object storage;
- bounded queues для memory control.

---

## 19. Reliability

## 19.1. Retry policy

Retry:

- OpenRouter 429/5xx с exponential backoff и jitter;
- object storage transient errors;
- OpenSearch bulk partial failures;
- parser worker crash после checkpoint.

Не retry автоматически:

- invalid document;
- authentication/authorization failure;
- schema validation error;
- deterministic model request error;
- zip bomb/security rejection.

## 19.2. Idempotency

- upload completion имеет idempotency key;
- ingestion job key = source hash + template version + embedding version + target index version;
- bulk index uses deterministic chunk IDs;
- query requests могут иметь client request ID;
- agent tool calls deduplicated по normalized payload hash.

## 19.3. Backups

- PostgreSQL PITR;
- MinIO versioning/replication;
- OpenSearch snapshots;
- model/config repository;
- parser templates export;
- evaluation datasets;
- documented restore drills.

OpenSearch можно перестроить из artifacts, но это не отменяет snapshots для быстрого recovery.

---

## 20. Evaluation и quality gates

## 20.1. Retrieval metrics

- Recall@5/10/20/50;
- nDCG@10;
- MRR;
- Hit Rate;
- context precision;
- context recall;
- page/document diversity;
- duplicate rate;
- evidence coverage;
- answerable retrieval rate.

## 20.2. Generation metrics

- answer correctness;
- faithfulness;
- citation precision;
- citation recall;
- claim coverage;
- source attribution correctness;
- completeness;
- appropriate refusal;
- unsupported claim rate.

## 20.3. Operational metrics

- p50/p95/p99 latency по stage;
- queue wait;
- OpenSearch latency;
- embedding throughput;
- rerank latency;
- generation TTFT/tokens per second;
- GPU memory;
- ingestion docs/pages per minute;
- parser failure/degraded rate;
- cost per query в OpenRouter phase;
- tokens per successful answer.

## 20.4. Evaluation slices

- Russian vs English;
- short factoid;
- long explanatory;
- multi-hop;
- comparison;
- list aggregation;
- ambiguous entity;
- no-answer;
- conflicting sources;
- Wikipedia-only;
- PDF/table;
- scanned OCR;
- multi-document;
- long document.

## 20.5. Ablation plan

Проводить по одному изменению:

1. BM25 only.
2. Dense only.
3. BM25 + dense RRF.
4. + reranker.
5. Chunk 250/350/450/600 tokens.
6. Fixed overlap vs structural overlap.
7. Neighbor expansion off/on.
8. Page quota 1/2/3/unlimited.
9. Qwen3 Embedding 0.6B/4B/8B при одинаковой dimension.
10. Rerank models.
11. Query rewrite off/on-triggered/on-always.
12. Native OpenSearch RRF vs service-side RRF.
13. Normal mode vs Extended Search.
14. Docling vs Tika для supported overlap formats.
15. Template variants для books/papers/tables.

## 20.6. Release gate

Новая pipeline/model/index версия публикуется, если:

- не ухудшает critical retrieval slices;
- citation precision не ниже threshold;
- no-answer false-positive не растёт сверх budget;
- p95 latency укладывается в SLO;
- cost/throughput приемлемы;
- migration rollback протестирован.

---

## 21. Рекомендуемые стартовые модели

## 21.1. Сейчас, OpenRouter-only

```yaml
generator_fast: qwen/qwen3.6-35b-a3b
generator_deep: qwen/qwen3.6-35b-a3b
verifier: qwen/qwen3.6-35b-a3b
embed_default: qwen/qwen3-embedding-4b
embed_challenger: qwen/qwen3-embedding-8b
embed_latency_baseline: qwen/qwen3-embedding-0.6b
rerank_default: cohere/rerank-v3.5
```

## 21.2. Целевой local llama.cpp

```yaml
generator:
  family: Qwen3.6-class or another validated GGUF instruct/tool model
  requirement: chat template + JSON schema + tools + Russian

embedding:
  family: Qwen3 Embedding
  requirement: validated GGUF, 1024-d output, normalization parity

reranker:
  first_candidate: bge-reranker-v2-m3
  challenger: Qwen reranker after GGUF validation
```

Локальная модель выбирается не по leaderboard в вакууме, а по собственному evaluation set, доступной VRAM, concurrency и latency.

---

## 22. Critique исходного baseline

## 22.1. Что оставить

- Wikipedia XML + future ZIM/libzim;
- OpenSearch;
- section-aware chunks;
- BM25;
- dense embeddings;
- HNSW;
- BM25 + dense;
- RRF;
- cross-encoder reranker;
- 8–12 evidence fragments;
- обязательные ссылки.

## 22.2. Что изменить

| Было | Стало |
|---|---|
| фиксированный chunk ~300 + overlap 50 | target range 300–450, sentence/section boundaries, structural overlap |
| только Wikipedia ingestion | универсальный pipeline Docling + Tika + specialized adapters |
| одна схема chunking | versioned document templates |
| top-k fragments | token-budget context packing |
| нет page diversity | page/document quotas и dedup |
| LLM делает всё | deterministic normal RAG, LLM только там, где нужен |
| links как оформление | claim-to-evidence model + deterministic citation validator |
| модельные endpoints в коде | logical aliases через Model Gateway |
| GPU stack в приложении | CUDA только в llama.cpp/optional Docling VLM containers |
| agent как основной путь | отдельный bounded Extended Search |
| generic logs | retrieval-specific event schema |

## 22.3. Что отложить

- GraphRAG до доказанного multi-hop gap;
- learned sparse до сравнения с BM25+dense;
- ColBERT до оценки стоимости индекса;
- semantic/proposition chunks до провала structural templates;
- multi-agent до исчерпания single-agent bounded loop;
- VLM parsing всех PDF — только для сложных документов.

---

## 23. Repository structure

```text
rag-platform/
  compose.yaml
  compose.gpu.yaml
  .env.example

  config/
    models.yaml
    retrieval.yaml
    templates/
      general.yaml
      wikipedia.yaml
      book.yaml
      paper.yaml
      legal.yaml
      presentation.yaml
      spreadsheet_table.yaml
      scanned_ocr.yaml

  services/
    api/
    model-gateway/
    retrieval/
    ingestion-orchestrator/
    worker-docling/
    worker-tika-client/
    worker-wiki/
    evaluator/
    ui/

  packages/
    domain/
    model_client/
    retrieval_contracts/
    document_model/
    template_engine/
    observability/
    security/

  infra/
    opensearch/
      mappings/
      pipelines/
    postgres/
      migrations/
    otel/
    prometheus/
    grafana/

  tests/
    unit/
    integration/
    e2e/
    eval/
    load/

  scripts/
    import_wiki_xml.py
    reindex.py
    smoke_models.py
    benchmark_retrieval.py
    export_trace.py
```

---

## 24. Python interfaces

## 24.1. Retriever contracts

```python
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class SearchQuery:
    text: str
    tenant_id: str
    knowledge_base_ids: tuple[str, ...]
    filters: dict[str, object]
    trace_id: str


@dataclass(frozen=True)
class Candidate:
    chunk_id: str
    document_id: str
    text: str
    title: str
    section_path: tuple[str, ...]
    scores: dict[str, float]
    ranks: dict[str, int]
    metadata: dict[str, object]


class Retriever(Protocol):
    async def search(self, query: SearchQuery, top_k: int) -> Sequence[Candidate]: ...


class FusionStrategy(Protocol):
    def fuse(self, result_sets: Sequence[Sequence[Candidate]]) -> Sequence[Candidate]: ...


class Reranker(Protocol):
    async def rerank(
        self,
        query: SearchQuery,
        candidates: Sequence[Candidate],
        top_k: int,
    ) -> Sequence[Candidate]: ...
```

## 24.2. Parser contracts

```python
class DocumentParser(Protocol):
    async def parse(self, source: "DocumentSource", config: dict) -> "CanonicalDocument": ...


class ChunkingStrategy(Protocol):
    def chunk(
        self,
        document: "CanonicalDocument",
        template: "TemplateVersion",
    ) -> list["Chunk"]: ...
```

## 24.3. Model client

```python
class ModelGatewayClient:
    async def chat(self, alias: str, messages: list[dict], **kwargs) -> dict: ...
    async def embeddings(self, alias: str, inputs: list[str], **kwargs) -> list[list[float]]: ...
    async def rerank(
        self, alias: str, query: str, documents: list[str], **kwargs
    ) -> list[dict]: ...
```

Бизнес-код не импортирует OpenRouter SDK и не вызывает llama.cpp напрямую.

---

## 25. Delivery plan

## Phase 0 — foundation

- monorepo;
- Compose;
- FastAPI;
- PostgreSQL/Redis/MinIO/OpenSearch;
- authentication/tenant model;
- Model Gateway;
- OpenRouter chat/embeddings/rerank smoke tests;
- OpenTelemetry.

**Готово, когда:** один API request проходит end-to-end через OpenRouter и trace виден.

## Phase 1 — Wikipedia fast RAG

- Wikimedia XML multistream ingestion;
- canonical page model;
- Wikipedia template;
- BM25+dense parallel retrieval;
- service-side RRF;
- remote reranker;
- context pack;
- citations;
- retrieval event storage.

**Готово, когда:** тестовые Wikipedia вопросы отвечаются со ссылками, есть retrieval debugger.

## Phase 2 — universal documents

- upload sessions;
- Docling worker;
- Tika server/fallback;
- canonical document model;
- `general`, `book`, `paper`, `presentation`, `spreadsheet`, `scanned_ocr` templates;
- document/chunk preview;
- reprocess/reindex;
- safety limits.

**Готово, когда:** PDF, DOCX, PPTX, XLSX, image и legacy fallback проходят versioned ingestion.

## Phase 3 — local llama.cpp

- three llama-server containers;
- model qualification suite;
- GGUF registry;
- provider switch in gateway;
- load test;
- GPU metrics;
- fallback to OpenRouter controlled by policy.

**Готово, когда:** приложение переключается `MODEL_PROVIDER=openrouter|llamacpp` без изменения API/retrieval code.

## Phase 4 — Extended Search

- intent classifier;
- bounded state machine/LangGraph;
- evidence ledger;
- parallel subqueries;
- loop breaker;
- claim verifier;
- agent trace viewer.

**Готово, когда:** multi-hop evaluation заметно улучшается при контролируемой latency/cost.

## Phase 5 — template studio и evaluation platform

- clone/version templates;
- visual chunk editor;
- A/B pipeline comparison;
- evaluation datasets;
- regression gates;
- index migration UI.

---

## 26. SLO и acceptance criteria

Стартовые ориентиры, уточняемые после benchmark:

### Normal RAG

- API availability: 99.5% для первой production версии;
- retrieval p95: < 800 ms на warm index;
- time to first streamed answer token: < 3–5 s в remote phase;
- citation validator success: > 99.9%;
- trace coverage: 100% query runs;
- no cross-tenant retrieval incidents: 0.

### Ingestion

- job state durable;
- crash-safe resume;
- 100% source/version provenance;
- parser timeout не блокирует очередь;
- failed document не публикует partial index;
- atomic index publication.

### Extended Search

- hard wall-time budget;
- hard step budget;
- no duplicate tool loops;
- full agent trace;
- explicit partial/insufficient answer when coverage missing.

---

## 27. Основные риски и меры

| Риск | Мера |
|---|---|
| OpenRouter model churn | logical aliases, startup capability tests, pinned config, fallback policy |
| GGUF несовместимость | model qualification suite, exact artifact checksums |
| Изменение embedding dimension | dimension fixed per index version, blue/green reindex |
| Плохой PDF parsing | Docling profiles, Tika fallback, degraded status, human preview |
| OCR слишком дорог | OCR auto-detection, отдельная очередь, GPU/VLM only when needed |
| Индекс содержит дубли | content hashes, structural overlap, dedup policies |
| Reranker latency | top-N cap, batching, optional bypass under overload |
| Agent loop | hard budgets, dedup hash, coverage-based stop |
| Потеря explainability | append-only retrieval events and versioned configs |
| Cross-tenant leak | server-side mandatory filters, authorization tests, trace redaction |
| GPU saturation | separate queues, slots, backpressure, per-mode concurrency |
| Parser exploit | isolated containers, no network, limits, scanning |
| Correlated generator/judge | independent human set or separate judge model later |

---

## 28. Итоговая рекомендуемая конфигурация

### Сейчас

```text
FastAPI + React
Custom thin Model Gateway
OpenRouter for chat, embeddings and rerank
Qwen3.6-35B-A3B generator/verifier baseline
Qwen3-Embedding-4B default, 8B challenger, 0.6B latency baseline
Cohere Rerank v3.5 temporary remote reranker
OpenSearch BM25 + HNSW + service-side RRF
PostgreSQL + Redis/Valkey + MinIO
Docling + Apache Tika + Wikipedia XML adapter + future libzim
OpenTelemetry + Phoenix + Prometheus/Grafana
Docker Compose
```

### Целевое локальное состояние

```text
Model Gateway
  -> llama-chat
  -> llama-embed
  -> llama-rerank

CUDA only in llama-* and optional Docling VLM workers
All application/retrieval/metadata services remain CPU-only
```

### Retrieval

```text
query normalization
-> BM25 and dense in parallel
-> RRF
-> cross-encoder reranking
-> dedup/page quota/neighbor expansion
-> token-budget context
-> answer with structured citations
-> deterministic validation
```

### Сложные вопросы

```text
normal deterministic RAG by default
switchable bounded Extended Search for multi-hop/conflict/coverage gaps
```

### Документы

```text
upload
-> security/MIME
-> parser router
-> Docling, Tika, Wikimedia XML or future libzim
-> canonical document
-> versioned template
-> chunks
-> embeddings
-> staging index
-> validation
-> atomic publish
```

---

## 29. Источники и официальная документация

Проверено 2026-07-24.

### llama.cpp

- Server/API: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- Docker images: https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md
- Repository: https://github.com/ggml-org/llama.cpp

### OpenRouter

- Embeddings API: https://openrouter.ai/docs/api/reference/embeddings
- Create embeddings: https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings
- List embedding models: https://openrouter.ai/docs/api/api-reference/embeddings/list-embeddings-models
- Rerank API: https://openrouter.ai/docs/api/api-reference/rerank/create-rerank
- Qwen3.6 35B A3B: https://openrouter.ai/qwen/qwen3.6-35b-a3b
- Qwen3 Embedding 0.6B: https://openrouter.ai/qwen/qwen3-embedding-0.6b
- Qwen3 Embedding 4B: https://openrouter.ai/qwen/qwen3-embedding-4b
- Qwen3 Embedding 8B: https://openrouter.ai/qwen/qwen3-embedding-8b
- Cohere Rerank v3.5: https://openrouter.ai/cohere/rerank-v3.5

### Document ingestion

- Docling supported formats: https://docling-project.github.io/docling/usage/supported_formats/
- Docling pipeline options: https://docling-project.github.io/docling/reference/pipeline_options/
- Docling CLI/OCR/table options: https://docling-project.github.io/docling/reference/cli/
- Apache Tika: https://tika.apache.org/
- Apache Tika Docker: https://github.com/apache/tika-docker

### Search/index

- OpenSearch hybrid query: https://docs.opensearch.org/latest/query-dsl/compound/hybrid
- OpenSearch RRF score ranker: https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/
- OpenSearch k-NN vector: https://docs.opensearch.org/latest/mappings/supported-field-types/knn-vector/
- OpenSearch methods/HNSW: https://docs.opensearch.org/latest/mappings/supported-field-types/knn-methods-engines/

### Wikipedia XML/ZIM

- libzim: https://github.com/openzim/libzim
- openZIM: https://openzim.org/

### Related architecture references

- RAGFlow: https://github.com/infiniflow/ragflow
- DeepAgents GigaChat: https://github.com/ai-forever/deepagents-gigachat
- LangGraph: https://langchain-ai.github.io/langgraph/
- Phoenix: https://phoenix.arize.com/
- OpenTelemetry Python: https://opentelemetry.io/docs/languages/python/

---

## 30. Final decision record

1. **Models:** OpenRouter now, `llama.cpp` target.
2. **Serving:** separate chat/embed/rerank llama-server containers.
3. **Gateway:** internal OpenAI-compatible Model Gateway with aliases and capability tests.
4. **Search:** OpenSearch BM25 + HNSW, RRF, cross-encoder.
5. **Chunking:** structural templates, not one global fixed splitter.
6. **Documents:** Docling primary, Tika broad fallback, libzim specialized.
7. **UI:** RAGFlow-like document/template workflow, but simpler and explainability-first.
8. **Agent:** opt-in/auto-gated Extended Search with hard budgets.
9. **Logging:** retrieval-specific events plus OTel traces from day one.
10. **Production:** versioned artifacts, staging indexes, atomic publication, no CUDA in application containers.
