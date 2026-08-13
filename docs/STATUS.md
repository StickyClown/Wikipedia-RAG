# Project Status

Last updated: 2026-08-13

## AUTH-004 / DR-003 operational-gate foundation (2026-08-13)

Public HTTP routes now carry typed authorization metadata that is exported as
the `x-wikipediarag-authorization` OpenAPI extension. The contract is checked
against every registered route and identifies tenant boundary, capability,
scenario family, cross-tenant behavior, and document/retrieval/research output
surfaces. This replaces the duplicated hand-maintained unit inventory.

`verify-provider-acl-revocation` is implemented for a separately managed
OpenRouter-ready stack: it uses a test-only DB identity seeder, performs upload
through current ACL revoke through HTTP, and checks every tagged exposure route
for the revoked marker and linked evidence IDs. It is not yet executed evidence.
`verify-live-http-authorization-matrix` now seeds two generated tenants plus
non-platform tenant-admin/KB-owner actors, creates HTTP-owned fixtures, and
executes the authorized and cross-tenant/actor-scoped replay for every route.
Denied resource mutations are checked against a content-free pre/post target
hash. The 2026-08-13 local attempt reached the readiness prerequisite but was
correctly non-passing because Model Gateway reported `structured_schema_invalid`
and API `/ready` was `degraded`; no route-matrix pass is claimed. This is an
operational environment blocker, not a skipped route or a completed AUTH-004
acceptance result.

## MODEL-002/003, AUTH-004, DR-003 security closure (2026-08-13)

`MODEL-002` remains closed for the current provider-driver set: runtime imports
are guarded by `tests/unit/test_architecture_boundaries.py`; a new provider SDK
must extend that allowlist in the same change. `MODEL-003` now has direct
regression evidence that an incomplete active revision degrades Gateway
readiness and rejects alias calls with `MODEL_REVISION_SNAPSHOT_INCOMPLETE`,
without falling back to YAML. The local mock functional path still confirms the
active revision/hash for chat, embeddings and rerank.

AUTH-004 now has a fail-closed route inventory for every public API handler.
Search authorizes client `source_id`, `document_id` and `knowledge_base_id`
filters before retrieval and rejects group/object-prefix authority filters with
`AUTHORITY_FILTER_FORBIDDEN`. XML, index and ZIM import fields retain their
public names but accept only existing basenames below operator-owned roots;
invalid names return `IMPORT_FILE_NAME_INVALID`, and worker import resolution
repeats that containment check.

DR-003 continues to rebuild both research context and public detail from
currently retrievable, ACL-visible evidence; claims with a revoked linked
evidence item are omitted. The current focused verification is deterministic
unit plus PostgreSQL persistence coverage.

**Closure state:** MODEL-002 and MODEL-003 are closed within their documented
boundaries. AUTH-004 and DR-003 are only partially closed: the remaining
approved proof work is a full authorised/cross-tenant API route matrix and a
provider-backed ACL-revocation E2E respectively. They are listed explicitly in
the architecture refactoring queue and are not represented as completed.

Executed evidence for this change:

- focused Ruff — passed;
- focused Mypy — passed;
- focused unit suite — 101 passed;
- `WIKIPEDIARAG_INTEGRATION_DATABASE_URL=... uv run pytest tests/integration/test_deep_research_persistence.py -q` — 1 passed;
- `WIKIPEDIARAG_FUNCTIONAL_DATABASE_URL=... uv run pytest tests/functional/test_model_runtime_config_path.py -q` — 1 passed.

## AUTH-003 / ING-003 Definition-of-Done evidence closure (2026-08-13)

Добавлен `tests/integration/test_search_projection_reconciliation.py`. Каждый
тест поднимает отдельную временную PostgreSQL database, применяет миграции
001–009 и использует уникальный physical index/read/write alias в реальном
OpenSearch 2.17. Все девять ранее отсутствовавших доказательств выполнены
детерминированно; production-код и схема после миграции 009 не менялись.

Executed evidence:

- `uv run pytest tests/unit` — 498 passed.
- `uv run pytest tests/integration` — 19 passed.
- `uv run pytest tests/integration/test_search_projection_reconciliation.py -q` — 4 passed.
- `WIKIPEDIARAG_REQUIRE_FUNCTIONAL=1 MODEL_PROVIDER=mock RETRIEVAL_PROFILE=upload_mock uv run pytest tests/functional/test_retrieval_business_path.py -q` — 1 passed.
- `pnpm playwright test --workers=1 playwright/ui-upload-search.spec.ts` — 3 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests` — passed.

Functional proof matrix:

| Proof | Before | After | Executed evidence |
| --- | --- | --- | --- |
| Missing OpenSearch projection restored | FAIL — deterministic real-service proof absent | PASS | real reconciliation integration test |
| Extra OpenSearch projection removed | FAIL — deterministic real-service proof absent | PASS | real reconciliation integration test |
| Second reconciliation has zero projection delta | FAIL — deterministic real-service proof absent | PASS | counted real OpenSearch mutation calls = 0 |
| No new `document_publication` event | FAIL — deterministic real-service proof absent | PASS | PostgreSQL event count unchanged |
| Historical scheduling bounded by configured batch | FAIL — deterministic real-service proof absent | PASS | real PostgreSQL batch size assertion |
| Repeated scheduling converges to zero work | FAIL — deterministic real-service proof absent | PASS | repeated scheduler calls return 0 |
| Concurrent schedulers cannot duplicate document/generation work | FAIL — deterministic real-service proof absent | PASS | concurrent PostgreSQL connections + unique-row assertion |
| Lease-loss reclaim converges after a possibly successful mutation | FAIL — deterministic real-service proof absent | PASS | real OpenSearch mutation, expired lease, second worker reclaim |
| Retention is bounded and preserves non-eligible work/canonical state | FAIL — deterministic real-service proof absent | PASS | bounded PostgreSQL cleanup and canonical snapshot |
| SAFETY HTTP/API/debug corrupted staged projection | PASS | PASS | functional retrieval gate |
| SAFETY Search UI corrupted staged projection | PASS | PASS | Playwright Search gate |

## Дополнение от 2026-08-13: точная сверка поисковой проекции

Добавлена миграция `008_search_projection_reconciliation` и ограниченная
сверка проекции документа. Работник читает допустимые фрагменты только из
PostgreSQL: документ должен быть активен, фрагмент — опубликован, а его версия
— текущей, опубликованной и активной. Затем он ограниченно сверяет точный набор
производных записей OpenSearch, удаляет только явно найденные лишние записи,
записывает канонические и повторно сравнивает отпечаток. Отпечаток канонизирует
порядок полей правила доступа, поэтому одинаковые правила не дают ложной ошибки.

Подтверждено на локальном стеке с `MODEL_PROVIDER=mock` и
`RETRIEVAL_PROFILE=upload_mock`: HTTP-путь загрузки и поиска, сквозная проверка
снимка фактической настройки модели, а также экранные пути поиска и вопроса с
открытием источника. После перезапуска контейнеры были явно пересозданы с этими
переменными: передача переменных только команде браузерной проверки не меняет
окружение уже работающих служб.

Закрыта историческая часть **AUTH-003 / ING-003**: миграция `009` хранит
поколение и durable cursor ограниченного discovery, уникальная строка на
`document_id` не допускает дублирование logical work, а long repair продлевает
lease условно по owner/state. Потерявший lease worker не делает следующий
OpenSearch mutation и не завершает строку. Reconciler применяет bounded delta,
проверяет bulk items и не создаёт новый `document_publication`; завершённые
projection events удаляются ограниченными пакетами по retention. Backlog
остаётся наблюдаемым в `/ready`, но не делает service unavailable: PostgreSQL
confirmation уже сохраняет safety.

Функциональный тест с `MODEL_PROVIDER=mock` вручную оставляет в OpenSearch
реалистичную запись `published`, одновременно переводя канонический chunk в
`staged`. Она намеренно попадает в BM25 и dense candidates, но отсутствует в
public Search, retrieval debug и browser Search. Полная матрица route IDs
(AUTH-004) и отдельный provider-backed отзыв видимости Deep Research (DR-003)
остаются незавершёнными сквозными доказательствами; базовые guards уже
реализованы и отражены в текущем security closure выше.

## Защитные и сквозные проверки перед упрощением договоров

Добавлен набор наблюдаемых проверок D-01—D-09 для подтверждённых повторов без
объединения их рабочего кода. Для поиска OpenSearch теперь отбирает только
кандидатов с признаком публикации, а PostgreSQL пакетно подтверждает публикацию,
активность документа и актуальные правила доступа до объединения результатов и
перед выдачей из Redis. Изменение документа участвует в отпечатке окна поиска.

Добавлены целевые команды и документ выбора сквозной проверки. Обычный путь
поиска использует только местный имитационный поставщик. Настройки
`Settings`, `.env.example`, Compose и YAML-профили намеренно не объединялись.

Следующее исправление AUTH-003 добавило миграцию `007_search_projection_events`.
PostgreSQL сохраняет изменение доступа и событие обновления OpenSearch в одной
транзакции; отдельный рабочий путь повторяет это обновление с ограничением
попыток. `/ready` показывает состояние этой очереди как `search_projection`.
Чтение всё ещё повторно подтверждает доступ в PostgreSQL, поэтому задержка или
ошибка OpenSearch может скрыть результат, но не раскрыть документ. Gateway
получил безопасную метку фактической конфигурации в служебных данных ответа;
активная конфигурация БД используется для запросов по псевдониму, когда она
существует, а YAML остаётся начальным каталогом.

Выполнен сквозной сценарий отдельного пользователя с ролью `EDITOR` только в
первой базе: реальный вход через HTTP не даёт переданному клиентом идентификатору
второй базы расширить доступ. После прямого ужесточения правила в PostgreSQL
устаревшие Redis и OpenSearch повторно проверяются и не раскрывают документ.

Публикация теперь сохраняет канонические опубликованные строки и долговечное
событие `document_publication` в одной транзакции. Работник загружает только эти
строки, сверяет точный набор и отпечаток фрагментов, идемпотентно заменяет
проекцию версии и ограниченно повторяет ошибку. Отсутствующая прежняя проекция
OpenSearch является безопасным пустым состоянием. Историческая сверка теперь
покрыта реальными PostgreSQL/OpenSearch integration tests с bounded discovery,
идемпотентным повтором, lease reclaim и retention; отдельный staged-фрагмент
по-прежнему проверяется существующим functional/UI safety gate.

Для MODEL-003 активная ревизия PostgreSQL хранит неизменяемый снимок псевдонимов;
Gateway не переходит к YAML при неполном активном снимке. Сквозная проверка
временно активирует локальную ревизию и подтверждает чат, векторизацию и
перестановку через `ModelClient → Gateway → mock`, включая идентификатор и хэш
фактической ревизии. Внешние поставщики и RRNCB не запускались.

## Universal Model Gateway / RRNCB v3 status

The active implementation now compiles every model request through the
OpenAI-compatible Gateway contract (`/v1/chat/completions`, `/v1/embeddings`,
configurable `/rerank`). Connection adapters declare endpoint paths, standard
field mappings and thinking paths; arbitrary JSON vendor parameters are merged
recursively, while transport fields and the bounded token envelope remain
Gateway-owned. The additive database migration is `006_model_gateway_request_adapter`.

`generator_fast` and `verifier` resolve to Qwen 3.5 9B; `generator_main` is
currently switched to OpenRouter Qwen 3.6-27B. All active chat aliases declare
an 80k context and a 4096-token startup canary. Deep Research planner and
synthesis use `generator_main` (80k/16k), verifier uses `verifier` (24k/4096),
and ordinary Answer remains 4096. Thinking-off is declared by adapter rather
than selected by provider-specific Python branches. Qwen3-30B-A3B-Thinking-2507
and local llama.cpp embedding remain inactive future connections.

RRNCB v3 ingestion remains complete and immutable:
`rrncb-public-v3-ingest-20260811` (65 documents, 13 batches); retrieval
preflight is 10/10. Existing failed runs remain unchanged. A generation
structured canary is now part of RRNCB preflight, so baseline cannot start on
retrieval evidence alone; the run contract records active revision/hash,
resolved provider models, connection IDs and adapter hashes. A new dev run must
be created only after Gateway/API readiness and model smoke are green.

Live validation on 2026-08-12: additive migration `006_model_gateway_request_adapter`
is applied; Gateway `/ready` and API `/ready` are `ok`; and
`smoke-models --provider openrouter` passed embedding `1024`, structured JSON
and rerank ordering. A new RRNCB dev attempt was started under the separate
`rrncb-public-v3-gateway-20260812` name but was stopped during the long
preflight before a run artifact or terminal result was written. Therefore no
40/40 or 160/160 quality claim is made yet; the next operator action is to run
that immutable dev split and resume its successful run for test.

Execution attempt after that validation: old baseline directories were retired
from the active `runs/` directory. New immutable dev runs
`rrncb-public-v3-20260812-dev2` and `rrncb-public-v3-20260812-dev3` both passed
retrieval preflight and the structured generation canary, then stopped on the
first task (`rrncb-0010`) with terminal `MODEL_OUTPUT_INVALID`. The live API
diagnostic is the safe citation-contract reason `structured model output claim
evidence is not cited in the answer`; this is a model-quality contract failure,
not a readiness or transport failure. No test split was started.

After switching `generator_main` to Qwen 3.6-27B, Gateway `/ready` remained
`ok`. Two single-question dev checks also reached terminal responses but both
failed strict answer validation: `rrncb-0010`
(`query_run_id=1ac693bd-0739-36d2-930f-57fe60d133b0`) failed because claim
evidence was not cited in the answer; `rrncb-0001`
(`query_run_id=097c5fd1-9c24-0f37-ee39-573c1bae6600`) failed because a single
answer contained interpretations. No successful dev question or 40/40 run has
been recorded, and the 160-task test split remains blocked by the terminal
dev-contract requirement.

An ad-hoc API/RAG check on 2026-08-12 reran the `rrncb-0010` question against
the RRNCB v3 knowledge base and completed as query run
`3d448bc7-b6f7-c8a0-6cbf-36f6201a1224` with three citations and no
model-contract abstention. This is not an RRNCB evaluator dev result and does
not change the 40/40 or test-split gate.

The current source now treats a parsed semantic answer-contract conflict
(claims/citations, answer mode or interpretations) as a completed, safe
abstention with a stable reason rather than exposing a terminal
`MODEL_OUTPUT_INVALID`. The abstention has no citations, claims or
interpretations and is recorded separately from retrieval answerability. This
does not establish model quality or alter RRNCB state. Malformed, schema-invalid
and truncated model output remain terminal `MODEL_OUTPUT_INVALID` or
`MODEL_OUTPUT_TRUNCATED`; no repair call or retry is introduced.

Focused local validation for this correction passed: the answering, Gateway,
model-control, diagnostics and API contract tests report 85 passed; focused
Ruff and mypy report no issues. RRNCB and paid provider calls were not run.

## Current milestone

**Fast source-agnostic ambiguity answering**

The active chat milestone keeps the final context at no more than 12 evidence,
uses one generator call, and sends no more than 24 candidates to the reranker by
default. `ambiguity_mode` is `off`, `auto` (default), or `always`; the same
generator determines whether the supplied evidence supports one meaning or
several. In `auto`/`always`, context packing gives the first eight slots to
different documents and fills the remaining four by relevance, with at most two
evidence items per document. No source-type rules, ingestion changes, ZIM work,
semantic clustering, extra embeddings, or second rerank are part of this
milestone.

Completion means the model lists every meaning actually supported by the 12
evidence items and the user can focus the next chat retrieval on a selected
meaning or deterministic free-text clarification. RRNCB baseline work starts
only after this milestone.

`always` now uses a strict structured contract only when the existing
answerability signal reports `ambiguous_entity=true`: the generator must return
`answer_mode=multiple`, at least two cited interpretations and a clarification
question. A prose-only list without the structured interpretations produces a
safe contract-abstention rather than a grounded answer; no second generator call
is used. When the signal is false, the normal single-answer contract remains
available.

The final provider-backed three-mode Browser smoke remains pending because
structured answer quality still fails the citation contract on Qwen 3.6-27B.
Gateway/API readiness and deterministic ambiguity-contract, retrieval and UI
build checks remain green; semantic contract failures are not presented as
grounded answers and do not trigger a fallback or retry.

Deep Research has moved from the initial V1 single-KB slice to a local-first
stage-profile runtime: durable episodes, typed evidence/claim/decision memory,
pause/resume/cancel, heartbeat recovery, verified synthesis and an
ACL-trimmed report surface.

The latest increments add the approved local-first SOTA foundations:
stage-specific Deep Research context windows through Model Gateway metadata and
tokenizer contract, `80k` planner/synthesis profiles with a separate `24k`
verifier, a closed five-tool local-private registry, Multi-KB scoped research
runs for one to three KBs in the same tenant, durable claim relations and
decision records, and report rebuild from current ACL-visible verified claims.
Ordinary Search and the chat Extended Search harness keep their existing
smaller normal-search context profile. The runtime default productive target
remains `45%` until a fresh real-model/local-model matrix safely beats it.

## Live UI runtime correction (2026-08-10)

The observed live regressions are corrected in source and covered by focused
unit/UI checks. `auto` is now a public UI sentinel only: server-owned profile
resolution converts it to default selection across the full KB scope, while
new research plans and runs persist the resolved concrete profile. Research
scope always contains its primary KB. Unknown explicit profiles return safe
`422 RETRIEVAL_PROFILE_UNKNOWN`; incompatible scopes return safe
`409 RETRIEVAL_PROFILE_INCOMPATIBLE`; the profile catalog stays `200` with
all profiles marked incompatible and a `scope_error_code` rather than leaking
a `KeyError` as a 500.

Interrupted normal/extended chat query runs are recovered at API startup as
`failed/STALE_QUERY_RUN_RECOVERED`. Streaming chat now owns and joins active
retrieval/generation tasks on cancellation, records the already-completed
provider failure if it wins the disconnect race, otherwise records
`CLIENT_DISCONNECTED`. Gateway provider errors expose only safe `code` and
`retryable` metadata; client retries are limited to retryable failures. Recent
Gateway alias failures temporarily degrade `/ready` until success or cooldown.

The UI omits `retrieval_profile` for auto, blocks Ask/Search/Research when the
selected scope lacks a shared profile, shows its safe reason, keeps terminal
Ask errors visible, renders model connection test state and time, and scrolls
and focuses the Document Viewer heading after structure arrives. Viewer
loading/errors are also visible before structure. Playwright helpers now select
exactly one isolated upload KB and set research scope separately; blank model
forms remain disabled with required-field guidance.

Focused validation completed: `uv run pytest tests/unit/test_research_plan_endpoint.py
tests/unit/test_retrieval_profile.py tests/unit/test_query_run_recovery.py
tests/unit/test_api_readiness.py tests/unit/test_gateway_app.py
tests/unit/test_model_client_observability.py -q` (39 passed); `pnpm test --
App.protocol.test.ts` (3 passed); UI lint, typecheck and production build
passed. Full provider-backed browser smoke is not claimed here: it still
requires healthy configured credentials and the documented ZIM deployment
prerequisite.

Runtime rebuild verification: `docker compose up -d --build api worker
model-gateway ui` completed without deleting volumes and API `/ready` returned
`ok`. The first isolated Playwright attempt found stale helper assertions
against hidden `<option>` elements; the second reached successful TXT
publication but found an ambiguous `published` locator and cross-origin
teardown fetch. Both helpers have been corrected in source. The complete mock
E2E acceptance remains pending a final clean run; provider-backed acceptance
is also pending real-provider canary health and is not represented as passed.

## Reliability & UX Correction V2 (2026-08-10)

The approved post-RRNCB correction is implemented in the current source tree.
Structured model output now uses typed `AnswerDraft`/`ClaimDraft` validation;
BOM/fenced JSON is normalized locally, while malformed or truncated output ends
with `MODEL_OUTPUT_INVALID`/`MODEL_OUTPUT_TRUNCATED` and never triggers an
automatic repair call. Parsed semantic contract conflicts now terminalize as
safe abstentions with redacted reason metadata, never as grounded answers. The
Gateway performs a positive synthetic grounded-claim schema/max-token startup
canary, keeps alias readiness/circuit state, and the mock provider has
deterministic malformed/truncated/delayed/schema-mismatch modes.

Chat, replay and eval share canonical safe failures, one monotonic deadline,
durable terminal query runs, disconnect cancellation, additive stage/heartbeat
SSE fields and idempotent `client_request_id` replay. Eval consumes SSE
incrementally and retains the query-run ID, last stage, terminal status and a
read-only retrieval snapshot after a truncated stream. Retrieval profile
resolution is server-side across every scoped KB; exact entity/title handling,
answerability, contextual abstention and bounded context packing cover the
RRNCB regressions (including the table-number false conflict). Worker and
parser persistence no longer writes exception class names as public error
codes; transport/parser/research failures use the canonical safe taxonomy.

Chat/Search UI now share an explicit multi-KB scope, use the backend `auto`
profile, show heartbeats/stages and cancellable loading states, render grouped
document results and safe citation/error details, and use stable transport
idempotency keys with a new key for explicit retry. UI protocol tests and a
Playwright smoke cover sequence gaps, terminal events, grouped Search and
failure-source retention.

Validation completed: `uv run pytest tests/unit -q` (387 passed in the latest
run), `uv run pytest
tests/integration/test_eval_runner.py -q` (8 passed), Ruff lint/format and
Mypy for `src/wikipediarag`, UI Vitest (2), Playwright (1), typecheck, lint and
production build all passed. The isolated Compose reliability smoke passed on
2026-08-10 in
`artifacts/validation/reliability/20260810T130725Z/report.json`: 2/2 uploads
published across a worker stop/restart, four terminal chat runs (two injected
Gateway failures followed by two successes), stable SSE sequences and an
idempotent replay with one `query_run_id`. Its disposable Compose project is
left intact for inspection; no corpus, existing KB, index or volume was
removed.

The old `rrncb-public-v1` run remains immutable and was not resumed or mixed
with this work. No new real 200-question RRNCB baseline was started; it is
allowed only after the rebuilt runtime uses the intended provider/profile and
the reliability smoke is green. The valid pinned RRNCB CSV revision in the
source is the 40-character Hugging Face commit ending in `...acac166` (the
shorter revision string in the original plan resolves to 404).

Follow-up verification rebuilt `ui`, `api`, `worker` and `model-gateway` from
the current source without removing data or volumes. The UI now renders the
safe `degraded` API status as localized `degraded`/`частично готово`, rather
than `checking`; its accessible label, `auto` retrieval profile and workspace
tab navigation were verified in the rebuilt browser UI with no console errors.
`uv run pytest tests/unit -q` passed 387 tests, Ruff and Mypy passed, and the
UI lint, typecheck, Vitest (3), production build and Playwright smoke passed.
The rebuilt real-provider Gateway is now ready. The earlier safe
`provider_smoke_failed`/`MODEL_ALIAS_UNREADY` result was a false canary failure,
not a missing key, exhausted quota or unavailable model: OpenRouter returned
HTTP 200, but Qwen 3.6 spent each bounded 64/128/256/512-token diagnostic
response on its default reasoning and ended with `finish_reason=length` before
emitting the required JSON. The startup canary now disables optional reasoning
for this short contract-only request while retaining the production answer
schema and 64-token output bound. No provider alias, readiness rule or real
profile was weakened and mock did not replace OpenRouter. After rebuilding only
the Gateway stack, Gateway `/ready` and API `/ready` both returned `ok`, all five
OpenRouter aliases were healthy, and `smoke-models --provider openrouter`
validated 1024-dimensional embeddings, structured chat and rerank ordering.

The repository-wide UI `pnpm format:check` is also green. The Playwright smoke
and `pnpm-lock.yaml` were formatted, while generated `test-results/` is now
excluded through `.prettierignore` so later Playwright runs do not reintroduce
the failure. Final validation on 2026-08-10: `uv run pytest tests/unit -q`
passed 388 tests; focused Ruff lint/format passed; UI lint, typecheck, Vitest
(3) and format check passed; `docker compose config --quiet` and
`git diff --check` passed; the rebuilt live OpenRouter smoke passed.

## Implemented now

- Playwright E2E contract coverage now includes local authentication, isolated
  knowledge-base creation/selection, in-memory TXT upload through visible
  ingestion progress, exact search/viewer verification, chat citations,
  retrieval debugger, Deep Research lifecycle entry, and safe disabled/error
  states. Authenticated checks remain opt-in and report `BLOCKED` skips when
  local API credentials or runtime dependencies are unavailable.
- A dedicated sequential `test:e2e:live` gate now validates the existing dev
  runtime without starting Compose: readiness, local login/session restore,
  HttpOnly/SameSite cookie attributes, logout, safe invalid-login response and
  CSRF-protected UI mutation. It accepts the documented localhost-only
  development credential fallback, while CI can make any `BLOCKED` preflight
  condition fatal with `WIKIPEDIARAG_REQUIRE_LIVE_E2E=1`.
- Local production-shaped RAG MVP with FastAPI API, worker, Model Gateway, React/Vite UI and Docker Compose dependencies.
- Web UI UX pass: desktop app shell with Chat/Search/Research/Knowledge Base tabs, compact KB context controls, structured Deep Research findings with clickable evidence refs, multi-KB scope selection (up to three), cancellable polling and Markdown/Word/CSV exports.
- UI read-only browser coverage now includes public smoke, RU/EN switching, offline login errors, mobile overflow checks, keyboard tab navigation and platform-admin model-control rendering. API/network failures are converted to localized safe messages without uncaught `Failed to fetch` console errors; generated Playwright results remain ignored by lint/format tooling. Authenticated suites are opt-in through local environment credentials and skip safely when the API is not running. Current UI validation: Playwright 6 passed/4 skipped (API unavailable), Vitest 3 passed, lint/typecheck/format/build passed.
- Local auth and OIDC foundation: Argon2id local passwords, opaque HttpOnly session cookie, CSRF, OIDC Authorization Code + PKCE, server-side encrypted provider tokens and `ActorContext`.
- Tenant and KB authorization foundation: platform, tenant and KB roles; local/OIDC groups; KB grants; route-level server-owned tenant/KB enforcement; safe error envelopes.
- Wikipedia ingestion from local ZIM/libzim with Kiwix source URLs, deterministic chunks, redirects as provenance and versioned OpenSearch publication; XML multistream fallback remains available.
- Async document upload and batch upload through presigned MinIO URLs, background validation/parsing/chunking/embedding and published-only retrieval.
- Document lifecycle hardening: KB-owner soft delete, immediate retrieval removal and deferred idempotent purge after the configured retention window.
- Parser runtime hardening in Compose: Xberg, Docling and metadata-service with sandbox settings, separate concurrency limiters and safe parser progress metadata.
- Retrieval: BM25, dense vectors, RRF, rerank, dedup/page quota, parent expansion, answerability, citation validation and experimental safe diagnostics.
- Direct and Extended Multi-KB chat/debug retrieval with all-KB role validation, bounded extended-search waves, composite `(knowledge_base_id, chunk_id)` evidence identity and KB-correct neighbour expansion.
- Ordinary user search: document-visibility-scoped `POST /api/v1/search` now
  runs through the hybrid retrieval pipeline with OpenSearch BM25/vector search, RRF,
  optional rerank, typed filter expressions, cursor pagination, optional
  highlights, facets and document grouping while preserving safe
  `KB_NOT_READY` handling and exact document-viewer open action by `chunk_id`.
- Search pagination uses Redis-backed tenant-scoped windows with v1/v2 cursor compatibility, power-of-two window growth and Redis-failure fallback to uncached retrieval.
- Worker job updates are lease-fenced; heartbeat loss cancels stale processors. Deep Research retrieval uses real deterministic query variants, one embedding batch and one global rerank, and chat emits stage progress events without holding a database transaction across external calls.
- Document navigation: `document_sections` persisted from published chunks,
  section-aware uploaded-document chunking, deterministic Wikipedia/ZIM chunk
  ordinals/locators, viewer-safe document structure/context/in-document search
  APIs and an inline React text viewer with TOC, local document search and
  neighboring chunk context.
- External source connection foundations: `knowledge_sources` now carry
  encrypted connector credentials, refresh metadata and sync cursors; source
  document states and sync-run journals track source versions, content hashes,
  current document versions and tombstones; APIs manage sources, healthchecks
  and sync jobs; worker scheduling enqueues due incremental source syncs.
- Stage 4 connectors: Confluence Data Center, Jira Data Center, GitLab
  Self-Managed, local folder, internal crawler, Kiwix/ZIM source-version
  tracking and deterministic Sunduk/DocSmart mocks share a connector contract
  that feeds the existing document ingestion pipeline.
- React/Vite source management UI now exposes the Stage 4 source kinds for the
  selected KB with safe create/list, healthcheck, incremental/full sync,
  disable/enable and sync-run status polling without returning stored
  credentials to the browser.
- Upload parity: `POST /api/v1/knowledge-bases/{kb_id}/documents` accepts
  multipart uploads through the API and reuses the same document upload records
  and async worker ingestion path as UI drag-and-drop uploads.
- Query-run observability V1: chat/debug queries persist protected query runs and retrieval events with stable stage names, query transforms, candidate rank movement, decision reasons, answerability reason codes, Model Gateway safe metadata, feedback/evaluation event endpoints and an updated Retrieval Debugger. PostgreSQL remains the source of truth; OpenTelemetry export is optional/no-op when packages are absent.
- Retrieval observability now carries explicit per-run `transform_id`/`subquery_id` query context through direct, extended and Multi-KB stage events, candidate debug payloads, extended harness search yield metrics and answer context source summaries.
- Search Quality V2 ACL/security trimming: documents are KB-visible by default,
  can be tenant-visible for all active-tenant actors, or restricted through
  trusted `metadata.document_access`; platform admins, tenant admins and KB
  managers/owners bypass document trimming, while restricted documents also
  support matching user/group allowlists. KB managers can update document access
  and source document access defaults through API/UI, and source defaults can be
  applied to existing synced documents. The same scope is applied to ordinary
  search, chat/debug retrieval, Multi-KB retrieval, Extended Search neighbor
  expansion, DB dense fallback and document viewer paths.
- Durable Deep Research: `research_runs`, `research_run_scopes`, episodes,
  questions, typed evidence records, verified claim records, claim relations,
  coverage records, public-safe tool calls, decisions and operational
  reflections are persisted in PostgreSQL; API endpoints create/list/read runs,
  expose compact events and request pause/resume/cancel; worker jobs with
  `kind='deep_research'` process one bounded episode at a time, checkpoint
  after every planner/tool transition, maintain heartbeat/stalled recovery and
  synthesize the public report only from current ACL-visible verified claims.
  Deep Research now supports one to three KBs in the same tenant through a
  server-owned scope snapshot: broad retrieval uses the existing retrieval
  stack, while document tools resolve only from already-visible evidence
  handles. Stage budgets default to `45%` productive target, `55%` soft limit,
  `70%` hard input limit, `15%` output reserve and `15%` safety reserve, with
  planner/reflection and synthesis on `80k` declared context, verifier on `24k`
  input/`2k` output, and ordinary Search/Extended Search unchanged.
- Deep Research operator/product surface: `research_plans` are now persisted as
  draft/approved records with questions, scope, retrieval profile, tool mode,
  context policy and notes; API endpoints create/list/read/patch/approve plans,
  approved plans can launch runs through `research_plan_id`, the worker now
  reports explicit `plan -> retrieve -> evaluate -> verify_claims ->
  synthesize -> quality_gate` progress stages, and broad `extended_search`
  keeps the original query while adding at most two bounded rewrites that stay
  inside the current tenant/KB scope and merge back through the existing
  retrieval stack.
- Eval and validation tooling: local-auth eval client, release-gate provider preflight, warm retrieval profiling, MIRACL-RU adapter, document corpus verification, cross-tenant hardening smoke command and Deep Research complex fixture/evaluator/runtime smoke harness.
- RRNCBPublic document benchmark tooling is implemented as a separate baseline increment:
  pinned CSV preparation validates 200 rows, 65 PDFs, 53 referenced documents and
  15 soft-unanswerable rows; deterministic dev/test manifests and SHA-256 source
  manifests are written under ignored `artifacts/eval/rrncb-public/` and the
  external CSV cache under `artifacts/eval/external/rrncb-public/cache/`.
  The public-API-only runner supports resumable five-document batches, bounded
  presigned uploads, ingestion timing/progress artifacts, KB-scoped retrieval and
  chat calls, ten-query retrieval preflight, and document-level Recall/MRR/nDCG,
  citation metrics and ROUGE-L. The historic `rrncb-public-v1` attempt ran on
  2026-08-10 against the local API after ingestion of all 65 PDFs and
  terminalized all 200 questions (162 completed, 38 technical failures). It is
  an immutable comparison point, not a successful baseline.
- Universal Model Control Plane foundation is now implemented as additive
  migration `005_model_control_plane`. PostgreSQL stores provider connections,
  encrypted versioned credentials, model contracts, immutable configuration
  revisions, stage bindings and safe validation runs; YAML is used only for
  first-run, no-secret bootstrap. The code-owned stage registry covers
  ingestion/retrieval, chat and Deep Research stages, keeps vision models
  catalog-only, and enforces one embedding fingerprint for document/query
  embeddings.
- Platform-admin-only model API and the UI Models tab expose connections,
  catalog, stage registry, draft/validate/activate/history operations, safe
  credential status, optimistic versions and localized RU/EN explanations.
  Drivers for OpenRouter, vLLM, llama.cpp, text-generation-webui,
  OpenAI-compatible endpoints and test-only Mock share one request contract;
  no driver manages endpoint processes. Thinking mappings, typed parameter
  precedence, token-envelope checks, tokenizer calibration and schema canary
  reserve reject unknown or unsafe settings.
- Model Gateway accepts the stage + immutable revision contract while retaining
  alias compatibility for CLI/tests. Active revisions control readiness when
  present; a failed unused catalog model does not degrade readiness, and no
  mock fallback is performed. Runtime records now have additive revision/hash
  columns for query, ingestion and research runs.
- The pre-control-plane comparison point is recorded as `pre-control-plane`
  with legacy YAML bundle hash
  `74d714f61305eb80d749f1bf3da0ec51529412030550908db3b82e2fb8c74aff`
  (`config/models.yaml` + `config/retrieval.yaml`). Any RRNCB run after
  activation must receive a new run ID and must be compared as a model-routing
  contract change, not as a pure quality delta.
- Validation after this increment: `uv run pytest tests/unit -q` -> 397 passed;
  integration suite -> 14 passed, 1 skipped; `uv run mypy src/wikipediarag` and
  focused Ruff checks passed; UI lint, typecheck and Prettier passed; `docker
  compose config --quiet` passed. A live database migration/activation and
  real local-endpoint conformance smoke remain deployment checks, not silently
  simulated by the application.

- Deep Research hard target fixtures: `tests/fixtures/deep_research/research_tasks_hard.json`
  covers alias resolution, multi-source bridge chains, policy exceptions, CSV
  evidence and contradiction-after-bridge cases. The fixtures now declare
  trajectory expectations for completed `extended_search` calls, durable
  derived questions and forbidden public raw-query/provider/storage leaks;
  `make deep-research-hard-gate` runs the hard OpenRouter/Qwen proxy runtime
  gate in a unique isolated Compose project, while `make deep-research-hard-smoke`
  is a compatibility alias. It uses fresh project-scoped PostgreSQL, MinIO and
  OpenSearch state so interrupted shared-stack jobs cannot affect a result.

## In progress

- Reliability Foundation V1 is implemented in the current Compose source tree:
  chat has one durable terminal `query_run`, monotonic 300-second deadline,
  disconnect cancellation, 10-second generation heartbeats and additive safe
  SSE correlation fields; the Model Gateway has alias-scoped circuit breaking
  and at most one automatic retry before a response; and upload/import/source
  sync/reprocess/research creation support 24-hour idempotency records. The
  document worker now persists one classified delayed retry before terminal
  failure. RRNCB uses stable chat request IDs, asynchronous SSE consumption,
  immutable run contracts and reports both conditional and end-to-end quality.
  The next real RRNCB baseline must use a fresh run ID after Compose has been
  rebuilt and the isolated reliability smoke has been executed; it must not be
  mixed with the existing 162/200 conditional-result baseline.

- Retrieval Correctness V3 implementation is now present in the shared retrieval
  contract. New ingestion writes scoped document/chunk/index identities, keeps
  native IDs in metadata and legacy mappings, and publishes tenant/KB-scoped
  OpenSearch names. Read-only BM25 no longer creates an index. Context
  selection uses stable ties, canonical content units, provenance-preserving
  parent deduplication, tokenizer-like budgeting and soft page-quota repair.
  Answerability now uses rank confidence, full title/alias matching and
  unsolicited divergent-value conflict detection. Retrieval telemetry is an
  allowlisted projection with a 256 KiB database cap and monotonic event
  sequence. Eval recall is fractional Recall@K and generated hard-negative /
  unanswerable labels are disjoint and measurable.
- Deterministic validation after Retrieval Correctness V3 changes:
  `uv run pytest tests/unit -q` -> 360 passed; `uv run ruff check src` ->
  passed; `uv run mypy src` -> passed; PostgreSQL schema application -> passed;
  `WIKIPEDIARAG_INTEGRATION_DATABASE_URL=... uv run pytest
  tests/integration/test_deep_research_persistence.py -q` -> 1 passed.
  Full `mypy src tests` and repository-wide format check still include
  pre-existing test/handler issues outside this increment and are tracked as
  validation debt.
- The bootstrap now records additive migration
  `001_retrieval_correctness_v3`; repeated startup remains idempotent and the
  new identity/event columns have a durable schema version marker.
- A one-task reviewed retrieval smoke was attempted against the already-running
  Compose API, but the container image predates this source checkout and
  returned an opaque 500 (`NotSupportedError`). Rebuilding `api`/`worker` was
  attempted and exceeded the bounded 120-second build window; no containers or
  volumes were removed. This smoke result is therefore not evidence against
  the source implementation and must be rerun after a successful image build.

- Deep Research policy tuning on the new stage-aware runtime: rerun the
  real-model/local-model matrix for `35%`, `45%` and `55%` productive targets
  against the `80k` planner/synthesis profile; change the default only if
  unsupported claims and ACL safety stay flat while coverage/recall improve.
- Deep Research quality tuning beyond the new runtime foundation: measure the
  five-tool registry on hard trajectories and validate the deterministic
  controller against real-provider latency and coverage.
- The retrieval/control defect identified in the focused real-provider run is
  implemented in the common retrieval contract: page quota keys are now
  `(knowledge_base_id, document_id, page_discriminator)`, document tools use a
  deterministic ACL-trimmed source router with persisted private routing
  history, and Extended Search stops only on an `ANSWERABLE` provisional final
  evidence set. `PARTIAL`/`CONFLICTING` results schedule bounded gap repair or
  end as `conflict_unresolved`; they cannot emit `evidence_sufficient`.
  A focused real-provider acceptance pass is still required before closing the
  quality blocker. Detailed analysis and implementation rationale:
  `docs/research/deep-research-controller-error-analysis-2026-08-06.md`.
- Browser UI OIDC through Dockerized API after the public/internal Keycloak URL strategy is decided.
- External deployment hardening remains planned, not supported production operation.

## Latest validation

- Reliability Foundation V1/V2 deterministic and UI checks on 2026-08-10 are
  recorded in the V2 section above. Earlier 378/386-count entries and the
  initial smoke-only contract mismatch are historical; the isolated fault
  smoke is now green and no real RRNCB rerun has been started.

- RRNCB benchmark implementation checks on 2026-08-09: `uv run pytest
  tests/unit/test_eval_document_benchmark.py tests/unit/test_eval_metrics.py
  tests/unit/test_eval_external.py tests/unit/test_eval_retrieval_profile.py
  tests/unit/test_eval_progress.py -q` -> 24 passed; `uv run pytest
  tests/integration/test_eval_runner.py -q` -> 8 passed; focused Ruff format,
  Ruff lint and mypy checks for `src/wikipediarag/eval` and `cli.py` passed.
  The historic failed `rrncb-public-v1` RRNCB attempt on 2026-08-10 used dataset hash
  `874489686ac5f10e598d0380fd2618e4fed9ecb328120ac70ab2526af9857d97`, KB
  `83f6ac93-18f6-436f-90ac-d3a25652e86f`, and ingestion time 2076.5 s for
  65/65 published PDFs. Preflight passed 10/10 gold documents in top-10.
  The RAG run took 11991.4 s; report artifacts are under
  `artifacts/eval/rrncb-public/rrncb-public-v1/` (`report.json`, `report.md`,
  `results.csv`, `latest-status.json`). The all-split baseline was document
  Recall@1/5/10 = 0.926/0.975/0.981, MRR@10 = 0.948, nDCG@10 = 0.957,
  citation precision/recall = 0.734/0.821, ROUGE-L = 0.213, token F1 =
  0.256, soft-unanswerable accuracy = 0.375, p50/p95 latency =
  51.0/105.6 s, and technical error rate = 0.19. This is a baseline, not a
  release gate; timeout and provider failures require follow-up before using
  the quality metrics as a release criterion.
  Repository unit suite afterwards: `uv run pytest tests/unit -q` -> 370 passed
  (two existing FastAPI deprecation warnings).

- Web UI UX pass on 2026-08-07: `services/ui` `pnpm lint`, `pnpm typecheck`,
  `pnpm build` and `pnpm format:check` all passed. Browser smoke against a
  disposable read-only local API fixture passed at 1440x900 and 1024x768:
  tab switching and keyboard navigation, hidden-panel state preservation,
  EN/RU persistence, source/research layouts and zero horizontal overflow.
  Full API action flow was not exercised because the Compose stack was not
  running during this UI-only pass.

- UI QA follow-up on 2026-08-08: Chat now renders busy, timeout and terminal
  SSE failure states; Search/Research errors use safe localized envelopes;
  terminal Research actions are disabled; Research plan fields and the
  document viewer have accessible/localized labels. Research plan creation
  maps `KnowledgeBaseNotReady` to the existing public `409 KB_NOT_READY`
  envelope without changing routes or payloads. `pnpm lint`, `pnpm typecheck`,
  `pnpm build`, `pnpm format:check`, targeted backend tests (`5 passed`) and
  `git diff --check` passed.

- Final deterministic controller validation on 2026-08-07:
  PostgreSQL persistence integration passed (`1 passed`) for idempotent query
  run/episode/tool/evidence/coverage/claim writes and deadline partial
  finalization. The focused mock hard gate passed with
  `coverage_score=1.0`, `evidence_recall=1.0`,
  `unsupported_claim_count=0`, `acl_safety=true`, six completed tool calls,
  and zero open required questions (`artifacts/validation/deep-research-hard-gate/20260807T150333Z/report.json`).
  The first preserved real-provider diagnostic isolated and fixed two
  PostgreSQL heartbeat type failures: nullable lease comparison without an
  explicit cast, then an incorrect UUID cast against the `text` lease column.
  The subsequent real-provider run completed with no controller/SQL failure,
  all questions `done`, `coverage_score=1.0`,
  `unsupported_claim_count=0`, `acl_safety=true`, but
  `evidence_recall=0.667` because the runbook marker was not retrieved
  (`artifacts/validation/deep-research-hard-gate/20260807T152545Z/report.json`).
  The full hard matrix was not run.

- Retrieval/evidence-control refactor on 2026-08-07: unit coverage now checks
  document-scoped page identity, quota accounting after token-budget drops,
  deterministic unseen-source routing and bounded partial repair. The focused
  mock suite passes. The focused real-provider acceptance pass also passed with
  `run_status=completed`, `coverage_score=1.0`, `evidence_recall=1.0`,
  `unsupported_claim_count=0`, `acl_safety=true`, zero open required questions
  and no raw query/provider/storage leaks. Its detail shows all three fixture
  sources and deterministic source rotation; the isolated Compose project and
  volumes were removed after log/report inspection. Report:
  `artifacts/validation/deep-research-hard-gate/20260807T165245Z/report.json`.

- Retrieval baseline locked on 2026-08-07 for dataset hash
  `a01d97a88620f5650601cc0a9ffe30165bb3f984048db0ca057a5b881d6a502a`
  (`generated-wikipedia-v1`, snapshot `5e698f31...`). The canonical release-gate
  configuration is `sota_mvp_normal`; it is **not** the same configuration as
  `hybrid_rerank`: both use the same hybrid/rerank retrieval core, but
  `sota_mvp_normal` keeps conditional Extended Search while `hybrid_rerank`
  disables it. In the completed 150-task run, both produced identical retrieval
  quality (`page_recall_at_10=0.896`, `chunk_recall_at_20=0.904`,
  `mrr_at_10=0.817`, `nDCG_at_10=0.787456`, 16 gold-miss tasks and zero
  execution errors); this equivalence is dataset/run evidence, not a contract
  alias. The retrieval settings are unchanged from the previous baseline, but
  the config hash moved from
  `b6508422a73bddd5ec4d3a669e4dad9fe63e9f89a1ec280333d0e8129b27041d` to
  `3c8ddf7024fa92da06f7f8257fc49593fc649112851d7d4fa276e873974f672c` because the
  answerability/evaluation and RunContract policies were versioned; this run
  is the new canonical baseline. Do not rerun it only to re-confirm equality.
  Rerun when the dataset or snapshot, index/run contract, retrieval
  profile/overrides, model aliases, or retrieval/evidence-control code changes.
  The authoritative report is
  `artifacts/eval/retrieval-reports/generated-wikipedia-v1-a01d97a88620-retrieval.md`.

- Deterministic Deep Research controller implementation on 2026-08-06:
  lifecycle state is split into `execution_state` and `outcome`; required,
  bridge and normal questions use a stable selector; duplicate evidence
  fingerprints consume per-question attempts; planner schema failures fall
  back to the immutable question; tool branches use the transient/permanent/
  security/controller-bug taxonomy; deadline terminalization builds a
  deterministic partial report. Unit/static checks passed after the final
  controller edits: `uv run ruff check ...` exit 0, `uv run ruff format
  --check ...` exit 0, targeted `uv run mypy ...` exit 0, and
  `uv run pytest tests/unit -q` exit 0 with 331 passed and 2 existing
  FastAPI deprecation warnings. The first focused post-change run exposed and
  fixed a PostgreSQL `ON CONFLICT` syntax error in evidence persistence; the
  next run reached Compose readiness and uploads but ended with
  `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED` before a terminal run report
  (`artifacts/validation/deep-research-hard-gate/20260806T153336Z/report.json`).
  A final focused run with an earlier report reserve produced a terminal
  detail and metrics `coverage_score=0.75`, `evidence_recall=1.0`,
  `unsupported_claim_count=0`, `acl_safety=true`, raw leak `false`, and no
  suite deadline exhaustion; `RB-17` and `Night Harbor` were
  `done/covered`. It still failed on a later bridge question after recording
  one attempt but before persisting its episode/tool call, so this is not an
  accepted hard-gate pass. The exact worker stack trace was lost on teardown;
  the error analysis is recorded in
  `docs/research/deep-research-controller-error-analysis-2026-08-06.md`.
  Report: `artifacts/validation/deep-research-hard-gate/20260806T161804Z/report.json`.
  The full hard matrix was not run.

- Deep Research planner/retrieval controller follow-up on 2026-08-05: bounded
  planner calls now classify timeout, empty content and malformed provider
  envelopes; recoverable planner failures use a deterministic safe fallback,
  the original research question is always retained in the tool query, new
  evidence rather than repeated evidence resets no-progress, and tool
  execution has a 180-second bounded timeout with safe code
  `research_tool_timeout`. Validation:
  `uv run ruff check src/wikipediarag/deep_research.py src/wikipediarag/retrieval_profile.py tests/unit/test_retrieval_profile.py`
  -> exit 0;
  `uv run mypy src/wikipediarag/deep_research.py src/wikipediarag/retrieval_profile.py`
  -> exit 0;
  `uv run pytest tests/unit/test_retrieval_profile.py tests/unit/test_deep_research.py tests/unit/test_answerability.py -q`
  -> exit 0, 48 passed.
  Focused real-provider hard runs reached terminal `completed_partial` with
  no planner/provider/ACL error and evidence recall `1.0`, but exited 1 at the
  evaluator because the first saturation window left expected questions open.
  Reports:
  `artifacts/validation/deep-research-hard-gate/20260805T195245Z/report.json`
  and `artifacts/validation/deep-research-hard-gate/20260805T201606Z/report.json`.
  The default `45%` context policy remains unchanged.

- Deep Research plan/product surface validation on 2026-08-05: persisted
  ResearchPlan CRUD/approve flow, run creation from approved plans, bounded
  original-query-plus-rewrites retrieval, explicit runtime stage reporting and
  UI type coverage are implemented without changing the default `45%` context
  policy. Validation:
  `uv run ruff check src/wikipediarag/schemas.py src/wikipediarag/db.py src/wikipediarag/repository.py src/wikipediarag/api/handlers.py src/wikipediarag/api/app.py src/wikipediarag/api/routers/research_plans.py src/wikipediarag/research_tools.py src/wikipediarag/deep_research.py tests/unit/test_deep_research.py`
  -> exit 0;
  `uv run pytest tests/unit/test_deep_research.py -q`
  -> exit 0, 32 passed;
  `pnpm typecheck` in `services/ui`
  -> exit 0.
  This pass validates the new contracts, storage/API surface and UI typings
  only; it does not replace the pending focused real-provider reruns for
  `within_doc_exception_clause` and `section_alias_owner_chain`.

- Deep Research bounded runtime rerun on 2026-08-05: the stabilized code path
  passed the isolated mock hard gate and still exposed a real-provider baseline
  blocker without raw payload leakage. Validation:
  `uv run python -m wikipediarag.cli deep-research-hard-gate --compose-model-provider mock --retrieval-profile upload_mock --max-tasks 1 --timeout-seconds 480`
  -> exit 0, `passed=true`; `alias_reformulation_chain` completed in isolated
  Compose with coverage score 1.0, evidence recall 1.0, 10 completed hashed
  tool calls, 7 derived questions, zero unsupported claims and ACL safety.
  Report:
  `artifacts/validation/deep-research-hard-gate/20260804T204840Z/report.json`.
  `uv run python -m wikipediarag.cli deep-research-hard-gate --task-id alias_reformulation_chain --max-tasks 1 --timeout-seconds 480`
  -> exit 1; isolated Compose, upload, auth and run creation all succeeded
  against OpenRouter, but the single real-provider fixture spent most of the
  shared deadline in `planner_failed` with safe code `planner_invalid_schema`,
  later reached one `episode_completed`, and still ended with
  `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED`. Report:
  `artifacts/validation/deep-research-hard-gate/20260805T042043Z/report.json`.
  The default `45%` context policy remains unchanged; the remaining blocker is
  real-provider planner structured-output conformance and/or latency under the
  current OpenRouter-backed Qwen alias, not isolated-stack startup. Proving
  model quality as the sole cause would require an A/B rerun against another
  model/provider alias, which is outside this documentation pass.

- Deep Research runtime stabilization validation on 2026-08-04: planner
  JSON/schema handling, runtime no-progress saturation, partial-terminal
  completion, OpenRouter gateway/provider retry semantics and hard-gate
  deadline reporting were hardened without changing the `45%` context default.
  Planner JSON recovery now extracts the outer JSON object span instead of
  accepting nested inner objects, document-tool arg rules are aligned between
  schema and validation, document-tool requests outside the current mode stop as
  `mode_insufficient_tools`, late planner/provider failures preserve an
  ACL-trimmed partial report with safe `stop_reason`/`error_code`, OpenRouter
  alias config can require structured-output parameters through Model Gateway,
  `Retry-After` is honored for transient `429/503` retries, and tool-matrix
  runs now write incremental `report.partial.json` snapshots. Validation:
  `uv run ruff check src/wikipediarag/deep_research.py src/wikipediarag/research_planner.py src/wikipediarag/model_client.py src/wikipediarag/gateway_app.py src/wikipediarag/cli.py src/wikipediarag/model_registry.py tests/unit/test_deep_research.py tests/unit/test_deep_research_eval.py tests/unit/test_gateway_app.py tests/unit/test_model_client_observability.py`
  -> exit 0;
  `uv run ruff format --check src/wikipediarag/deep_research.py src/wikipediarag/research_planner.py src/wikipediarag/model_client.py src/wikipediarag/gateway_app.py src/wikipediarag/cli.py src/wikipediarag/model_registry.py tests/unit/test_deep_research.py tests/unit/test_deep_research_eval.py tests/unit/test_gateway_app.py tests/unit/test_model_client_observability.py`
  -> exit 0, 10 files already formatted;
  `uv run mypy src/wikipediarag/research_planner.py src/wikipediarag/deep_research.py src/wikipediarag/model_client.py src/wikipediarag/gateway_app.py src/wikipediarag/cli.py src/wikipediarag/model_registry.py`
  -> exit 0;
  `uv run pytest tests/unit/test_deep_research.py tests/unit/test_deep_research_eval.py tests/unit/test_model_client_observability.py tests/unit/test_gateway_app.py tests/unit/test_retrieval_profile.py -q`
  -> exit 0, 85 passed, 2 FastAPI deprecation warnings.
  This pass validates the control-plane fixes only; no real-provider rerun has
  been accepted yet, so the default `45%` policy remains unchanged.

- Local-first SOTA Deep Research implementation validation on 2026-08-03:
  stage-aware context budgets, Model Gateway tokenizer exposure, Multi-KB
  Deep Research scope persistence, retrieve-multi tool dispatch, claim-relation
  detail surface and ACL-trimmed Multi-KB evidence filtering are now covered by
  deterministic tests. Validation:
  `uv run pytest tests/unit/test_deep_research.py tests/unit/test_auth_schema.py tests/unit/test_retrieval_profile.py tests/unit/test_gateway_app.py tests/unit/test_model_client_observability.py tests/unit/test_multi_kb_retrieval.py -q`
  -> exit 0, 54 passed, 2 FastAPI deprecation warnings;
  `uv run ruff check src/wikipediarag/api/handlers.py src/wikipediarag/deep_research.py src/wikipediarag/research_tools.py src/wikipediarag/repository.py src/wikipediarag/schemas.py tests/unit/test_deep_research.py tests/unit/test_auth_schema.py tests/unit/test_retrieval_profile.py tests/unit/test_gateway_app.py tests/unit/test_model_client_observability.py tests/unit/test_multi_kb_retrieval.py`
  -> exit 0;
  `uv run mypy src/wikipediarag/api/handlers.py src/wikipediarag/deep_research.py src/wikipediarag/research_tools.py src/wikipediarag/repository.py src/wikipediarag/schemas.py`
  -> exit 0.
  This pass validates the new contracts and storage/API surfaces only; it does
  not replace a fresh runtime hard-gate or local-model matrix.

- Documentation sync on 2026-08-03: active README and architecture documents
  now distinguish the demonstrated isolated mock alias-chain trajectory from
  the unpassed full OpenRouter/Qwen hard pack. The latter is a provider/runtime
  baseline failure, not evidence for changing planner logic or the 45% context
  default. Historical plans and status archive remain historical records.

- Deep Research isolated hard-gate implementation on 2026-08-02: added
  `compose.deep-research-gate.yaml` and a unique per-run Compose project with
  loopback-only API, MinIO and PostgreSQL endpoints. The CLI passes those
  endpoints through upload, auth and ACL-viewer setup, records only safe project
  metadata, stops isolated containers without deleting volumes, and retries
  Compose startup at most three times only for a detected port conflict. The
  hard fixtures are Markdown/CSV, so this profile omits Xberg, Docling and
  metadata-service while retaining API, worker, PostgreSQL, MinIO, OpenSearch
  and Model Gateway. `--skip-compose` retains the external API mode. The hard
  gate timeout is one post-readiness deadline shared by all fixtures and
  pause/resume/cancel actions, not a fresh 900-second timeout for each wait.
  Runtime preflight:
  `uv run python -m wikipediarag.cli deep-research-hard-gate
  --compose-model-provider mock --retrieval-profile upload_mock --max-tasks 1
  --timeout-seconds 480` -> exit 0, `passed=true`; isolated upload, ingestion,
  worker run and trajectory evaluation passed for `alias_reformulation_chain`
  with evidence recall 1.0, 9 completed hashed tool calls, 7 derived questions,
  zero unsupported claims and ACL safety. Report:
  `artifacts/validation/deep-research-hard-gate/20260802T201703Z/report.json`.
  The completed OpenRouter/Qwen default-45% baseline exited 1 after the shared
  900-second deadline: all four hard fixtures failed, two partly traversed the
  tool loop, ACL safety stayed true and unsupported claims stayed at zero, but
  coverage/recall were insufficient. Its isolated containers were removed and
  volumes retained. No 35% candidate run was made and the 45% default remains.
  A post-run regression fix makes research and ingestion terminal-timeout
  errors fixed safe messages/codes rather than serializing the last API payload
  into an artifact; this behavior is covered by deterministic tests.
- Deep Research hard runtime gate implementation validation on 2026-08-02:
  added `deep-research-hard-gate` with default fixture
  `tests/fixtures/deep_research/research_tasks_hard.json`, default profile
  `upload_sota_mvp`, default Compose provider `openrouter`, 900 second timeout,
  per-task detail artifacts under
  `artifacts/validation/deep-research-hard-gate/<timestamp>/`, OpenRouter key
  resolution from `OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_FILE` or `.env`
  through the shared Settings path before Compose startup, Qwen alias usage
  through Model Gateway only,
  trajectory metrics in `evaluate_research_detail` and hard fixture
  expectations for required derived terms/tool-call hashes/no raw query leaks.
  `upload_sota_mvp` now participates in real-provider readiness semantics, and
  Model Gateway uses the same resolver for startup smoke, `/v1/models` health
  and provider proxy calls without logging the secret.
  Validation:
  `uv run pytest tests/unit/test_deep_research.py tests/unit/test_deep_research_eval.py tests/unit/test_auth_schema.py tests/unit/test_gateway_app.py tests/unit/test_retrieval_profile.py`
  -> exit 0, 51 passed, 2 FastAPI deprecation warnings;
  `uv run ruff format --check .` -> initial exit 1, then
  `uv run ruff format src/wikipediarag/deep_research_eval.py tests/unit/test_deep_research_eval.py`
  -> exit 0 and repeat `uv run ruff format --check .` -> exit 0, 132 files
  already formatted;
  `uv run ruff check .` -> exit 0;
  `uv run mypy src tests` -> exit 0, no issues found in 130 source files;
  `uv run pytest tests/unit` -> exit 0, 281 passed, 2 FastAPI deprecation
  warnings;
  `uv run python -m wikipediarag.cli deep-research-hard-gate --help` -> exit 0.
  That implementation-validation pass did not run the OpenRouter hard gate
  because it starts Docker Compose and consumes provider quota; the later
  isolated baseline is recorded above. The safe resolver check found the key
  source as `settings:OPENROUTER_API_KEY` without printing the key value, which
  verifies the previous false-negative `os.environ`-only path is closed.
- Deep Research planner/tool-loop V1 implementation validation on 2026-08-02:
  implemented strict planner schemas and deterministic fallback, public-safe
  `research_tool_calls`, durable derived-question append with lineage, bounded
  episode planning around `extended_search`, verified claim persistence,
  contradiction-first repair question creation and API/CLI context-policy
  override support. Added `make deep-research-hard-smoke`.
  Validation:
  `uv run ruff format --check .` -> exit 0, 132 files already formatted;
  `uv run ruff check .` -> exit 0;
  `uv run mypy src tests` -> exit 0, no issues found in 130 source files;
  `uv run pytest tests/unit` -> exit 0, 273 passed, 2 FastAPI deprecation
  warnings.
- Deep Research hard fixture/tool-loop target validation on 2026-08-02:
  Added `tests/fixtures/deep_research/research_tasks_hard.json` with four
  hard multi-source targets that require alias resolution and evidence-driven
  query reformulation; added unit coverage for the
  hard manifest, clarified the implemented V1 tool/reformulation boundary in
  architecture docs and added `docs/exec-plans/36-deep-research-tool-loop.md`.
  Focused validation:
  `uv run ruff check src\wikipediarag\deep_research.py tests\unit\test_deep_research.py tests\unit\test_deep_research_eval.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\deep_research.py tests\unit\test_deep_research.py tests\unit\test_deep_research_eval.py`
  -> exit 0, 3 files already formatted;
  `uv run mypy src\wikipediarag\deep_research.py src\wikipediarag\deep_research_eval.py tests\unit\test_deep_research.py tests\unit\test_deep_research_eval.py`
  -> exit 0, no issues found in 4 source files;
  `uv run pytest tests\unit\test_deep_research.py tests\unit\test_deep_research_eval.py -q`
  -> exit 0, 21 passed;
  `uv run python -m wikipediarag.cli deep-research-matrix --fixture-path tests/fixtures/deep_research/research_tasks_hard.json`
  -> exit 0, passed=true, 4 fixtures, 27 policy aggregates / 108
  fixture-policy rows, report:
  `artifacts/validation/deep-research-matrix/20260802T164751Z/report.json`.
  This hard matrix is still offline packer validation and has been superseded
  by the planner/tool-loop V1 implementation validation above; runtime hard
  smoke still needs to be run.
- Deep Research runtime smoke and policy matrix validation on 2026-08-02:
  `make` was unavailable in the Windows shell, so the Makefile-equivalent `uv`
  commands were run directly. `uv run python -m wikipediarag.cli
  deep-research-smoke` -> exit 0, passed=true, 10/10 fixtures passed with
  compose rebuild/start, API readiness, platform-admin login, seeded viewer,
  upload-backed ingestion, ACL trimming and pause/resume/cancel lifecycle.
  Runtime report:
  `artifacts/validation/deep-research/20260802T152743Z/report.json`.
  The smoke exposed and fixed four harness/runtime issues during the run:
  host-side DB seeding now rewrites compose hostname `postgres` to `localhost`
  when needed, tenant-membership seeding no longer assumes an `updated_at`
  column, smoke auth refreshes CSRF after `/auth/session`, and
  `create_research_run` now inserts the `ingestion_jobs` row before the
  `research_runs.active_job_id` FK reference. It also exposed a pre-claim
  lifecycle bug: pause/cancel now marks received Deep Research jobs cancelled
  and moves the run to `paused`/`cancelled` instead of leaving
  `received/cancel_requested=true`. `uv run python -m wikipediarag.cli
  deep-research-matrix` -> exit 0, passed=true, 27 policy aggregates and 270
  fixture-policy rows passed; offline recommended policy:
  `target_35_abstracts_only_none`; report:
  `artifacts/validation/deep-research-matrix/20260802T153246Z/report.json`.
  This recommendation is offline/synthetic, so the runtime 45% default remains
  unchanged until measured through runtime policy override or local-Qwen
  validation. Final stable checks after fixes:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 128 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 128 source files;
  `uv run pytest tests\unit -q` -> exit 0, 266 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
- Deep Research testing harness validation on 2026-08-02:
  Added `tests/fixtures/deep_research/research_tasks.json` with 10 complex
  synthetic tasks, `wikipediarag.deep_research_eval` fixture validation and
  offline scoring, `deep-research-smoke` CLI/Make target with report/JUnit
  artifact output and architecture/operation docs. Deterministic validation:
  `uv run ruff format src tests` -> exit 0, 128 files left unchanged;
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 128 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 128 source files;
  `uv run pytest tests\unit -q` -> exit 0, 260 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `uv run python -m wikipediarag.cli deep-research-smoke --help` -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
  Runtime `make deep-research-smoke` was not run in this deterministic pass.
- Durable Deep Research V1 validation on 2026-08-02:
  Added durable single-KB research lifecycle tables, repository operations,
  `POST/GET /api/v1/research-runs`, detail/events and pause/resume/cancel
  endpoints, worker execution via `ingestion_jobs.kind='deep_research'`,
  context budget/packing policy, ACL-trimmed report rebuild, minimal React UI
  panel and architecture/status docs. Validation:
  `uv run ruff format src tests` -> exit 0, 126 files left unchanged;
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 126 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 126 source files;
  `uv run pytest tests\unit -q` -> exit 0, 253 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `pnpm lint` in `services/ui` -> exit 0;
  `pnpm typecheck` in `services/ui` -> exit 0;
  `pnpm build` in `services/ui` -> exit 0;
  `pnpm format:check` in `services/ui` -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
- Search Quality V2 ACL/security trimming validation on 2026-08-02:
  Extended trusted `metadata.document_access` trimming to `kb`, `tenant` and
  `restricted` policies with admin/manager bypass across public search,
  chat/direct retrieval, Multi-KB retrieval, Extended Search neighbor
  expansion, debug search and document viewer paths. KB managers can update
  document access and source document access defaults through API/UI; source
  defaults can be applied to existing synced documents and inherited by future
  source syncs. Direct client upload metadata is normalized back to KB-visible
  access unless it comes from trusted source sync. Validation:
  `uv run ruff format src tests` -> exit 0, 7 files reformatted;
  `uv run ruff format --check src tests` -> exit 0, 123 files already
  formatted;
  `uv run ruff check .` -> exit 0;
  `uv run mypy src tests` -> exit 0, no issues found in 123 source files;
  `uv run pytest tests\unit -q` -> exit 0, 245 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `pnpm lint` in `services/ui` -> exit 0;
  `pnpm typecheck` in `services/ui` -> exit 0;
  `pnpm build` in `services/ui` -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `git diff --check` -> exit 0, with Git CRLF normalization warnings only.
- Backend search and Deep Research documentation validation on 2026-08-02:
  Added `docs/architecture/search-and-deep-research.md` as the canonical
  backend/product contract for ordinary search, debug search, chat retrieval,
  current single-KB Extended Search and planned durable Deep Research.
  Documentation indexes were updated in `README.md` and
  `docs/architecture.md`. Claims were checked against
  `src/wikipediarag/search_service.py`, `src/wikipediarag/retrieval.py`,
  `src/wikipediarag/extended.py`, `src/wikipediarag/api/handlers.py`,
  `src/wikipediarag/retrieval_contract.py`, `src/wikipediarag/schemas.py` and
  `config/retrieval.yaml`. Validation:
  `rg -n "search-and-deep-research|Detailed Architecture|Documentation Map" README.md docs`
  -> exit 0.
- Backend API refactoring validation on 2026-08-02:
  API routes were split into domain `APIRouter` modules under
  `src/wikipediarag/api/`, while `wikipediarag.api_app:app` remains the public
  service entrypoint. Chat and debug-search orchestration now have explicit
  service entry functions, and endpoint/large-helper docstrings were added
  without changing public routes, schemas, auth/CSRF checks, SSE event names or
  retrieval behavior. Validation:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 122 files already
  formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 122 source files;
  `uv run pytest tests\unit -q` -> exit 0, 229 passed, 2 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `docker compose config --quiet` -> exit 0.
- Search/deep-research runtime validation on 2026-08-02:
  `docker compose up -d --build api worker ui` -> exit 0;
  authenticated API smoke created KB `82d4f568-cb38-408b-bcea-dfb3f7e9ff2a`,
  uploaded document `doc:04ce3e7844c7fb96b1469765`, observed ingestion job
  `8382d53e-18d5-4e05-80fa-02373c2666b9` complete with
  `chunks_published=1`, verified `POST /api/v1/search` without explicit
  `ranking_profile` returned HTTP 200 with 1 result, facets, groups,
  highlights, `chunk_id`, source metadata and score/ranks, verified document
  structure/context APIs, then verified extended chat completed with query run
  `b1e18d2d-52ea-4c59-a678-850567813acc` and retrieval events including
  extended harness stages. Artifact:
  `artifacts/validation/runtime-smokes/20260802T085033Z-search-v2-deep-research/report.json`.
  The runtime check exposed that upload search needs a compatible default
  profile when clients omit `ranking_profile`; public search now infers
  `upload_sota_mvp`/`upload_mock` from active upload index metadata for
  homogeneous upload scopes. Browser connector validation against
  `http://localhost:5173` with local auth `admin` selected the same KB,
  narrowed retrieval scope to that KB, searched
  `SEARCH_V2_DEEP_RESEARCH_MARKER_20260802T085033Z`, observed the result with
  facets, document grouping, highlight text and `score 0.929`, opened it in the
  document viewer, and ran Chat in `Extended` mode with `upload_sota_mvp`;
  the UI produced an answer with citation `[S1]`, the marker evidence and an
  enabled Debug action. Final readiness checks:
  `Invoke-RestMethod http://localhost:8000/ready` -> exit 0, status ok;
  `Invoke-RestMethod http://localhost:8081/ready` -> exit 0, status ok.
  Post-fix stable checks: `uv run pytest
  tests\unit\test_search_endpoint.py tests\unit\test_search_service.py -q`
  -> exit 0, 8 passed, 2 warnings; `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0;
  `uv run mypy src tests` -> exit 0, no issues found in 102 source files;
  `uv run pytest tests\unit -q` -> exit 0, 228 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `cd services/ui; pnpm lint` -> exit 0; `cd services/ui; pnpm typecheck`
  -> exit 0; `cd services/ui; pnpm build` -> exit 0;
  `cd services/ui; pnpm format:check` -> exit 0;
  `docker compose config --quiet` -> exit 0.
- Search Quality V2 foundation validation on 2026-08-02:
  `uv run pytest tests\unit\test_search_endpoint.py tests\unit\test_search_service.py -q`
  -> exit 0, 7 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\retrieval.py src\wikipediarag\search_service.py src\wikipediarag\api_app.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py`
  -> exit 0;
  `uv run mypy src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\retrieval.py src\wikipediarag\search_service.py src\wikipediarag\api_app.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py`
  -> exit 0, no issues found in 7 source files;
  `uv run ruff format --check src\wikipediarag\schemas.py src\wikipediarag\search_index.py src\wikipediarag\retrieval.py src\wikipediarag\search_service.py src\wikipediarag\api_app.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py`
  -> exit 0, 7 files already formatted;
  `uv run pytest tests\unit\test_retrieval_answering.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_search_endpoint.py tests\unit\test_search_service.py -q`
  -> exit 0, 29 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 227 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed;
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0, 102 files already formatted;
  `uv run mypy src tests` -> exit 0, no issues found in 102 source files;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0;
  `cd services/ui; pnpm format:check` -> exit 0;
  `docker compose config --quiet` -> exit 0.
- Stage 4 source UI validation on 2026-08-02:
  `cd services/ui; pnpm exec prettier --write src/App.tsx src/styles.css`
  -> exit 0;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0;
  `cd services/ui; pnpm format:check` -> exit 0;
  `uv run pytest tests\unit\test_external_sources.py -q -k "local_folder_connector_reports_changes_and_full_sync_tombstones or corporate_mock_connectors_freeze_contracts"`
  -> exit 0, 2 passed, 5 deselected, 2 warnings;
  Vite dev server was started with `pnpm exec vite --host 127.0.0.1 --port 5174`
  and `Invoke-WebRequest http://127.0.0.1:5174` -> exit 0, HTTP 200.
- Stage 4 external source foundations validation on 2026-08-02:
  `uv run ruff check src\wikipediarag\source_connectors.py src\wikipediarag\api_app.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\search_index.py src\wikipediarag\worker.py src\wikipediarag\schemas.py tests\unit\test_external_sources.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\source_connectors.py src\wikipediarag\api_app.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\search_index.py src\wikipediarag\worker.py src\wikipediarag\schemas.py tests\unit\test_external_sources.py`
  -> exit 0, 8 files already formatted;
  `uv run mypy src\wikipediarag\source_connectors.py src\wikipediarag\api_app.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\search_index.py src\wikipediarag\worker.py src\wikipediarag\schemas.py tests\unit\test_external_sources.py`
  -> exit 0, no issues found in 8 source files;
  `uv run pytest tests\unit\test_external_sources.py tests\unit\test_upload_batches.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py tests\unit\test_document_ingestion.py -q`
  -> exit 0, 35 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 224 passed, 4 warnings;
  `docker compose config --quiet` -> exit 0.
- Stage 3 document navigation implementation validation on 2026-08-01:
  `uv run pytest tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 33 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\db.py src\wikipediarag\document_ingestion.py src\wikipediarag\wiki_dump.py src\wikipediarag\zim_dump.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\schemas.py src\wikipediarag\api_app.py tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\db.py src\wikipediarag\document_ingestion.py src\wikipediarag\wiki_dump.py src\wikipediarag\zim_dump.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\schemas.py src\wikipediarag\api_app.py tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py`
  -> exit 0, 11 files already formatted;
  `uv run mypy src\wikipediarag\document_ingestion.py src\wikipediarag\wiki_dump.py src\wikipediarag\zim_dump.py src\wikipediarag\repository.py src\wikipediarag\ingestion.py src\wikipediarag\schemas.py src\wikipediarag\api_app.py tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py`
  -> exit 0, no issues found in 10 source files;
  `docker compose config --quiet` -> exit 0;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Stage 3 browser/MinIO upload smoke on 2026-08-01:
  Browser connector drove the React UI at `http://localhost:5173` with local
  auth `admin`, created KB `f763e161-9392-44f9-b0f4-c1d0cbc431f7`, uploaded
  `artifacts/validation/runtime-smokes/stage3-browser-upload-fixture-retry.md`
  through the UI file chooser and MinIO-backed upload path, and observed batch
  completion with `1 completed`, `0 failed`, `1 published`,
  parser `local_text_adapter`.
  Ordinary search for
  `STAGE3_BROWSER_MINIO_MARKER_20260801_232237` returned the uploaded document
  with `Open in viewer`; opening the result loaded `.document-viewer` with one
  `.document-chunk.highlighted`, marker text and locator chips
  `page: 1` / `block_index: 1`. In-document search returned the same hit, hit
  click reloaded the highlighted chunk, and TOC click loaded section context.
  The smoke initially exposed asyncpg `AmbiguousParameterError` on nullable
  `document_version_id` predicates in section replacement and viewer reads;
  fixed by explicit `CAST(:document_version_id AS text)` in the affected
  repository SQL.
  Follow-up checks:
  `uv run pytest tests\unit\test_document_viewer.py -q` -> exit 0, 6 passed,
  2 warnings;
  `uv run pytest tests\unit\test_document_ingestion.py tests\unit\test_search_endpoint.py tests\unit\test_document_viewer.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 34 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\repository.py tests\unit\test_document_viewer.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\repository.py tests\unit\test_document_viewer.py`
  -> exit 0, 2 files already formatted;
  `uv run mypy src\wikipediarag\repository.py tests\unit\test_document_viewer.py`
  -> exit 0, no issues found in 2 source files;
  `docker compose up -d --build api worker` -> exit 0;
  final `Invoke-RestMethod http://localhost:8000/ready` -> exit 0, status ok
  for PostgreSQL and Model Gateway.
- Stage 2 ordinary search implementation validation on 2026-08-01:
  `uv run pytest tests\unit\test_auth_service.py tests\unit\test_document_deletion_lifecycle.py tests\unit\test_cli_cross_tenant_hardening.py tests\unit\test_search_endpoint.py tests\unit\test_api_auth_disabled.py tests\unit\test_multi_kb_retrieval.py -q`
  -> exit 0, 40 passed, 2 warnings;
  `uv run pytest tests\unit\test_document_deletion_lifecycle.py tests\unit\test_upload_batches.py tests\unit\test_acl_mirroring_metadata.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 26 passed, 2 warnings;
  targeted `uv run mypy` for the changed backend/test files -> exit 0;
  `uv run ruff check` for changed backend/test files -> exit 0;
  `uv run ruff format --check` for changed backend/test files -> exit 0;
  `docker compose config --quiet` -> exit 0;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
  Full `uv run mypy src tests` was also run at this point and exposed
  test-only typing issues that were closed in the following validation entry:
  `tests\unit\test_retrieval_answering.py:148-149` and
  `tests\unit\test_extended.py:55`.
  Runtime: `docker compose up -d --build` -> exit 0;
  `Invoke-RestMethod -Uri http://localhost:8000/ready` -> exit 0 after
  startup, status ok for PostgreSQL and Model Gateway;
  `uv run python -m wikipediarag.cli verify-document-upload --skip-compose`
  -> exit 0, passed=true, report
  `artifacts/validation/document-upload/20260801T181338Z`;
  `uv run python -m wikipediarag.cli verify-cross-tenant-hardening --skip-compose`
  -> exit 0, passed=true, report
  `artifacts/validation/cross-tenant-hardening/20260801T181428Z`.
  Ordinary search runtime smoke used local auth + CSRF and KB
  `eff3437d-f000-45d1-88fb-3a1d78f8ab67`: filtered
  `POST /api/v1/search` for `verify-metadata` returned HTTP 200 with 1 result,
  source metadata and locator, artifact
  `artifacts/validation/runtime-smokes/20260801T212051Z/ordinary-search-title-filtered.json`.
  Document deletion runtime smoke on `doc:a43385f99cb1eb1a043244a8` returned
  lifecycle `deleting`, created deferred purge job
  `cd045ccc-a27b-42ac-861e-44c3f6f5cf55`, and the deleted document was absent
  from ordinary search after delete; artifacts
  `artifacts/validation/runtime-smokes/20260801T212106Z/document-delete.json`,
  `artifacts/validation/runtime-smokes/20260801T212106Z/ordinary-search-after-delete.json`
  and
  `artifacts/validation/runtime-smokes/20260801T212117Z/document-purge-job.json`.
- Full backend typecheck cleanup on 2026-08-01:
  fixed the remaining test-only typing issues in
  `tests\unit\test_retrieval_answering.py` and
  `tests\unit\test_extended.py`;
  `uv run mypy src tests` -> exit 0, no issues found in 97 source files;
  `uv run pytest tests\unit\test_retrieval_answering.py tests\unit\test_extended.py -q`
  -> exit 0, 23 passed;
  `uv run ruff check tests\unit\test_retrieval_answering.py tests\unit\test_extended.py`
  -> exit 0;
  `uv run ruff format --check tests\unit\test_retrieval_answering.py tests\unit\test_extended.py`
  -> exit 0, 2 files already formatted.
- Stage 1 Docker runtime validation on 2026-08-01:
  `uv run pytest tests\unit\test_document_deletion_lifecycle.py tests\unit\test_upload_batches.py tests\unit\test_acl_mirroring_metadata.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_cli_cross_tenant_hardening.py -q`
  -> exit 0, 22 passed, 2 warnings;
  `docker compose config --quiet` -> exit 0;
  `docker compose up -d --build` -> exit 0;
  `Invoke-RestMethod -Uri http://localhost:8000/ready` -> exit 0 after
  startup, status ok for PostgreSQL and Model Gateway.
  Runtime artifacts were written under
  `artifacts/validation/runtime-smokes/20260801T172131Z/` and remain ignored.
  Authenticated parser batch upload smoke passed in the custom runtime harness:
  metadata-service, Xberg and Docling parser checks passed; two KBs were
  created (`f86cf74e-0120-4a38-91aa-fa935d6e6e2b`,
  `9b7fa803-94a1-4fbf-a4a2-0a5cca5d7947`); batch uploads completed with
  parser routes `docling` and `local_csv_adapter`; artifact
  `stage-1-runtime-smokes.json`.
  Official `uv run python -m wikipediarag.cli verify-document-upload --skip-compose`
  is blocked in the default auth environment: exit 1, report
  `artifacts/validation/document-upload/20260801T171726Z`, safe reason HTTP 401
  on `GET /api/v1/knowledge-bases`. Re-running with `AUTH_DISABLED=true`
  reached writes but failed with exit 1, report
  `artifacts/validation/document-upload/20260801T171823Z`, safe reason
  audit insert rejected non-UUID `auth-disabled:*` session id.
  Document deletion API smoke returned HTTP 202 for document
  `doc:f32a21fccea781d693ec4e1a`, purge job
  `f8b5b4d2-0424-417a-b195-9295549c99bc`; deleted document was absent from
  later retrieval, artifact `document-post-delete-retrieval.json`; deferred
  purge is blocked because the job ended `failed` with safe error code
  `ClientError`, artifact `document-deletion-purge.json`.
  OIDC group-grant runtime smoke passed with Keycloak profile and host-header
  callback flow: KB `269ebb8c-d027-489b-9f07-898bb3287b5b`, grant
  `c1ae7246-dbd4-4ab6-8409-3c84fc8b1ede`, OIDC read before revoke HTTP 200,
  read after revoke HTTP 403, no reindex; artifact
  `oidc-group-grant-access-existing-group-correct-password-v5.json`.
  Multi-KB retrieval passed against two ready KBs with compatible
  `upload_sota_mvp` profile and dedup disabled for evidence coverage:
  `/api/v1/search:debug` -> HTTP 200, `query_run_id`
  `2bb67226-2019-4caa-8a5c-d854d89b85d6`, both KB IDs present in final
  evidence and events; artifact
  `multi-kb-final-evidence-1449761485479121206.json`.
  `uv run python -m wikipediarag.cli verify-cross-tenant-hardening --skip-compose`
  is blocked: exit 1, report
  `artifacts/validation/cross-tenant-hardening/20260801T173951Z/cross-tenant-hardening-report.json`;
  API ready, platform-admin login and two tenant creation passed, then
  `/api/v1/search:debug` returned HTTP 409 `KB_NOT_READY` because profile
  embedding alias `mock_embed_default` did not match index alias `embed_default`;
  captured body artifact
  `artifacts/validation/cross-tenant-hardening/20260801T173951Z/search-debug-409-body.json`.
  API and worker were restored to the default local Docker profile afterwards;
  final `/ready` -> exit 0.
- Explicit transform/subquery retrieval observability validation on 2026-08-01:
  `uv run ruff check src\wikipediarag\retrieval.py src\wikipediarag\extended.py src\wikipediarag\api_app.py tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_api_readiness.py tests\unit\test_multi_kb_retrieval.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\retrieval.py src\wikipediarag\extended.py src\wikipediarag\api_app.py tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_api_readiness.py tests\unit\test_multi_kb_retrieval.py`
  -> exit 0, 7 files already formatted;
  `uv run pytest tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_model_client_observability.py -q`
  -> exit 0, 36 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 201 passed, 4 warnings;
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Retrieval observability Docker runtime smoke on 2026-08-01:
  `docker compose up -d --build api model-gateway` -> exit 0;
  `Invoke-RestMethod -Uri http://localhost:8000/ready` -> exit 0,
  status ok for PostgreSQL and Model Gateway;
  authenticated `/api/v1/search:debug` direct runs for simple fact, comparison
  and explicit-negative-title questions -> HTTP 200 with persisted
  `query_run_id`s `d9ba16e6-519c-4c6d-94ac-117ccf7c44a4`,
  `a8f86877-56a9-42b0-82d9-30f5a6f0f3b3` and
  `ed8f3f67-5dfe-41d6-82b7-c113563c2797`;
  authenticated `/api/v1/chat` normal and extended synthetic unanswered queries
  -> HTTP 200 with persisted `query_run_id`s
  `5d4f4536-0cc7-4b06-b805-696260f182a2` and
  `399896ff-9561-4819-ba97-e2e5bc17076e`;
  incompatible Wikipedia+upload Multi-KB debug request -> HTTP 409
  `KB_NOT_READY` with safe reason `active index source is incompatible with
  retrieval profile`;
  compatible upload+upload Multi-KB debug request with `upload_mock` profile
  -> HTTP 200, `query_run_id`
  `103fb6de-18f1-48bf-b275-14497e1c7aa3`, both KB IDs present in run scope and
  candidate events;
  after fixing candidate-stage snapshots, repeated compatible upload+upload
  Multi-KB debug request -> HTTP 200, `query_run_id`
  `0c5e1b11-5be8-4695-91aa-4667b34efb1a`, PostgreSQL `rrf` payload contains
  BM25/dense/fusion ranks only and `rerank` appears only in the following
  `rerank` payload;
  protected feedback/evaluation append endpoints -> HTTP 200;
  unauthenticated query-run retrieval read -> HTTP 401;
  controlled Model Gateway outage chat -> SSE `run.failed`, persisted
  `query_run_id` `373cac08-e266-4473-a537-03c311ee3842` with
  `query_runs.status=failed`, `error_code=ModelGatewayError` and an
  `answer_generation` failure event carrying safe model metadata
  `safe_error_code=provider_network_error`, `attempts=3`, `retries=2` and no
  prompt/provider payload fields.
  PostgreSQL row counts after the smoke: `query_runs=626`,
  `retrieval_events=102914`.
- Candidate-stage snapshot regression check after runtime smoke:
  `uv run pytest tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py tests\unit\test_multi_kb_retrieval.py -q`
  -> exit 0, 25 passed, 2 warnings;
  `uv run ruff check src\wikipediarag\api_app.py src\wikipediarag\retrieval.py tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py`
  -> exit 0;
  `uv run ruff format --check src\wikipediarag\api_app.py src\wikipediarag\retrieval.py tests\unit\test_api_readiness.py tests\unit\test_retrieval_answering.py`
  -> exit 0.
- Retrieval observability V1 backend/UI validation:
  `uv run pytest tests\unit\test_retrieval_answering.py tests\unit\test_extended.py tests\unit\test_multi_kb_retrieval.py tests\unit\test_model_client_observability.py -q`
  -> exit 0, 29 passed, 2 warnings;
  `uv run pytest tests\unit -q` -> exit 0, 197 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed.
- Retrieval observability V1 static checks:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0;
  `uv run mypy src tests` -> exit 0.
- Retrieval observability V1 UI checks:
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Unit and focused quality checks after public batch upload and Multi-KB retrieval:
  `uv run pytest tests\unit\test_upload_batches.py tests\unit\test_multi_kb_retrieval.py -q`
  -> exit 0, 5 passed, 2 warnings.
- Full deterministic backend validation after the same increment:
  `uv run pytest tests\unit -q` -> exit 0, 191 passed, 4 warnings;
  `uv run pytest tests\integration -q` -> exit 0, 14 passed.
- Static checks after the same increment:
  `uv run ruff check .` -> exit 0;
  `uv run ruff format --check src tests` -> exit 0;
  `uv run mypy src tests` -> exit 0.
- UI validation after the same increment:
  `cd services/ui; pnpm lint` -> exit 0;
  `cd services/ui; pnpm typecheck` -> exit 0;
  `cd services/ui; pnpm build` -> exit 0.
- Compose validation after parser sandboxing/autoscaling:
  `docker compose config --quiet` -> exit 0.
- Latest provider-backed reviewed Wikipedia gate:
  `uv run python -m wikipediarag.cli eval-release-gate --suite reviewed-wikipedia-smoke-v1 --api http://localhost:8000`
  -> exit 0, passed=true, blocking_failures=0.
- Latest gate report:
  `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260730T195822Z-reviewed-wikipedia-smoke-v1-release-gate-5b04e45f/report.json`.
- Document corpus verification retained from the document ingestion implementation:
  standard fixture set -> exit 0, report `artifacts/validation/document-corpus/20260729T205604Z`;
  full fixture set -> exit 0, report `artifacts/validation/document-corpus/20260729T205923Z`;
  smoke external set -> exit 0, report `artifacts/validation/document-corpus/20260729T210847Z`.
- Document upload verification retained from the document ingestion implementation:
  `uv run python -m wikipediarag.cli verify-document-upload --skip-compose`
  -> exit 0, report `artifacts/validation/document-upload/20260729T210047Z`.

## Validation after the 2026-08-09 critical Retrieval/Deep Research increment

- `uv run pytest tests/unit -q`: 362 passed, 2 warnings.
- `uv run pytest tests/integration -q`: 14 passed, 1 skipped.
- `uv run ruff check .`, `uv run ruff format --check src tests`, `uv run mypy src tests`: passed.
- `cd services/ui && pnpm lint && pnpm typecheck && pnpm build && pnpm format:check`: passed. The DOCX exporter is dynamically loaded; initial JS remains about 282 kB and the exporter is a separate chunk.
- `docker compose up -d --build api worker`: passed; `/ready` returned `{"status":"ok"}`. Migration `002_research_evidence_refs_and_job_leases` is applied, null evidence refs = 0, expired leases = 0.
- Live mock smoke gates remain red/blocked: `deep-research-smoke` with `upload_mock` reports active index alias mismatch (`mock_embed_default` vs `embed_default`); `upload_sota_mvp` and hard gate with a 30/60 second gate deadline outlive the CLI deadline but later terminalize. Tool matrix was started with visible command output and stopped after no progress output; no pass is claimed.
- Follow-up hardening remains: SSE currently emits stage start/completion events but not a periodic heartbeat while a stage is executing; facets still need a dedicated lexical aggregation path (the Redis window cache is in place, but `_facets` remains a retrieval-window fallback); multi-KB transformed retrieval still shares the existing per-KB first-stage path rather than a single cross-KB embedding batch.

## Active blockers and risks

- 2026-08-10 live correction follow-up: isolated `upload_mock` Playwright Ask / source / Debug acceptance passed (`1 passed`, 13.8s). The helper now uses one selected KB, collapses the long scope list before Chat interaction, and tears down bounded test-owned upload KBs including normal Chat child records. Older MinIO `MissingContentMD5` batch delete falls back to single-object deletion. Research mock acceptance and the full three-spec E2E set remain unrun after this cleanup change.
- Provider-backed smoke is passing for a bounded Russian-Wikipedia Ask and a short Deep Research run. The Ask canary now uses `RETRIEVAL_PROFILE=sota_mvp`, structured reasoning defaults are disabled, nested FastAPI Gateway errors are unwrapped, and the production answer budget is 4096 tokens. A live Ask for `Что такое Россия?` completed with terminal `run.completed`, answer text and citations on 2026-08-11. A short provider-backed Research run also reached `completed` with 15 evidence rows, 14 claims and a final report. The final Ask repeat after tightening the insufficient-evidence prompt also reached `run.completed`. The full Research/upload E2E matrix remains outstanding.

- **Russian-Wikipedia retrieval relevance blocker (2026-08-11):** live Browser
  Ask and Deep Research runs terminalize correctly, but the current Russian
  Wikipedia index returns unrelated asteroid, domain-name and short-article
  fragments for broad questions such as `Что такое Россия?`. Ask therefore
  produces a technically valid answer with citations that are semantically
  unreliable, while Deep Research reaches `completed · quality_gate` with
  `partial` coverage and no confirmed findings. Do not treat these runs as a
  semantic-quality pass. Resolution requires rechecking the Russian ZIM/index
  contents, document-to-chunk normalization and retrieval relevance/rerank
  filters; `ZIM_NOT_FOUND` remains a separate deployment prerequisite.

- Multi-KB direct retrieval is implemented; Multi-KB Extended Search remains future work.
- Offline Deep Research matrix results are not yet a provider/runtime policy
  benchmark. The current 45% productive-target default should not be changed
  solely from the synthetic packer matrix.
- API `/ready` checks PostgreSQL, worker heartbeat, Model Gateway, OpenSearch,
  MinIO and required parser services; Redis/Valkey remains a Compose dependency
  without a readiness check.
- Redis/Valkey is used for tenant-scoped public-search windows with uncached
  fallback; current ingestion job claiming still uses PostgreSQL `FOR UPDATE
  SKIP LOCKED`, and Redis is not durable or readiness-fatal.
- Malware scanning, external ACL connector import policy, restore drills, production TLS/secrets, observability retention and resource sizing remain production hardening work.
- OpenTelemetry collector remains debug-exporter only; optional OpenSearch observability data-stream projection is not implemented.
- OpenRouter-backed gates still depend on provider quota, credits, latency and model behavior.

## Next approved task

### Model Gateway structured canary correction (2026-08-11)

The startup canary retains the full production `grounded_answer` schema and
`reasoning={"effort":"none"}`, but its isolated structured-output budget is
now 256 tokens (the production answer budget is unchanged). Its safe readiness
reason distinguishes `structured_output_truncated`,
`structured_schema_invalid`, and `structured_output_limit_exceeded` without
recording provider response bodies, prompts, credentials, or document content.
The affected alias remains `MODEL_ALIAS_UNREADY`. Gateway unit tests, Ruff,
format, and Mypy pass. The Gateway/API Compose rebuild preserved volumes;
both `/ready` endpoints returned `ok`, and the real `smoke-models --provider
openrouter` passed embeddings (1024 dimensions), structured chat and rerank.

A new `rrncb-public-v2` suite was then prepared with the pinned 200-task
dataset hash and all 65 PDFs. Its fresh ingestion run
`rrncb-public-v2-20260811t224700z` stopped at the existing 900-second document
timeout in batch 1 while the API reported its worker stale. No `dev` task and
no `test` task were submitted, no timeout/retry policy was weakened, and the
historic `rrncb-public-v1` artifacts were not modified. A completed ingestion
and healthy API readiness remain mandatory before the live `dev` baseline can
resume.

### RRNCB baseline runner preparation (2026-08-11)

The immutable baseline runner now defaults to a new `rrncb-public-v2` suite and
supports `eval-document-run --split dev|test`. A baseline is strict
fail-fast with sequential task execution: it writes a safe partial report and
stops on any failed task, missing terminal SSE event, `MODEL_OUTPUT_INVALID`,
readiness, preflight or run-contract failure. `test` requires
`--resume-run-id` for a successful completed 40-task `dev` run; the stable run
contract pins dataset/PDF hashes, KB, retrieval profile, model aliases and
index contract IDs. The report now records the requested split and selected
task outcome, while the final test phase retains dev/test/all metrics.

Deterministic validation passed: RRNCB unit tests (7), focused Ruff and Mypy.
The local `RRNCB/` directory contains 65 PDFs, but the current API `/ready` is
`degraded` because Model Gateway is failed, so no live `dev` run was started.

After the Model Gateway provider canary is healthy, run the new immutable RRNCB
baseline: fresh run ID, 40 dev tasks first, then 160 test tasks, with the same
KB/PDF hashes and no automatic repair calls. Do not resume or modify
`rrncb-public-v1`; stop on any readiness, contract, terminal-event or
`MODEL_OUTPUT_INVALID` gate failure. Deep Research tuning remains a separate
follow-up and must not change this regression configuration.

## Related artifacts

- Architecture overview: [architecture.md](architecture.md).
- Focused architecture docs: [architecture/](architecture/).
- Agent instructions: [../AGENTS.md](../AGENTS.md).
- Historical implementation plans: [exec-plans/](exec-plans/).
- Historical status archive: [history/STATUS-archive.md](history/STATUS-archive.md).
- Latest reviewed gate report:
  `artifacts/eval/release-gates/reviewed-wikipedia-smoke-v1/20260730T195822Z-reviewed-wikipedia-smoke-v1-release-gate-5b04e45f/report.json`.

### Provider-backed Ask follow-up (2026-08-11)

The full Research/upload E2E matrix remains unrun. A provider-backed Russian-Wikipedia Ask was repeated after the Gateway/API rebuild: retrieval and extended search completed, answer generation initially exposed the true `MODEL_OUTPUT_TRUNCATED` contract failure (the 768-token answer cap), then completed after the production budget was raised. The terminal SSE event was `run.completed` and included answer text with citations. Safe diagnostics now retain only error code/reason text; provider body, prompt and document content are not logged. A bounded provider-backed Deep Research smoke subsequently reached `completed` with evidence and a final report.

### Generation budget hardening follow-up (2026-08-11)

Production budgets are now explicit and envelope-checked: Ask `4096`, verifier
`4096`, planner/synthesis `16000`. The requested `16384` planner/synthesis cap
would exceed the mathematically compatible `0.70 + 0.20 + 0.10` reserves in an
`80000` context window, so `16000` is the highest safe value. Structured
Gateway requests keep reasoning disabled by default, retry `MODEL_OUTPUT_TRUNCATED`
at most once up to the stage cap, and do not retry `MODEL_OUTPUT_INVALID` or
non-retryable provider rejections. Runtime metadata exposes only safe budget,
attempt and finish information. Full unit tests remain green after this change.

### In-app Browser Ask/Research confirmation (2026-08-11)

After rebuilding the UI container, a fresh authenticated Browser tab with only
Russian Wikipedia selected completed both live paths. Ask sent a real SSE
chat request, rendered an answer with `[S1]`–`[S12]`, and exposed a working
Debug timeline ending in `query_complete` (query id suffix
`5e698f31-09c0-0346-b23f-8a943b6646ea`). Deep Research run `826e7e8a` visibly
progressed through retrieval/evaluation/synthesis to `completed · quality_gate`
with 15 evidence-memory items and 9 episodes. The run completed technically,
but its coverage stayed `partial` because the current Russian-Wikipedia index
returned unrelated asteroid/domain fragments; semantic retrieval quality is
still an open blocker even though transport and terminalization now work.
