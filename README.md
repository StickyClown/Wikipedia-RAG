# WikipediaRag: LLM handoff и обзор проекта

WikipediaRag - локальная Docker-first RAG-платформа для вопросов по русской Wikipedia и будущим пользовательским базам знаний. Цель продукта - давать ответы с проверяемыми источниками, показывать полный retrieval trace и позволять инженеру улучшать качество поиска на фиксированных evaluation наборах.

Этот файл предназначен для нового инженера или LLM-агента: он описывает текущее устройство, бизнес-логику, ограничения и точки роста. Практические команды запуска и импорта остаются в [README_START_HERE.md](README_START_HERE.md). Архитектурный источник истины - [docs/architecture.md](docs/architecture.md).

## Текущее состояние

Текущий milestone: ExecPlan 13 завершён. Рабочий MVP использует:

- ZIM/libzim + Kiwix как основной реальный demo-source русской Wikipedia.
- Wikimedia XML `pages-articles` как поддерживаемый regression/local fallback.
- FastAPI backend, worker и Model Gateway на Python 3.12.
- React + Vite + TypeScript UI.
- PostgreSQL, OpenSearch, MinIO, Redis/Valkey, Kiwix и OpenTelemetry Collector в Docker Compose.
- OpenRouter через Model Gateway для `sota_mvp`; mock provider только для `test_mock` и локальных тестов.
- Hybrid retrieval: BM25 + dense vector search, service-side RRF, rerank, dedup/page quota, selective parent expansion, token-budget context packing.
- Ответы с evidence IDs `[S1]`, `[S2]` и deterministic citation validation.
- Retrieval debugger через `/api/v1/search:debug` и persisted events.
- Bounded Extended Search MVP для сложных/сравнительных запросов.
- Trusted evaluation artifacts для retrieval-only и answer-eval прогонов.

## Бизнес-логика

Платформа решает задачу доверяемого поиска и ответа по локальному корпусу. Пользователь не должен верить модели "на слово": каждый factual claim должен опираться на evidence chunk, а система должна показать, почему именно этот chunk попал в контекст.

Основные роли продукта:

- конечный пользователь задаёт вопрос и получает ответ с citations;
- оператор импортирует Wikipedia snapshot и следит за health/readiness;
- инженер retrieval quality смотрит stage events, ablation reports и misses;
- будущий администратор tenant управляет knowledge bases, доступами и retention.

Главная бизнес-инварианта: RAG должен быть воспроизводимым. Документы, chunks, embedding aliases, index versions, retrieval profiles и eval datasets фиксируются так, чтобы результат можно было объяснить и сравнить после изменения модели или индекса.

## Runtime контуры

Система разделена на несколько контуров.

| Контур | Компоненты | Ответственность |
|---|---|---|
| API/UI | FastAPI, React UI | chat, SSE, debugger, job APIs, upload MVP |
| Ingestion | worker, PostgreSQL, MinIO, OpenSearch | импорт ZIM/XML, chunking, embeddings, публикация индекса |
| Retrieval | OpenSearch, Model Gateway, retrieval profile | BM25, dense, RRF, rerank, context assembly |
| Model | Model Gateway, OpenRouter, mock provider, future llama.cpp | chat, embeddings, rerank через логические aliases |
| Storage | PostgreSQL, MinIO, OpenSearch, Redis/Valkey | metadata, artifacts, online search representation, queue/cache |
| Observability | retrieval events, OTel Collector, reports | traceability, timings, eval summaries |

Business code не вызывает OpenRouter напрямую. Все model operations идут через Model Gateway и aliases:

- `embed_default`
- `generator_fast`
- `generator_main`
- `verifier`
- `rerank_default`

Для `sota_mvp` эти aliases являются real-provider путём. Он не должен тихо падать на mock/hash embeddings. Mock aliases (`mock_embed_default`, `mock_generator_fast`, `mock_generator_main`, `mock_verifier`, `mock_rerank_default`) используются явно в тестах и локальном mock profile.

## Ingestion lifecycle

Основной demo path:

1. Оператор кладёт один русский `.zim` в `./zim`.
2. Kiwix обслуживает этот же файл read-only как локальную Wikipedia.
3. Worker через libzim читает `/zim/*.zim`.
4. Service/assets/metadata entries пропускаются.
5. Redirect entries сохраняются как provenance/aliases, но не chunked и не входят в `WIKI_LIMIT`.
6. Canonical article pages нормализуются, режутся на parent/child chunks и получают deterministic IDs.
7. Для child chunks считаются embeddings через Model Gateway.
8. PostgreSQL хранит metadata/provenance, MinIO предназначен для artifacts, OpenSearch хранит online search representation.
9. Index version строится из source snapshot, retrieval profile, embedding alias и dimensions.
10. Read alias публикует актуальную версию индекса.

XML fallback остаётся поддерживаемым путём для regression/development. Он не является основным demo-source после ADR-008.

## Chat lifecycle

Обычный `/api/v1/chat` поток:

1. API создаёт `query_run` с request/trace ID.
2. Выбирается tenant и knowledge base из server-owned контекста, а не из raw client tenant filter.
3. Загружается retrieval profile, обычно `sota_mvp`.
4. Выполняется normal retrieval или, при trigger/explicit mode, Extended Search.
5. Evidence chunks получают IDs `[S1]`, `[S2]`, ...
6. Generator получает только собранный evidence context.
7. Ответ парсится и проходит deterministic citation validation.
8. SSE отдаёт `run.started`, `message.delta`, `usage.updated`, `run.completed` или `run.failed`.
9. `usage.updated` содержит retrieval events, citation validation и safe `timings_ms`.
10. `query_runs.usage` сохраняет итоговые usage/timing/provider metadata без prompts и raw provider bodies.

Если evidence меньше `final_evidence_min`, включается insufficient-evidence behavior: система должна вернуть квалифицированный отказ или ограниченный ответ без неподтверждённых claims.

## Retrieval pipeline

Текущий normal retrieval реализован в `src/wikipediarag/retrieval.py`:

1. **Profile loading.** `config/retrieval.yaml` задаёт switches и лимиты: BM25, dense, RRF, rerank, top_k, parent expansion, page quota, token budget.
2. **Query normalization.** Сейчас это минимальная whitespace normalization. Conditional rewrite/decomposition есть в profile, но не реализованы как полноценный normal-path planner.
3. **Index selection.** Используется active OpenSearch read alias knowledge base; fallback - `wiki-chunks-read`.
4. **BM25.** OpenSearch `multi_match` по `title^3`, `section_path_text^2`, `content`.
5. **Dense retrieval.** Query embedding считается через `embed_default`, затем OpenSearch `knn` ищет по `embedding`.
6. **Tenant/KB safety.** И BM25, и vector query получают server-side filters `tenant_id` и `knowledge_base_id`.
7. **Fusion.** RRF объединяет ranks BM25 и dense без попытки калибровать разные score шкалы.
8. **Rerank.** Cross-encoder reranker получает `query`, title, section path и chunk content, затем переупорядочивает top candidates.
9. **Postprocess.** Dedup по content hash, page quota, selective parent expansion и token-budget packing.
10. **Evidence.** Итоговые chunks возвращаются как `Evidence` с source URL, scores, ranks и metadata.
11. **Trace.** Каждая стадия пишет events: `profile`, `query`, `bm25`, `dense`, `rrf`, `rerank`, `policy`, `context`, `timings`.

`/api/v1/search:debug` запускает retrieval без генерации. Он полезен для качества поиска, но не проверяет качество ответа и citation validation.

## Multi-hop: что реально работает

Важно не переоценивать текущий multi-hop. Retrieval-only evaluation показывает, что `sota_mvp_normal` часто находит multi-hop evidence, но это в основном происходит потому, что вопросы содержат сильные anchors: названия альбомов, фильмов, групп, годы, числа, редкие entity strings.

Пример: вопрос про альбомы `1184` Windir и `12` Face находит `1184 (альбом)` и `12 (альбом)` одним hybrid query, потому что оба title явно есть в вопросе. Это multi-hop для ответа, но не сложный поиск скрытой цепочки.

Ограничения:

- `/api/v1/search:debug` не запускает conditional Extended Search harness.
- `sota_mvp_conditional_harness` в retrieval-only report помечается unsupported.
- Normal retrieval не делает настоящую entity linking/decomposition/follow-links стратегию.
- Текущий Extended Search MVP дробит запрос простыми правилами и повторно вызывает normal `retrieve()`.

Следовательно, текущая система хорошо работает на anchored multi-hop, но слабее на скрытых bridge-вопросах, где один hop нужно сначала вывести из другого.

## Evaluation snapshot

Последний зафиксированный trusted-v3 retrieval result для `sota_mvp_normal`:

- Page recall `@10 = 0.9709`
- Chunk recall `@10 = 0.9200`
- MRR@10 `0.8844`
- nDCG@10 `0.8756`
- Path completion `0.8836`
- p50 latency `1688 ms`
- p95 latency `5927 ms`
- Retrieval miss count `21`
- Error rate `0`

Это сильный сигнал для current anchored Wikipedia slice, но не release gate для production. Все trusted records пока `train/unreviewed`; human-reviewed dev/test splits ещё не введены.

## Known risks and growth backlog

Приоритетные точки роста:

1. **Multi-hop planning.** Добавить нормальную decomposition, entity linking, search-within-document/follow-links и coverage scoring. Это нужно, чтобы решать bridge-вопросы без явных title anchors.
2. **Extended Search hardening.** Заменить простой split по `?`/`и` на typed planner, parallel subqueries, tool result cache, better stop criteria и UI trace state transitions.
3. **Evaluation quality.** Разделить trusted dataset на reviewed train/dev/test, добавить locked slices для multi-hop, unanswerable, redirects, hard negatives и citation failures.
4. **Citation faithfulness.** Усилить claim-to-evidence validation: проверять не только наличие `[Sx]`, но и поддержку каждого claim соответствующим source span.
5. **Document ingestion.** Реальный universal document path для PDF/Office/images пока не production-ready; нужны parser isolation, malware scanning, object-storage artifacts и reprocess contracts.
6. **Auth/tenancy.** Сейчас local MVP использует seeded/default tenant. Production требует OIDC/SSO, role matrix, tenant onboarding, retention/deletion и cross-tenant regression tests.
7. **Local llama.cpp.** Compose profile подготовлен, но production switch требует model artifacts, licenses, checksums, GPU/VRAM sizing и quality gates.
8. **Operational SLO.** p95 retrieval в real eval выше целевого warm SLO 800 ms; нужна профилировка OpenSearch/vector/rerank, caching и load tests.
9. **Index lifecycle.** Dynamic index naming есть, но production needs atomic alias publication, rollback drills, snapshots and migration discipline for committed indices.
10. **Cost and safety.** OpenRouter-backed runs incur cost and provider exposure. Нужно закрыть owner decisions: можно ли отправлять пользовательские documents во внешний provider, какие residency/retention правила действуют.

## Где читать дальше

- [README_START_HERE.md](README_START_HERE.md) - запуск, импорт, smoke/eval commands.
- [docs/architecture.md](docs/architecture.md) - архитектурный источник истины.
- [docs/STATUS.md](docs/STATUS.md) - фактическое состояние, команды и результаты.
- [docs/contracts/API_CONTRACT.md](docs/contracts/API_CONTRACT.md) - public API и SSE contract.
- [docs/contracts/DOMAIN_INVARIANTS.md](docs/contracts/DOMAIN_INVARIANTS.md) - safety/tenancy/indexing invariants.
- [docs/contracts/EVALUATION_CONTRACT.md](docs/contracts/EVALUATION_CONTRACT.md) - evaluation artifacts and CLI contract.
- [docs/DECISIONS_REQUIRED.md](docs/DECISIONS_REQUIRED.md) - owner decisions before production/local-GPU phases.
