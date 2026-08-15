# План улучшения Search, RAG и Deep Research на 2026 год

**Проект:** WikipediaRag

**Дата проверки:** 2026-08-14

**Назначение:** технический план развития качества, скорости и исследовательских возможностей поиска без календарной разбивки на спринты.

## 1. Как читать этот документ

Документ не принимает исходные планы и публикации поставщиков на веру. Для каждого существенного вывода используется одна из меток:

- **`CODE EVIDENCE`** — поведение подтверждено исполняемым кодом, конфигурацией или тестом WikipediaRag.
- **`EXTERNAL EVIDENCE`** — возможность или архитектурный приём описаны в первичном внешнем источнике.
- **`VENDOR CLAIM`** — число или преимущество заявлено разработчиком системы и ещё не воспроизведено на WikipediaRag.
- **`INFERENCE`** — рекомендация для WikipediaRag, выведенная из кода проекта и внешних материалов; требует локальной проверки.

Порядок `P0 → P1 → P2` означает техническую зависимость, а не календарный roadmap. Размеры пулов, пороги и бюджеты ниже являются стартовыми экспериментальными диапазонами, но не новой догмой: итоговые значения должны определяться eval-набором.

Текущая активная задача проекта в [`docs/STATUS.md`](../docs/STATUS.md) — эксплуатационная проверка Model Gateway и связанного canary/RRNCB-пути. Она не включена в этот продуктовый план поиска по явному решению пользователя. При этом сохраняется архитектурный инвариант: модели локальные/private, а бизнес-код обращается к embedding, rerank и generation только через Model Gateway aliases.

## 2. Целевой результат

Пользователь должен быстро получить один из четырёх проверяемых результатов:

1. полный ответ с точными цитатами;
2. частичный ответ с явным перечислением пробелов;
3. описание найденного конфликта источников;
4. обоснованное `not_found_in_scope`, которое означает не «модель не знает», а «система выполнила зафиксированный поиск в указанной области и не нашла достаточных доказательств».

Для обычного вопроса система выбирает минимально достаточный retrieval-путь. Для большого исследования она умеет декомпозировать задачу, выполнять независимые поисковые ветви ограниченно параллельно, проверять coverage, повторно искать пропуски и строить воспроизводимый отчёт. Внутренние документы остаются основным корпусом; внешний web search подключается только как дополнительный evidence source.

## 3. Проверенный AS-IS

### 3.1 Что уже реализовано и не должно проектироваться заново

1. **`CODE EVIDENCE` — hybrid retrieval уже существует.** В [`src/wikipediarag/retrieval.py`](../src/wikipediarag/retrieval.py) выполняются query normalization/variants, общий embedding-вызов, BM25 и dense retrieval, RRF, rerank, post-processing, parent expansion, context assembly и answerability. Профиль `sota_mvp` в [`config/retrieval.yaml`](../config/retrieval.yaml) включает BM25, dense, RRF и rerank.

2. **`CODE EVIDENCE` — PostgreSQL подтверждает кандидатов до fusion/rerank.** `_confirm_candidates` в [`src/wikipediarag/retrieval.py`](../src/wikipediarag/retrieval.py) повторно проверяет поисковые документы против канонического состояния PostgreSQL до объединения результатов. Поэтому предложение «добавить ACL trimming только после генерации» было бы регрессией, а не улучшением.

3. **`CODE EVIDENCE` — базовая наблюдаемость retrieval уже есть.** Pipeline записывает timings и события `dense.embedding`, `bm25`, `dense`, `rrf`, `rerank`, `postprocess`, `context` и `answerability`; таблицы `query_runs` и `retrieval_events` определены в [`src/wikipediarag/db.py`](../src/wikipediarag/db.py). Не хватает не самих событий, а сравнительных quality/latency dashboards и метрик потерь кандидатов между этапами.

4. **`CODE EVIDENCE` — answerability уже различает несколько исходов.** [`src/wikipediarag/answerability.py`](../src/wikipediarag/answerability.py) и [`src/wikipediarag/answering.py`](../src/wikipediarag/answering.py) обрабатывают answerable, partial, conflicting и unanswerable. Улучшение должно расширить доказательность и внешний контракт, а не создать второй параллельный классификатор.

5. **`CODE EVIDENCE` — Deep Research уже durable и bounded.** [`src/wikipediarag/deep_research.py`](../src/wikipediarag/deep_research.py) реализует сохранённые runs, questions, episodes, tool calls, evidence, claims, coverage, decisions/reflections, pause/resume/cancel и bounded tool loop. Архитектурная документация прямо фиксирует single-agent planner/tool loop, а не swarm. Следовательно, generic multi-agent rewrite не имеет доказанного основания.

6. **`CODE EVIDENCE` — eval-инфраструктура уже существует.** В [`src/wikipediarag/eval`](../src/wikipediarag/eval) есть manifests, provenance/contract IDs, retrieval/generation metrics и review workflow. Текущий зафиксированный Wikipedia baseline в [`docs/architecture/search-and-deep-research.md`](architecture/search-and-deep-research.md) сообщает page Recall@10 `0.896`, chunk Recall@20 `0.904`, MRR@10 `0.817` и nDCG@10 `0.787`. Эти числа нельзя считать multilingual/cross-source доказательством: они относятся к конкретному locked corpus/contract.

### 3.2 Реальные ограничения

1. **`CODE EVIDENCE` — upload chunking остаётся примитивным.** `chunks_for_normalized_document` в [`src/wikipediarag/document_ingestion.py`](../src/wikipediarag/document_ingestion.py) режет слова независимыми окнами по 220 слов через `range(0, len(words), 220)`. `parent_chunk_id` записывается, но `parent_text` в metadata upload-чанка не сохраняется.

2. **`CODE EVIDENCE` — это ограничение не относится ко всему корпусу.** [`src/wikipediarag/zim_dump.py`](../src/wikipediarag/zim_dump.py) уже формирует section-aware parent-child chunks и сохраняет `parent_text`. Поэтому утверждение исходного плана «весь проект использует слепые 220-word chunks» неверно.

3. **`CODE EVIDENCE` — parent expansion работает неравномерно.** Retrieval читает `metadata.parent_text`, но upload-ingestion его не создаёт. В результате один и тот же retrieval-контракт даёт различное качество контекста для ZIM и загруженных документов.

4. **`CODE EVIDENCE` — определение языка не является настоящим multilingual detector.** `detect_language` в [`src/wikipediarag/document_ingestion.py`](../src/wikipediarag/document_ingestion.py) опирается главным образом на соотношение кириллицы и латиницы и возвращает ограниченную картину `ru/en/und`. Это недостаточно для украинского, белорусского, казахского, сербского, смешанных документов, CJK и других языков.

5. **`CODE EVIDENCE` — fusion и rerank содержат непроверенные фиксированные значения.** `rrf_fuse(..., k=60)` зафиксирован в [`src/wikipediarag/retrieval.py`](../src/wikipediarag/retrieval.py). В `sota_mvp` `fusion_top_k=60`, `rerank_input_k=24`, `rerank_top_k=50`; фактически reranker не может вернуть более 24 входных кандидатов. Это не обязательно ошибка, но это ограничение recall, которое должно быть экспериментально обосновано.

6. **`CODE EVIDENCE` — индекс минимально использует структуру и язык.** [`src/wikipediarag/search_index.py`](../src/wikipediarag/search_index.py) индексирует title, alias text, section path, content и embedding, но не задаёт полноценные language-specific lexical lanes, source offsets, тип блока или отдельный retrieval/source text.

7. **`CODE EVIDENCE` — зрелость коннекторов неодинакова.** [`src/wikipediarag/source_connectors.py`](../src/wikipediarag/source_connectors.py) содержит Confluence DC, Jira DC, GitLab self-managed, local folder, internal crawler и Kiwix/ZIM. Local folder умеет tombstones, но корпоративные коннекторы в основном возвращают `tombstones: 0`; Jira записывает `overlap_minutes` в cursor, но текущий путь не доказывает фактическое вычитание overlap; вложения Jira/Confluence не проходят полный extraction/indexing lifecycle; crawler не имеет законченного conditional/incremental fetch-контракта.

8. **`CODE EVIDENCE` — OpenSearch проекта имеет версию 2.17.1.** Это закреплено в [`compose.yaml`](../compose.yaml). Возможности Search Relevance Workbench, relevance agent и agentic search из документации OpenSearch 3.x нельзя считать доступными без отдельной миграции.

9. **`CODE EVIDENCE` — IndexContract существует, но нуждается в более явной версии retrieval unit.** [`src/wikipediarag/retrieval_contract.py`](../src/wikipediarag/retrieval_contract.py) уже включает index version, source type, snapshot, embedding и chunking. Для безопасного сравнения Index v1/v2 необходимо сделать chunker/schema/analyzer versions явной частью идентичности и rollout-политики, а не вводить независимый конкурирующий контракт.

## 4. Что подтверждают практики 2026 года

### 4.1 Perplexity

- **`EXTERNAL EVIDENCE`** В [Rethinking Search as Code Generation](https://research.perplexity.ai/articles/rethinking-search-as-code-generation) Perplexity описывает search primitives, которые модель компонует в программу, а deterministic runtime выполняет batching, filtering, joins, aggregation и fan-out. Полезный перенос для WikipediaRag — компактный типизированный `SearchPlan` и batch executor.
- **`INFERENCE`** Генерация произвольного Python не нужна в основном search path. Она усложнила бы воспроизводимость и нарушила бы уже существующие typed filters, Model Gateway boundary и bounded research runtime. Модель должна выбирать разрешённые операции, а приложение — компилировать и исполнять их.
- **`EXTERNAL EVIDENCE`** [Query-Aware Context Compression](https://research.perplexity.ai/articles/query-aware-context-compression-for-better-snippets) предлагает extractive selection: выбирать релевантные предложения из retrieved document, сохраняя исходный текст, а не пересказывать его до ответа.
- **`VENDOR CLAIM`** Заявленные Perplexity сокращение контекста и прирост качества получены на их стеке. Они являются основанием для локального эксперимента, но не обещанием аналогичного эффекта.
- **`EXTERNAL EVIDENCE`** [WANDR](https://research.perplexity.ai/articles/wandr-benchmark-evaluating-research-agents-that-must-search-wide-and-deep) оценивает не только финальный ответ, но и иерархические этапы discovery, enrichment, identity, qualification и evidence.
- **`INFERENCE`** Для WikipediaRag это важнее копирования конкретного agent loop: текущему Deep Research нужен измеряемый coverage по ветвям и claim-level gaps.

### 4.2 Microsoft, Google и OpenAI

- **`EXTERNAL EVIDENCE`** [Azure AI Search RAG overview](https://learn.microsoft.com/en-us/azure/search/retrieval-augmented-generation-overview) разделяет быстрый classic RAG и agentic retrieval, который декомпозирует сложный вопрос на параллельные focused subqueries и возвращает структурированный результат с citations/query metadata.
- **`INFERENCE`** WikipediaRag не должен отправлять каждый запрос в Deep Research. Нужен router `fast/balanced/research`, оставляющий простые exact/entity запросы на коротком детерминированном пути.
- **`EXTERNAL EVIDENCE`** [Gemini Deep Research](https://ai.google.dev/gemini-api/docs/deep-research) использует предварительный план, его уточнение перед запуском, background execution и режимы с разной полнотой.
- **`VENDOR CLAIM`** Публичные оценки количества запросов и токенов Gemini описывают их сервис, а не рекомендуемые бюджеты WikipediaRag.
- **`EXTERNAL EVIDENCE`** Официальная [документация OpenAI Deep Research](https://developers.openai.com/api/docs/guides/deep-research) описывает Responses API с обязательным источником данных — web search, remote MCP или file search — и допускает background execution и code interpreter. Для MCP выделен специализированный поисковый контракт.
- **`INFERENCE`** Для WikipediaRag полезны узкие `search/fetch`, предварительное clarification/rewrite и durable source ledger. Подключать OpenAI как обязательного provider нельзя: проект сохраняет локальную/private модель и Model Gateway boundary.

### 4.3 Anthropic, Elastic и OpenSearch

- **`EXTERNAL EVIDENCE`** [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) разделяет session (append-only durable log), harness и sandbox/tools. Контекст задачи хранится вне context window и может быть повторно выбран из журнала после сбоя.
- **`INFERENCE`** Текущую durable модель Deep Research стоит расширять event cursor и восстановлением ветвей, а не связывать прогресс исследования с prompt history.
- **`EXTERNAL EVIDENCE`** [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) рекомендует разделять task, trial, graders, transcript и outcome; для research-задач оценивать groundedness, coverage и качество источников.
- **`EXTERNAL EVIDENCE`** [Elastic deterministic control plane](https://www.elastic.co/search-labs/blog/agentic-ai-search-deterministic-guardrail-query-execution) отделяет вероятностное понимание намерения от детерминированного построения запросов и не передаёт LLM index mapping/Query DSL.
- **`INFERENCE`** Это сильное подтверждение typed `SearchPlan`: LLM формирует intent и логический план, а search service применяет разрешённые filters, budgets и query templates.
- **`EXTERNAL EVIDENCE`** [OpenSearch hybrid optimization](https://docs.opensearch.org/latest/search-plugins/search-relevance/optimize-hybrid-search/) показывает систематическое сравнение normalization, weights и RRF parameters по query sets/judgments.
- **`INFERENCE`** Поскольку проект использует OpenSearch 2.17.1, сначала следует расширить собственный eval runner. Обновление OpenSearch — отдельное решение только после доказанного преимущества.

### 4.4 Научные результаты как дополнительная проверка

- **`EXTERNAL EVIDENCE`** Работа [Beyond Chunk-Then-Embed (2026)](https://arxiv.org/abs/2602.16974) показывает, что structure-aware chunking часто полезно, но contextualization зависит от задачи и может ухудшить отдельные режимы retrieval.
- **`INFERENCE`** Поэтому Index v2 должен начать с детерминированной структуры и сравнивать LLM-contextualization отдельной ablation, а не делать её обязательной частью ingestion.
- **`EXTERNAL EVIDENCE`** [Claim-selective certification for RAG (2026)](https://arxiv.org/abs/2605.21949) исследует статусы full/partial/conflict/abstain на уровне claims.
- **`INFERENCE`** Эти статусы согласуются с существующим answerability проекта, но конкретные пороги и результаты медицинского домена нельзя переносить на WikipediaRag.

## 5. Целевая архитектура

```text
Source adapters
  -> canonical document + source spans
  -> structure parser
  -> RetrievalUnitV2
  -> versioned IndexContract / dual index

User query
  -> language + intent + mode router
  -> typed SearchPlan
  -> deterministic search executor
       exact/identifier lane
       BM25 language lane
       multilingual dense lane
       optional search_many/fetch_many
  -> fusion experiment winner
  -> rerank cascade
  -> extractive compressor
  -> evidence ledger + SearchReceipt
  -> AnswerabilityEnvelope
  -> answer / partial / conflict / not_found_in_scope

Deep Research
  -> plan + coverage graph
  -> bounded independent waves
  -> durable branch events
  -> gap repair
  -> claim/evidence verification
  -> reproducible report
```

Критический принцип: LLM является planning/control component, но не автором OpenSearch DSL, SQL или неограниченного исполняемого кода. Все model calls идут через Model Gateway. Все источники — внутренний index, PostgreSQL metadata, connector fetch или optional web — нормализуются в единый evidence/citation contract.

## 6. P0 — измеримость и качество поиска

### P0.1. Расширить evaluation contract — выполнено 2026-08-15

- **Проблема:** текущие сильные Wikipedia metrics не доказывают multilingual, cross-source, structured document, conflicting и absence-сценарии.
- **Изменение:** добавить один versioned benchmark manifest с семействами `exact_identifier`, `entity_alias`, `multilingual`, `cross_source`, `table_lookup`, `multi_hop`, `partial`, `conflicting`, `not_found_in_scope`, `freshness` и `citation_span`.
- **Ожидаемая польза:** любое изменение chunking, fusion, reranker или research policy сравнивается на одинаковых задачах, а не по впечатлению от нескольких запросов.
- **Компоненты:** существующие `src/wikipediarag/eval`, fixture manifests, retrieval run provenance, answer/citation graders и Deep Research evaluator.
- **Минимальная реализация:** минимум по 20 проверенных запросов на основное семейство и не менее трёх языковых групп: русский, английский, третий язык/смешанный текст. Для absence/conflict обязательна ручная верификация corpus scope.
- **Проверка:** Recall@k, MRR, nDCG, citation precision/coverage, answer groundedness, answerability macro-F1, false-answer rate для absent cases, latency p50/p95 и Model Gateway usage. Результаты запрещено смешивать между разными index/run contract IDs.
- **Зависимости:** нет; это первая работа, поскольку все следующие решения нуждаются в baseline.
- **Риск переноса:** чужие benchmark-задачи могут не представлять документы пользователя. WANDR/MIRACL используются как модель структуры eval, а не как единственный acceptance corpus.

P0.1 закрыт как workflow и provenance contract. RRNCB reference измеряет
document-level retrieval; его source rows не содержат reviewed section/chunk
anchors, поэтому evidence-level метрики и production regression policy
перенесены в отдельную будущую задачу.

### P0.2. Ввести RetrievalUnitV2 и Index v2

- **Проблема:** upload ingestion теряет документную структуру и контекст; citation coordinates недостаточно богаты; ZIM и upload имеют различное parent behavior.
- **Изменение:** парсить document hierarchy и создавать две связанные сущности: короткий retrieval unit для поиска и точный source span для показа/цитирования. Сохранять document title, heading path, block type, page/sheet/slide coordinates, offsets, parent text и соседние block IDs.
- **Ожидаемая польза:** выше recall для таблиц/разделов, меньше разорванных мыслей, точнее цитаты, меньше лишнего контекста.
- **Компоненты:** document normalization, ZIM chunker, upload ingestion, chunk schemas, OpenSearch mapping, PostgreSQL chunk metadata, viewer и citation resolver.
- **Минимальная реализация:** HTML/Markdown/headings/paragraphs/lists/tables; deterministic packing по token budget с небольшим overlap только внутри логического раздела; унифицированное заполнение `parent_text`; v1 индекс продолжает обслуживать чтение во время построения v2.
- **Проверка:** ablation `fixed-220` против `structure-aware`; chunk Recall@20, citation exactness, context tokens, indexing time и размер индекса. Отдельно проверить короткие секции, большие таблицы, list items, сканы без структуры и документы с неверными heading levels.
- **Зависимости:** P0.1.
- **Риск переноса:** LLM-generated contextual prefixes могут повысить стоимость и иногда ухудшить in-document retrieval. На первом этапе контекст детерминированный; LLM-contextualization — отдельная выключенная ablation.

### P0.3. Реализовать настоящую multilingual-поддержку

- **Проблема:** алфавитная эвристика `ru/en` ошибается на близких кириллических языках, смешанном тексте и нелатинских письменностях; единый lexical analyzer не учитывает морфологию языка.
- **Изменение:** заменить detector на локальный language identification component с confidence и segment-level fallback; создать language-aware lexical fields/query lanes, сохранив общий multilingual embedding lane и cross-lingual query variants.
- **Ожидаемая польза:** устойчивый BM25 и query expansion для разных языков при сохранении межъязыкового поиска по смыслу.
- **Компоненты:** normalization metadata, OpenSearch mapping/analyzers, query planner, embedding instruction и eval corpus.
- **Минимальная реализация:** поддержать `ru`, `en`, `uk`, `de`, `fr`, `es`, `zh` и `und/mixed`; для неподдержанного анализатора использовать language-neutral field, а не ошибочно русский или английский. Детектор и модели остаются локальными и вызываются по утверждённой границе, если требуют model inference.
- **Проверка:** language identification F1, Recall@k по языкам, cross-language query/document pairs, proper nouns, code-switching, цифры/артикулы и документы с малым количеством текста.
- **Зависимости:** P0.1; mapping входит в Index v2 из P0.2.
- **Риск переноса:** список анализаторов Azure не является доказательством их качества или доступности в OpenSearch 2.17. Нужны совместимые локальные analyzers и собственный corpus.

### P0.4. Сделать retrieval измеряемым каскадом

- **Проблема:** одинаковый pipeline применяется к разным типам запросов, а `RRF k=60` и окно reranker=24 не обоснованы текущим широким eval-набором.
- **Изменение:** ввести стадии `exact/identifier → lexical+dense recall → fusion → lightweight filtering → expensive rerank → context selection`; логировать вход/выход и причины отсева каждого кандидата.
- **Ожидаемая польза:** exact lookup становится быстрее, сложный semantic поиск получает более широкий recall, дорогой reranker не тратится на заведомо слабые документы.
- **Компоненты:** query intent, retrieval profiles, `rrf_fuse`, rerank adapter, query events и evaluator.
- **Минимальная реализация:** протестировать BM25/dense pools `50/100/200`, fusion top-k `40/60/100`, rerank input `24/50/100`, RRF `k=10/20/60` и weighted normalized fusion. Exact lane может завершать поиск только при однозначном canonical match; иначе возвращает кандидатов в общий cascade.
- **Проверка:** paired runs на одном IndexContract; quality/latency Pareto frontier, candidate survival по relevant judgments, rerank uplift, empty/error fallback каждого lane.
- **Зависимости:** P0.1; P0.2/P0.3 желательно завершить до финальной фиксации параметров.
- **Риск переноса:** OpenSearch Workbench experiments нельзя запустить непосредственно на 2.17. Сначала те же варианты реализуются в существующем eval runner без обновления search engine.

### P0.5. Ввести доказуемый SearchReceipt и расширить AnswerabilityEnvelope

- **Проблема:** существующий answerability оценивает evidence, но пользователь не видит полноту выполненного поиска; `unanswerable` не равен доказанному отсутствию в выбранном corpus.
- **Изменение:** сохранить существующий классификатор как owner и расширить его внешним envelope: `answered`, `partial`, `conflicting`, `not_found_in_scope`. Каждый результат сопровождается `SearchReceipt` с scope, index snapshot, query variants, source/lane coverage, applied filters, candidate counts и stopping reason.
- **Ожидаемая польза:** система перестаёт маскировать пробелы уверенным ответом и объясняет границы отрицательного результата.
- **Компоненты:** answerability, retrieval events, API schemas, answering prompt, debug UI и report citations.
- **Минимальная реализация:** `not_found_in_scope` разрешён только после успешного выполнения обязательных lanes, отсутствия достаточных evidence и отсутствия connector/index freshness error. При сбое источника статус должен быть `partial`/`search_incomplete`, а не `not_found`.
- **Проверка:** curated answerable/partial/conflict/absent fixtures; macro-F1, false `not_found`, false confident answers, корректность receipt при timeout, empty index, stale connector и filters that exclude relevant evidence.
- **Зависимости:** P0.1 и stage counters из P0.4.
- **Риск переноса:** claim-selective certification из узкого научного домена не задаёт универсальные thresholds. Пороги калибруются на корпусе проекта.

### P0.6. Довести существующие коннекторы до полного ingestion lifecycle

- **Проблема:** источник может сообщить изменённые документы, но не гарантирует одинаковую полноту пагинации, attachments, deletion/tombstone и overlap semantics.
- **Изменение:** унифицировать connector conformance contract: stable source ID, canonical URL, paginated snapshot/delta, cursor with real overlap window, attachment enumeration/fetch, content hash, tombstones и freshness watermark.
- **Ожидаемая польза:** поиск становится полным и актуальным; удалённые/переименованные материалы не остаются в derived index; повторный sync идемпотентен.
- **Компоненты:** Confluence, Jira, GitLab, crawler, source sync jobs, document versions, object storage и OpenSearch publication.
- **Минимальная реализация:** сначала Confluence/Jira: bounded pagination, attachment documents, cursor regression tests, timestamp overlap applied before request, full-sync tombstones и second-sync zero-delta. Затем те же conformance tests применить к GitLab/crawler.
- **Проверка:** `initial sync → update → attachment change → delete → repeated sync → search`; убедиться, что publication происходит worker-ом, а не синхронно внутри HTTP request.
- **Зависимости:** P0.2 для новых attachment/source spans; conformance tests можно начать раньше.
- **Риск переноса:** API семантика разных версий Data Center различается. Коннектор должен фиксировать capabilities и degraded reason, а не притворяться полным.

## 7. P1 — скорость, программируемый поиск и Deep Research

### P1.1. Добавить extractive query-aware context compression

- **Проблема:** после rerank контекст в основном ограничивается quota/dedup/parent expansion; длинный parent может внести нерелевантные предложения и вытеснить полезные evidence.
- **Изменение:** между context selection и generation добавить compressor, выбирающий точные предложения/blocks относительно query и сохраняющий исходные offsets.
- **Ожидаемая польза:** меньше токенов и latency generation, больше разных evidence в том же контекстном бюджете, отсутствие дополнительной paraphrase-ошибки.
- **Компоненты:** retrieval postprocess, citation/source span resolver, Model Gateway scoring alias при model-based варианте и answering context builder.
- **Минимальная реализация:** deterministic sentence segmentation + lightweight relevance scoring; fallback на исходный span при низкой уверенности. Model-based compressor сравнивать отдельно и вызывать только через Gateway.
- **Проверка:** retained answer-bearing spans, citation exactness, context token reduction, answer quality и end-to-end latency. Проверить таблицы, code blocks, короткие chunks и multilingual punctuation.
- **Зависимости:** source offsets из P0.2 и baseline P0.1.
- **Риск переноса:** vendor claim Perplexity не доказывает локальный выигрыш; compressor отклоняется, если снижает citation coverage или groundedness.

### P1.2. Ввести режимы `fast`, `balanced`, `research`

- **Проблема:** простой lookup и широкое аналитическое исследование имеют разные оптимальные latency/coverage budgets.
- **Изменение:** typed mode policy задаёт allowed stages, candidate budgets, query decomposition, rerank depth, context budget и Model Gateway alias. Router выбирает режим по явному запросу пользователя и детерминированным признакам; модель может предложить повышение режима, но не скрытно расширять бюджет.
- **Ожидаемая польза:** быстрый ответ для большинства вопросов и предсказуемая полнота для сложных задач.
- **Компоненты:** API schema, retrieval profile resolver, search/answer service, Deep Research creator и query diagnostics.
- **Минимальная реализация:** `fast` — exact + shallow hybrid; `balanced` — полный текущий cascade; `research` — decomposed batch search и durable run. При недостаточном evidence fast/balanced возвращает предложение расширить поиск или автоматически применяет заранее разрешённую policy.
- **Проверка:** mode-routing confusion matrix, latency/quality curves, budget enforcement, одинаковая tenant/KB scope во всех режимах и понятное отображение выбранного режима.
- **Зависимости:** P0.4 и P0.5.
- **Риск переноса:** Gemini/другие vendor modes не задают подходящие budgets; значения калибруются локально.

### P1.3. Добавить typed batch search primitives

- **Проблема:** последовательные model/tool turns тратят latency и context на механическую координацию множества однотипных запросов.
- **Изменение:** определить разрешённые операции `search_many`, `fetch_many`, `filter`, `deduplicate`, `join`, `aggregate`, `coverage_check`; модель создаёт JSON `SearchPlan`, а deterministic executor валидирует и выполняет DAG с bounded concurrency.
- **Ожидаемая польза:** широкий fan-out за меньшее число inference rounds, воспроизводимость, повторное использование результатов и ясная трассировка.
- **Компоненты:** research tool registry, search service, planner schema, executor, durable event log и cache.
- **Минимальная реализация:** начать с `search_many`, `fetch_many`, `deduplicate`, `coverage_check`; ограничить число узлов, depth, concurrency и total result bytes существующими research budgets. Каждый узел получает stable ID и записывает inputs, output references, latency и status.
- **Проверка:** sequential против batch на одинаковых задачах; wall time, Model Gateway calls, token use, coverage и identical-scope invariants. Проверить partial node failure, retry, resume и duplicate query collapse.
- **Зависимости:** P0.4/P0.5 и mode policy P1.2.
- **Риск переноса:** Perplexity Search as Code использует другой runtime. Проект принимает composability, но не arbitrary Python, прямой DSL или неограниченный sandbox.

### P1.4. Добавить типизированные аналитические операции

- **Проблема:** синтез текста моделью ненадёжен для массовых сравнений, подсчётов, группировок и временных рядов.
- **Изменение:** после retrieval/fetch нормализовать выбранные значения в provenance-aware records и выполнять `group`, `compare`, `count`, `sort`, `time_series`, `pivot` детерминированно. Каждый результат ссылается на source spans строк.
- **Ожидаемая польза:** проверяемые таблицы и вычисления, меньше арифметических ошибок, возможность повторно использовать результат в отчёте.
- **Компоненты:** research tools, structured extraction schema, calculation executor, evidence ledger и report renderer.
- **Минимальная реализация:** count/group/sort/compare для явно типизированных полей; неизвестные/непарсируемые значения сохраняются как missing с evidence reference, а не угадываются.
- **Проверка:** fixtures с дублями, разными единицами/датами, missing values и conflicting facts; сравнить вычисление с вручную проверенным expected table.
- **Зависимости:** P1.3 и точные source spans P0.2.
- **Риск переноса:** LLM extraction остаётся вероятностным. Детерминировано только вычисление над извлечёнными records; точность extraction оценивается отдельно.

### P1.5. Развить Deep Research в coverage-driven DAG/waves

- **Проблема:** существующий bounded loop устойчив, но последовательная обработка независимых ветвей ограничивает ширину и скорость; общий финальный ответ скрывает, где именно потеряна coverage.
- **Изменение:** сохранить одного lead planner и durable lifecycle, но представить вопросы как branch DAG. Независимые ready-ветви выполняются ограниченными waves; после каждой wave `coverage_check` создаёт только необходимые gap-repair branches.
- **Ожидаемая польза:** ниже wall time на разложимых задачах, точное восстановление после сбоя и объяснимая полнота отчёта.
- **Компоненты:** research questions/episodes, tool dispatcher, planner schema, event log, coverage/claims и report synthesis.
- **Минимальная реализация:** branch fields `goal`, `dependencies`, `coverage_target`, `evidence_gaps`, `status`, `event_cursor`; concurrency по умолчанию 2, максимум определяется config; synthesis начинается только после terminal/explicitly-skipped branches.
- **Проверка:** single-loop против waves на WANDR-подобных локальных задачах; coverage, hard/soft completeness, wall time, token/tool calls и resume after worker loss. Неразложимые задачи должны оставаться последовательными.
- **Зависимости:** P1.3; P0.1 должен содержать research coverage graders.
- **Риск переноса:** multi-agent vendor results часто требуют значительно больше токенов. Generic swarm не вводится; параллелизуются только независимые tool branches внутри существующего server-owned scope.

### P1.6. Добавить необязательный внешний web source

- **Проблема:** внутренний corpus не отвечает на вопросы о новых событиях и может не содержать необходимых внешних доказательств.
- **Изменение:** добавить read-only `search` + `fetch` adapter через MCP или встроенный provider, нормализующий web results в тот же Evidence/SourceSpan contract. Web не смешивается с внутренним corpus без явной source label.
- **Ожидаемая польза:** актуальное исследование с единым citation ledger и возможностью отдельно оценить внутренние и внешние доказательства.
- **Компоненты:** research tool registry, source policy, URL canonicalization, evidence persistence и report renderer.
- **Минимальная реализация:** web доступен только в `research` или при явном разрешении; `search` возвращает metadata/snippets, `fetch` — точный source content и retrieval time; failure не превращается в `not_found_in_scope`.
- **Проверка:** internal-only, web-only и mixed fixtures; citation URL/span validity, duplicate canonical URLs, stale pages, fetch failure и provenance separation.
- **Зависимости:** SearchReceipt P0.5 и batch tools P1.3.
- **Риск переноса:** внешний web ухудшает приватность и воспроизводимость corpus. Он остаётся optional source, а не обязательной заменой локального RAG.

## 8. P2 — оптимизация по фактическим данным

### P2.1. Экспортировать quality и pipeline metrics

- **Проблема:** retrieval events существуют, но нет единого представления candidate attrition, cache efficiency, connector freshness и research coverage в динамике.
- **Изменение:** агрегировать существующие безопасные события в метрики без prompt/document payloads: stage latency, input/output candidates, relevant-candidate survival на eval, cache hit, connector lag, branch coverage и stopping reasons.
- **Ожидаемая польза:** performance regressions и quality bottlenecks видны по конкретной стадии, а не только по итоговому p95.
- **Компоненты:** query/research events, OTel export, eval reports и dashboards.
- **Минимальная реализация:** offline report из PostgreSQL/eval artifacts и совместимые metric names; production backend/dashboard выбирается отдельно. Debug-only collector не считается законченным monitoring solution.
- **Проверка:** synthetic run с известными counts/latencies; отсутствие raw text/secrets в labels; корректное связывание с index/run contract IDs.
- **Зависимости:** stage counters P0.4, receipts P0.5 и branch events P1.5.
- **Риск переноса:** observability stack поставщика не является целью. Важны стабильные события и вычислимые показатели, а не конкретный dashboard product.

### P2.2. Рассматривать обучение policy, новые retrievers и обновление OpenSearch только после eval

- **Проблема:** новые модели и search-engine features легко создают дорогую миграцию без доказанного улучшения на реальном corpus.
- **Изменение:** завести отдельный research decision record для каждого кандидата: learned search policy, новый embedding/reranker, late interaction, learned sparse retrieval или OpenSearch 3.x.
- **Ожидаемая польза:** архитектура развивается по измеримому Pareto improvement, а не по популярности технологии.
- **Компоненты:** eval runner, Model Gateway alias/revision contract, IndexContract и deployment architecture.
- **Минимальная реализация:** shadow index/run, одинаковый benchmark manifest, immutable model/index revisions и rollback path. В основной contract вариант попадает только после заранее заданного improvement threshold без критической регрессии latency/citations.
- **Проверка:** paired statistical comparison, resource usage, multilingual slices, absence/conflict behavior и rebuild/rollback rehearsal.
- **Зависимости:** весь P0; отдельные P1-компоненты по предмету эксперимента.
- **Риск переноса:** benchmark производителя модели или OpenSearch 3.x demo недостаточны. Любая замена — только после локального воспроизводимого результата.

## 9. Новые и расширяемые контракты

Ниже — логические схемы. Конкретный transport JSON/Pydantic/SQL должен быть согласован с существующими repository/schema owners при реализации.

### 9.1 `RetrievalUnitV2`

```yaml
retrieval_unit_id: stable string
tenant_id: server-owned UUID
knowledge_base_id: server-owned UUID
document_id: canonical UUID
document_version_id: canonical UUID
source:
  type: upload | zim | confluence | jira | gitlab | crawler | web
  source_id: stable external identifier
  canonical_uri: string
language:
  primary: BCP-47-like code
  confidence: 0..1
  mixed: boolean
structure:
  heading_path: [string]
  block_type: paragraph | list | table | code | caption | other
  parent_unit_id: optional string
  neighbor_unit_ids: [string]
retrieval_text: string
source_span:
  exact_text: string
  char_start: optional integer
  char_end: optional integer
  page: optional integer
  sheet: optional string
  slide: optional integer
parent_context: optional string
acl: canonical server-derived access metadata
provenance:
  parser_version: string
  chunker_version: string
  content_hash: string
```

`retrieval_text` можно оптимизировать для поиска, но цитата всегда строится из `source_span.exact_text`. `tenant_id`, KB scope и ACL никогда не берутся из недоверенного query plan.

### 9.2 `SearchPlan`

```yaml
plan_version: string
mode: fast | balanced | research
intent: normalized natural-language intent
language_hints: [string]
source_scope: server-resolved references
query_variants: [string]
operations:
  - id: stable node id
    type: exact | search | search_many | fetch_many | filter | deduplicate | join | aggregate | coverage_check
    depends_on: [node id]
    parameters: validated typed parameters
budgets:
  max_nodes: integer
  max_queries: integer
  max_fetches: integer
  max_concurrency: integer
  max_result_bytes: integer
```

План не содержит OpenSearch DSL, SQL, credentials или произвольного кода. Executor компилирует typed intent в существующие server-owned filters и retrieval profiles.

### 9.3 `SearchReceipt`

```yaml
receipt_version: string
query_run_id: UUID
index_contract_ids: [string]
scope_summary: safe source/KB identifiers
executed_operations:
  - node_id: string
    status: completed | partial | failed | skipped
    candidate_input: integer
    candidate_output: integer
    latency_ms: integer
source_coverage: safe per-source counts/freshness
stopping_reason: sufficient | budget_exhausted | sources_exhausted | incomplete | cancelled
```

Receipt содержит безопасную структурную информацию и ссылки на защищённые события, но не копирует prompts, provider payloads или raw document text в обычные логи.

### 9.4 `AnswerabilityEnvelope`

```yaml
status: answered | partial | conflicting | not_found_in_scope
confidence: 0..1
supported_claims: [claim reference]
unsupported_claims: [claim description]
conflicts: [claim/evidence references]
missing_parts: [string]
receipt_id: UUID
scope_statement: human-readable boundary of the conclusion
```

`not_found_in_scope` допустим только при `stopping_reason: sources_exhausted` или эквивалентном успешном полном поиске. `budget_exhausted`, connector failure и stale/unavailable index означают незавершённый поиск, а не отсутствие факта.

### 9.5 Versioned `IndexContract`

Существующий owner — [`src/wikipediarag/retrieval_contract.py`](../src/wikipediarag/retrieval_contract.py). Контракт расширяется, а не дублируется:

```yaml
schema_version: v2
parser_version: string
chunker_version: string
retrieval_unit_schema_version: string
analyzer_profile_version: string
embedding_alias: Model Gateway alias
embedding_revision/hash: immutable reference
dimensions: integer
source_types: [string]
```

Rollout: построить physical index v2 → проверить contract и document counts → прогнать paired eval → переключить read alias → сохранить v1 для bounded rollback → удалить старый индекс только отдельной одобренной операцией.

### 9.6 Research branch state

```yaml
branch_id: UUID
goal: string
dependencies: [branch_id]
coverage_target: structured criteria
evidence_gaps: [string]
status: planned | ready | running | completed | partial | failed | skipped
attempts: integer
event_cursor: durable sequence
result_refs: [evidence/claim IDs]
```

Branch state хранится в PostgreSQL/event log; prompt context является временной проекцией durable state, а не единственным местом хранения прогресса.

## 10. Критерии принятия всей программы улучшений

Работа считается доказанной не по наличию новых классов, а по следующим исходам:

1. **Качество:** новый pipeline улучшает заранее выбранные primary metrics на versioned multilingual/cross-source benchmark без регрессии citation precision и false-answer rate.
2. **Отсутствие ответа:** absent/conflict/partial cases завершаются правильным `AnswerabilityEnvelope`; `not_found_in_scope` никогда не выдаётся при incomplete search.
3. **Цитаты:** каждая существенная claim имеет source span, который открывается в исходном документе или по canonical URL; compressor не создаёт paraphrased pseudo-citation.
4. **Скорость:** `fast` имеет меньший p50/p95, а batch research сокращает wall time на разложимых задачах при bounded Model Gateway/tool usage.
5. **Ingestion:** update/delete/attachment и повторный sync дают правильную поисковую проекцию без дублей и stale results.
6. **Multilingual:** качество отчётно разделено по языкам и mixed/cross-language случаям; средняя метрика не скрывает провал отдельного языка.
7. **Воспроизводимость:** каждый eval/report сохраняет index contract, model configuration revision/hash, retrieval policy и source snapshot/freshness.
8. **Границы:** бизнес-модели вызываются только через Model Gateway; OpenSearch остаётся rebuildable derived state; HTTP request не выполняет синхронный ingestion больших файлов.

## 11. Что намеренно не входит в этот план

Без отдельного исследовательского решения не добавляются:

- GraphRAG;
- ColBERT/late-interaction retrieval;
- learned sparse retrieval/SPLADE;
- proposition indexing;
- RAPTOR или другая иерархическая summarization index;
- generic multi-agent swarm;
- миграция на другую vector database;
- прямой LLM-generated OpenSearch DSL/SQL;
- произвольное выполнение сгенерированного Python в основном search path;
- обновление OpenSearch только ради демонстрационных agentic/relevance features.

Эти подходы не объявляются бесполезными. Они вынесены за границы, потому что текущие bottlenecks — eval coverage, upload retrieval units, multilingual lexical path, candidate cascade, connector completeness и доказательность отрицательного ответа — можно исправить меньшим и лучше проверяемым изменением.

## 12. Рекомендуемая последовательность без календарных спринтов

1. Зафиксировать расширенный eval contract и исходный baseline.
2. Реализовать RetrievalUnitV2/Index v2 вместе с multilingual mapping.
3. Настроить retrieval cascade и AnswerabilityEnvelope/SearchReceipt на новом benchmark.
4. Закрыть ingestion lifecycle существующих коннекторов.
5. Добавить extractive compression и режимы `fast/balanced/research`.
6. Ввести typed batch primitives и аналитические операции.
7. Расширить Deep Research coverage-driven waves и optional web source.
8. Подключить агрегированные metrics и только затем принимать решения о новых моделях, policy training или OpenSearch upgrade.

Такая последовательность сначала создаёт способ отличить реальное улучшение от красивой архитектурной гипотезы, затем улучшает качество исходных retrieval units и только после этого усложняет agentic orchestration.

## 13. Источники

### Первичные материалы 2026 года

- Perplexity Research, [Rethinking Search as Code Generation](https://research.perplexity.ai/articles/rethinking-search-as-code-generation), 2026-06-01.
- Perplexity Research, [Query-Aware Context Compression for Better Snippets](https://research.perplexity.ai/articles/query-aware-context-compression-for-better-snippets), 2026-05-14.
- Perplexity Research, [WANDR: Evaluating Research Agents That Must Search Wide and Deep](https://research.perplexity.ai/articles/wandr-benchmark-evaluating-research-agents-that-must-search-wide-and-deep), 2026-07-14.
- Perplexity Research, [Advancing Search-Augmented Language Models](https://research.perplexity.ai/articles/advancing-search-augmented-language-models), 2026-04-22. Используется только как аргумент в пользу будущих policy-training экспериментов после eval, не как P0-рекомендация.
- Microsoft, [Retrieval-augmented generation overview — Azure AI Search](https://learn.microsoft.com/en-us/azure/search/retrieval-augmented-generation-overview), актуальная редакция 2026 года.
- Google, [Gemini Deep Research API](https://ai.google.dev/gemini-api/docs/deep-research), актуальная редакция 2026 года.
- OpenAI Docs, [Deep research](https://developers.openai.com/api/docs/guides/deep-research), актуальная редакция 2026 года.
- Anthropic Engineering, [Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents), 2026-04-08.
- Anthropic Engineering, [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), 2026-01-09.
- Elastic, [Agentic AI search with deterministic guardrails in Elasticsearch](https://www.elastic.co/search-labs/blog/agentic-ai-search-deterministic-guardrail-query-execution), 2026-05-18.
- OpenSearch, [Optimizing hybrid search](https://docs.opensearch.org/latest/search-plugins/search-relevance/optimize-hybrid-search/), актуальная документация; применимость к 2.17.1 не предполагается.

### Дополнительные научные источники

- [Beyond Chunk-Then-Embed: A Systematic Study of Document Segmentation for Retrieval-Augmented Generation](https://arxiv.org/abs/2602.16974), 2026. Результаты требуют воспроизведения на корпусе проекта.
- [Claim-selective certification for retrieval-augmented generation](https://arxiv.org/abs/2605.21949), 2026. Используется как подтверждение формы claim-level статусов, но не как источник порогов.

### Внутренние материалы

- [`README.md`](../README.md).
- [`docs/STATUS.md`](STATUS.md).
- [`docs/architecture.md`](architecture.md).
- [`docs/architecture/search-and-deep-research.md`](architecture/search-and-deep-research.md).
- `C:\Users\Компьютер\Downloads\search_agent_deep_research_blueprint.md`.
- `C:\Users\Компьютер\Downloads\search_agent_deep_research_improvement_plan.md`.

Публикации поставщиков подтверждают направление эксперимента, но не локальный результат. Финальным источником решения для WikipediaRag являются code evidence, versioned benchmark, воспроизводимый eval run и измеренный end-to-end пользовательский исход.
