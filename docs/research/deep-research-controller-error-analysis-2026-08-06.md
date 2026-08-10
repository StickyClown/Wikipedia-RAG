# Анализ ошибок Deep Research Controller

Дата: 2026-08-06
Область: focused hard gate `alias_reformulation_chain`
Статус: controller не принят как прошедший hard gate; одна ошибка исправлена, вторая требует точного stack trace

## 1. Цель анализа

Этот документ фиксирует проверяемую цепочку событий вокруг исправления Deep Research Controller. Цель анализа:

- отделить ошибки инфраструктуры, SQL и controller от проблем качества retrieval;
- сопоставить наблюдения с инвариантами нового controller;
- определить, что уже доказано тестами и focused gate;
- не потерять причинность из-за общего безопасного `error_code`;
- ответить, можно ли исправить оставшийся сбой и какой следующий эксперимент это подтвердит.

Документ не содержит скрытых внутренних рассуждений модели, provider payloads, сырых поисковых запросов или storage keys. Для аудита используются факты из исходного кода, тестов, безопасных gate reports и состояния БД.

## 2. Краткий вывод

В ходе работы выявлены два разных класса отказов.

1. **Подтверждённая SQL-ошибка была найдена и исправлена.** После добавления persistence для evidence fingerprint запрос с неявным `ON CONFLICT DO UPDATE` перестал быть допустимым для PostgreSQL. Из-за этого retrieval выполнялся, но evidence не сохранялся. Исправление заменило запрос на явный conflict target `(research_run_id, chunk_id)` и убрало неподходящий partial unique index для fingerprint.

2. **Остался отдельный `ProgrammingError` в последнем focused run.** По безопасному артефакту видно, что он произошёл после выбора bridge-вопроса `Who explicitly states the responsibility...` и записи первой попытки, но до сохранения episode/tool call. Точный текст исключения и stack trace не сохранились, потому что Compose был остановлен через `--down-after`. Поэтому нельзя честно утверждать, какой именно SQL или сериализация вызвали второй сбой.

Сбой выглядит исправимым: controller успел получить `covered` для `RB-17` и `Night Harbor`, `evidence_recall=1.0`, `unsupported_claim_count=0`, `acl_safety=true`, а все восемь предыдущих tool episodes успешно сохранились. Но hard-gate pass пока не доказан: финальный run имеет `status=failed`, а не `completed_partial` или `completed`.

## 3. Контекст и прочитанные материалы

Перед изменениями были просмотрены:

- `README.md`;
- `docs/STATUS.md`;
- `docs/architecture/search-and-deep-research.md`;
- `docs/research/deep-research-hard-gate-failure-analysis-2026-08-06.md`;
- `src/wikipediarag/deep_research.py`;
- `src/wikipediarag/research_planner.py`;
- `src/wikipediarag/repository.py`;
- `src/wikipediarag/research_tools.py`;
- `src/wikipediarag/research_tool_registry.py`;
- `src/wikipediarag/retrieval_profile.py`;
- `src/wikipediarag/schemas.py`;
- `src/wikipediarag/db.py`;
- unit tests для указанных модулей;
- reports и detail artifacts focused gate.

Рабочее дерево уже было изменено до этой задачи. Существующие изменения не откатывались и не смешиваются с выводами этого документа без проверки по diff.

## 4. Ожидаемый контракт controller

### 4.1 Состояние вопроса

Состояние вопроса разделено на две независимые оси:

- `execution_state`: `pending | running | done`;
- `outcome`: `covered | partial | exhausted | failed | null`.

Инварианты:

- `done` всегда имеет ненулевой `outcome`;
- `pending` и `running` всегда имеют `outcome=null`;
- terminal run не должен оставлять required question с `execution_state != done`;
- каждый controller tick обязан изменить состояние либо завершить run безопасной ошибкой controller bug.

Legacy `status` остаётся API-совместимой проекцией и дополнен значениями `exhausted` и `failed`.

### 4.2 Выбор следующего вопроса

`select_next_question` использует стабильный порядок:

1. required;
2. bridge;
3. normal;
4. tie-break по `created_at`, затем по `id`.

Это устраняет зависимость порядка работы от случайного порядка строк или результата planner.

### 4.3 Progress и повторное evidence

Повторный evidence определяется по стабильному fingerprint. Fingerprint строится из доступных идентификаторов KB/document version/chunk; при их отсутствии используется безопасный hash метаданных источника, title и abstract.

Повторный fingerprint не считается progress. Он расходует attempts текущего вопроса. После исчерпания бюджета вопрос terminalize-ится:

- как `partial`, если накоплены полезные claims/evidence;
- как `exhausted`, если полезного результата нет.

Global `no_progress_saturation` не используется как нормальное условие завершения всего run.

### 4.4 Planner и tools

Planner является advisory-only и может предлагать:

- search queries;
- tool candidates;
- discovered questions.

Planner не может:

- менять immutable original query;
- удалять required questions;
- выставлять coverage;
- завершать run.

При invalid planner schema используется fallback: поисковый запрос равен immutable text текущего вопроса, `derived_questions=[]`, run продолжается.

Все branches используют единый `ToolResult` и классификацию ошибок:

- `transient`: допускается retry;
- `permanent`: branch/question завершается без retry;
- `security`: branch останавливается безопасно;
- `controller_bug`: run завершается с безопасным кодом ошибки.

### 4.5 Deadline и отчёт

Есть абсолютный run deadline и per-question budgets: `max_attempts`, `max_rewrites`, `max_depth`.

При deadline оставшиеся вопросы должны быть terminalized как `exhausted` с причиной `run_deadline_exhausted`, после чего создаётся `completed_partial` с ненулевым synthesis. Partial report обязан перечислять confirmed findings, incomplete findings, unresolved questions, evidence и ограничения.

## 5. Хронология запусков

| Время UTC | Запуск / этап | Наблюдение | Вывод |
|---|---|---|---|
| `20260806T104157Z` | Первый официальный focused run | `DEEP_RESEARCH_GATE_COMPOSE_START_FAILED` | Инфраструктурный сбой старта; не является диагностикой controller. |
| `20260806T152720Z` | Официальный run | Compose, auth и uploads прошли; worker достиг retrieval, но evidence не сохранялся; run завершился DB `ProgrammingError` | Retrieval был достигнут, failure произошёл на persistence. |
| `20260806T153032Z` | Диагностический rerun с сохранением логов | В worker найдено: `asyncpg.exceptions.PostgresSyntaxError: ON CONFLICT DO UPDATE requires inference specification or constraint name` | Подтверждённая причина первой SQL-ошибки. |
| `20260806T153336Z` | Run после SQL-исправления | Нет terminal run report до истечения ожидания suite: `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED` | Интеграционный timeout suite, не доказательство нового controller bug. |
| `20260806T160022Z` | Run с `DEADLINE_REPORT_RESERVE_SECONDS=30` | Controller реально продвигал вопросы: были `done/partial`, `Night Harbor` и `RB-17` обрабатывались; suite всё равно дождалась deadline раньше полного отчёта | Внутренний deadline и внешний suite wait были плохо разделены. Позже reserve увеличен до 300 секунд. |
| `20260806T161804Z` | Последний focused run после reserve=300 | Полный detail artifact создан; `RB-17` и `Night Harbor` стали `done/covered`; позже run завершился `ProgrammingError` на следующем bridge-вопросе | Первый SQL-сбой устранён, но остаточный controller/DB сбой ещё не локализован. |

## 6. Доказательства последнего focused run

Основные артефакты:

- report: `artifacts/validation/deep-research-hard-gate/20260806T161804Z/report.json`;
- detail: `artifacts/validation/deep-research-hard-gate/20260806T161804Z/alias_reformulation_chain-detail.json`.

Параметры последнего run:

- начало: `2026-08-06T16:18:04Z`;
- конец: `2026-08-06T16:30:10Z`;
- `run_id`: `5d355ac3-01f9-4fec-81af-45ca266afa99`;
- `latency_seconds`: `622.25`;
- Compose/auth/uploads: passed;
- suite deadline exhaustion: не зафиксирован;
- финальный статус: `failed`.

### 6.1 Состояние вопросов

Безопасная сводка из detail artifact:

| Вопрос | Финальное состояние | Attempts | Значение |
|---|---|---:|---|
| Primary question | `done/partial` | 3 | Полезная, но неполная evidence. |
| Decomposition question | `done/partial` | 3 | Полезная, но неполная evidence. |
| `RB-17` | `done/covered` | 1 | Required bridge target достигнут. |
| `Night Harbor` | `done/covered` | 1 | Required bridge target достигнут. |
| `LTN-42` и следующие normal questions | `done/failed` | 0 | Они не были выбраны; были terminalized outer failure path. |
| Последующий responsibility bridge question | `done/failed` | 1 | Failure после записи попытки, до persisted episode/tool call. |

Важный положительный факт: в terminal run не осталось open required questions. Это подтверждает работу terminalization path, но не компенсирует `run_status=failed`.

### 6.2 Метрики

| Метрика | Значение | Интерпретация |
|---|---:|---|
| `coverage_score` | `0.75` | Основная требуемая область покрыта частично. |
| `evidence_recall` | `1.0` | Требуемая evidence найдена. |
| `unsupported_claim_count` | `0` | Неподтверждённых claims не обнаружено. |
| `contradiction_handled` | `true` | Противоречие обработано. |
| `acl_safety` | `true` | ACL/tenant/KB isolation в gate не нарушены. |
| `resume_integrity` | `true` | Проверка resume прошла. |
| `tool_call_count` | `8` | Все 8 сохранённых tool calls завершены. |
| `completed_tool_call_count` | `8` | До residual failure branches завершались. |
| `document_tool_call_count` | `0` | Использовался `extended_search`, document tool не был нужен. |
| `derived_question_count` | `9` | Planner обнаруживал questions; required terms не потеряны. |
| `raw_tool_payload_leak` | `false` | Raw provider payload не попал в проверяемый отчёт. |
| `missing_tool_query_hash_count` | `0` | Запросы имели безопасные hashes. |
| `episodes_with_context_summary` | `8` | Сохранённые episodes имели context summary. |
| `max_context_ratio` | `0.0223625` | Context defaults не были превышены. |
| `avg_context_ratio` | `0.015146875` | Нет сигнала о context overflow. |

На trajectory были найдены все ожидаемые derived terms: `LTN-42`, `RB-17`, `Night Harbor`. Missing required terms: none.

## 7. Подтверждённая и исправленная SQL-ошибка

### Симптом

Retrieval проходил, но evidence persistence завершалась PostgreSQL syntax error:

~~~text
asyncpg.exceptions.PostgresSyntaxError:
ON CONFLICT DO UPDATE requires inference specification or constraint name
~~~

### Причина

В persistence-коде использовался запрос вида `ON CONFLICT DO UPDATE` без conflict target. После изменений evidence persistence в схеме появились fingerprint-related ограничения, и PostgreSQL больше не мог однозначно вывести conflict arbiter для такого upsert.

### Воздействие

- retrieval мог вернуть hits;
- episode мог иметь `had_retrieval=true`;
- evidence record не сохранялся;
- controller видел DB failure, хотя первичная причина была в SQL upsert.

### Исправление

Запрос изменён на явный target:

~~~sql
ON CONFLICT (research_run_id, chunk_id) DO UPDATE
~~~

Также удалён partial unique index, который не был необходим для фактической идемпотентности evidence persistence. Смысл идемпотентности сохранён через существующий ключ run/chunk, retrieval-логика и ACL не менялись.

### Проверка исправления

После изменения focused runs прошли дальше persistence этого участка. В последнем run успешно сохранились 8 tool episodes, а `RB-17` и `Night Harbor` получили `done/covered`. Это подтверждает, что первая SQL-ошибка устранена, но не доказывает отсутствие других DB boundary failures.

## 8. Остаточный `ProgrammingError`

### Что точно известно

В последнем run:

1. До ошибки были сохранены 8 completed tool episodes.
2. `RB-17` и `Night Harbor` были terminalized как `done/covered`.
3. Затем был выбран bridge-вопрос про явно указанную ответственность.
4. Для него записан `attempt_count=1`.
5. Для него не появился persisted episode и не появился persisted tool call.
6. После исключения outer controller path выставил `controller_bug` и terminalized ещё не обработанные questions как `done/failed`.
7. В safe detail artifact сохранён только `error_code=ProgrammingError`; точный DB message отсутствует.

### Что неизвестно

Нельзя установить по имеющимся артефактам:

- какой SQL statement упал;
- был ли сбой в `create_query_run`, `create_research_episode`, `create_research_tool_call` или JSON serialization/constraint;
- возник ли failure внутри planner result persistence или при создании первой episode;
- была ли ошибка вызвана конкретным planner output.

Это ограничение связано с диагностикой, а не с отсутствием failure: Compose был остановлен через `--down-after`, поэтому worker logs с полным stack trace были удалены до анализа.

### Ранжированные гипотезы

| Гипотеза | Вероятность по текущим данным | Основание | Что подтвердит / опровергнет |
|---|---|---|---|
| Failure в initial episode/tool setup или DB transaction | Средняя-высокая | Ошибка после записи attempt, но до persisted episode/tool call; предыдущие episodes работали | Stack trace и безопасные stage markers для `create_query_run`, `create_research_episode`, `create_research_tool_call`. |
| Невалидный planner output на DB serialization/constraint boundary | Средняя | Ошибка появилась на новом вопросе после planner cycle; invalid schema path должен быть проверен на реальном output | Безопасная валидация перед persistence и точная asyncpg ошибка. |
| Общая ошибка retrieval/tool branch | Низкая | Нет tool call/episode для этого вопроса; предыдущие 8 calls успешны; retrieval quality metrics прошли | Если stack trace указывает на tool invocation до DB commit, гипотеза повысится. |
| Deadline или внешний suite timeout | Низкая для последнего run | Последний run завершился report за 622 секунд, ошибка классифицирована как `ProgrammingError` | Timestamp exception и worker logs; текущий report не указывает на deadline. |
| ACL/tenant scope regression | Очень низкая | `acl_safety=true`, leakage checks false, retrieval targets достигнуты | Только security audit или failing ACL fixture мог бы изменить оценку. |

Гипотезы не являются установленной причиной. До получения stack trace нельзя выбирать конкретный SQL fix на основании одного `ProgrammingError`.

## 9. Можно ли это исправить

**Да, с высокой вероятностью это локально исправимый сбой, но он ещё не подтверждён исправленным.** Основания:

- controller смог пройти discovery и retrieval;
- required bridge questions `RB-17` и `Night Harbor` достигли `covered`;
- `evidence_recall=1.0`, `unsupported_claim_count=0`, `acl_safety=true`;
- все предыдущие 8 tool branches завершились и записались;
- первый SQL defect был локализован и исправлен без изменения retrieval, ACL, Model Gateway или context defaults.

Одновременно есть существенный риск: ошибка находится на persistence/controller boundary и внешний safe error code скрывает конкретный statement. Поэтому объявлять hard-gate pass или менять DB схему вслепую нельзя.

## 10. Следующий диагностический эксперимент

Порядок действий должен быть bounded и воспроизводимым:

1. Запустить только focused `alias_reformulation_chain`, без полного hard matrix.
2. Не использовать `--down-after` до получения terminal report и worker logs.
3. Сохранить безопасные артефакты до teardown:
   - report и detail artifact;
   - worker log с фильтрацией provider payloads, raw queries и storage keys;
   - run status/stage/error code;
   - количество questions, attempts, episodes и tool calls.
4. Временно добавить безопасные stage markers без содержимого запросов:
   - `after_attempt_record`;
   - `before_create_query_run` / `after_create_query_run`;
   - `before_create_episode` / `after_create_episode`;
   - `before_create_tool_call` / `after_create_tool_call`.
5. Для каждого marker записывать только stage, run/question IDs в разрешённом безопасном формате и error class; не записывать raw planner result.
6. Повторить unit/static tests.
7. Повторить focused gate.
8. Только после focused pass проверять, что:
   - `run_status` не `failed`;
   - `RB-17` и `Night Harbor` не open;
   - `evidence_recall=1.0`;
   - `unsupported_claim_count=0`;
   - `acl_safety=true`;
   - нет suite deadline exhaustion;
   - synthesis для partial completion не null;
   - нет raw provider/query/storage-key leak.

Stage markers нужны именно для локализации. Они не должны превращаться в нормальные логи с содержимым пользовательских запросов или документов.

## 11. Возможные исправления после получения stack trace

| Если stack trace укажет на | Исправление | Что не менять |
|---|---|---|
| Неверный upsert или constraint | Добавить явный conflict target, проверить migration и идемпотентный ключ, покрыть повторным insert тестом | Retrieval ranking, ACL, tenant/KB scope. |
| JSON serialization/constraint planner result | Валидировать advisory result до persistence и отправлять invalid schema в fallback; не сохранять недопустимое поле | Immutable original question и planner advisory-only contract. |
| Ошибку создания episode/tool call | Сделать transaction boundary и error taxonomy явными; transient branch retry ограничить question budget | Не превращать controller bug в silent partial success. |
| Transient DB/provider failure | Классифицировать как transient и повторить в рамках `max_attempts` | Не вводить unbounded retry. |
| Permanent/security failure | Terminalize branch/question безопасной причиной и продолжить независимые questions | Не скрывать security failure и не завершать весь run без необходимости. |
| Controller invariant violation | Fail run с `controller_bug`, сохранить безопасную диагностику, добавить regression test | Не выставлять coverage напрямую planner-у. |

Нельзя просто заменить общий `except Exception` на `partial` и считать проблему исправленной: это скроет controller bug и может дать отчёт без корректной provenance.

## 12. Проверки и текущий статус acceptance

Уже выполнены:

~~~text
uv run ruff check ...
PASS

uv run ruff format --check ...
PASS: 10 files already formatted

uv run mypy <targeted changed modules>
PASS: no issues in 7 source files

uv run pytest tests/unit -q
331 passed, 2 warnings

uv run pytest tests/unit/test_deep_research.py tests/unit/test_retrieval_profile.py tests/unit/test_answerability.py -q
56 passed

git diff --check
PASS
~~~

Последний focused run `20260806T161804Z`:

| Acceptance criterion | Статус | Комментарий |
|---|---|---|
| `RB-17` terminal (`covered/partial/exhausted`, не open) | PASS | `done/covered`. |
| `Night Harbor` terminal (`covered/partial/exhausted`, не open) | PASS | `done/covered`. |
| `evidence_recall=1.0` | PASS | По report. |
| `unsupported_claim_count=0` | PASS | По report. |
| `acl_safety=true` | PASS | По report. |
| Нет raw provider/query/storage-key leaks | PASS | Leak checks false / query hashes present. |
| Нет suite deadline exhaustion | PASS для последнего run | Последний failure не был suite deadline. |
| `run_status` completed/partial вместо failed | FAIL | `run_status=failed`, `ProgrammingError`. |
| Partial report с ненулевым synthesis | Не подтверждено последним run | Failed path имел `final_report.synthesis=null`; deadline fallback unit-tested отдельно. |
| Full hard matrix | НЕ ЗАПУСКАЛСЯ | Запрещён до focused pass. |

## 13. Ограничения анализа

- Точный stack trace второго `ProgrammingError` отсутствует из-за teardown после focused run.
- Safe detail artifact намеренно не содержит raw provider payload и SQL query; это правильно для production safety, но снижает диагностическую детализацию.
- Не следует делать вывод, что весь controller стабилен только из-за `331 passed`: unit tests не заменяют real-provider DB transaction path.
- Не следует делать вывод, что retrieval сломан: `evidence_recall=1.0`, claims и ACL checks указывают на другой failure boundary.
- Existing warnings относятся к FastAPI `on_event` deprecation и не связаны с этим инцидентом.

## 14. Итоговое решение

Текущий результат имеет статус **не принят**.

Первая причина была доказана и исправлена. Последняя причина, вероятнее всего, находится в ограниченном controller/DB setup path после записи attempt и до сохранения episode, поэтому исправление реалистично. Однако без сохранённого worker stack trace нельзя достоверно назвать конкретный дефект и нельзя утверждать, что следующий patch будет корректным.

Блокирующее действие одно: повторить focused fixture с сохранением worker logs и безопасными stage markers, локализовать второй `ProgrammingError`, добавить regression test на найденный boundary и только затем повторить focused hard gate.

## 15. Addendum: validation on 2026-08-07

The previous sections intentionally describe the state before the preserved
diagnostic reruns. They are historical evidence, not the current conclusion.

### 15.1 Confirmed partial-finalization defect

The suspected duplicate assignment in finalization was reproduced at the SQL
builder level and exercised against PostgreSQL. When a completed job carried a
controlled error, the old builder could emit two assignments for
`error_code`/`error_message`: one clearing a stale value and one writing the new
safe value. PostgreSQL rejects that statement, so the transaction containing
question terminalization and the partial report rolled back. The fix keeps an
explicit error value and a clear operation mutually exclusive. The same rule
is applied to job items and research runs.

The PostgreSQL integration test now verifies that `_finish_partial_run` stores:

- `research_runs.status=completed` and `progress.stage=completed_partial`;
- `stop_reason=run_deadline_exhausted`;
- non-empty deterministic synthesis with confirmed, incomplete, unresolved,
  evidence and limitations sections;
- every remaining question as `done/exhausted`;
- the job as `completed` with one safe error code assignment.

This cause is confirmed for the old deadline/partial-finalization failure. It
was not the failure in the later real-provider run, because that run failed in a
heartbeat task before its final report was stable.

### 15.2 Preserved real-provider diagnosis and fixes

The first focused real-provider rerun without `--down-after` preserved the
Compose stack and filtered diagnostics. The first heartbeat error was:
`AmbiguousParameterError: could not determine data type of parameter $1` for a
nullable lease comparison. An explicit cast was added, then the preserved
stack exposed a second type mismatch: the database column
`research_runs.controller_lease_id` is `text`, so casting the parameter to
`uuid` produced `text = uuid`. The final implementation casts the nullable
parameter to `text` in the heartbeat comparison.

The failure path was important: the controller had already completed useful
episodes, then the heartbeat task exception escaped the worker cleanup path and
could replace a completed run with `failed/ProgrammingError`. The final
heartbeat fix removes this database type failure without weakening error
visibility or changing the run status model.

The preserved post-fix run at
`artifacts/validation/deep-research-hard-gate/20260807T152545Z/report.json`
had:

| Metric | Result |
|---|---:|
| run status | `completed` |
| required open questions | `0` |
| `RB-17` / `Night Harbor` | `done` |
| `coverage_score` | `1.0` |
| `evidence_recall` | `0.667` |
| `unsupported_claim_count` | `0` |
| `acl_safety` | `true` |
| raw provider/query/storage-key leak | `false` |

The missing marker was `DR_HARD_LANTERN_RUNBOOK`. This is a real-provider
retrieval/quality blocker, not evidence of another controller crash. The mock
hard gate, using the same fixture and controller, passed with recall `1.0` at
`artifacts/validation/deep-research-hard-gate/20260807T150333Z/report.json`.

### 15.3 Harness provenance correction

The evaluator previously compared fixture-local document IDs such as
`lantern_summary` directly with persisted API IDs such as `doc:...`. The
evidence content was present, but that mismatch could report false recall
failures. The CLI now keeps an in-memory fixture-to-persisted ID map for
evaluation only. It is not included in the public research detail or report,
and no retrieval behavior changes. A regression test covers the mapping.

### 15.4 Current conclusion

The deterministic controller and PostgreSQL persistence boundary are now
validated by unit/static tests, a real PostgreSQL integration test and the
focused mock gate. The focused real-provider run no longer fails with the
previous `ProgrammingError`, and the required questions are terminalized.
The acceptance gate remains open because the real-provider run retrieved only
two of three required evidence markers. No retrieval ranking, ACL scope,
Model Gateway contract or `45/55/70` context default should be changed solely
from this result. The next approved task is a bounded provider/retrieval
quality investigation using the preserved quality artifact, not another
controller rewrite or the full hard matrix.

## 16. Addendum: why `DR_HARD_LANTERN_RUNBOOK` was missed

The latest artifact shows that this is primarily a system/retrieval path
problem, not missing fixture data and not a controller crash.

### 16.1 Facts from the run

- All three fixture uploads returned `job_status=completed`.
- The fixture contains three independent evidence markers: alias, runbook and
  service owner.
- The real run persisted only two evidence records: the alias document and the
  service-owner document. The runbook document is absent from persisted
  evidence, so the evaluator is not inventing the failure.
- The mock run persisted all three markers and passed with `evidence_recall=1.0`.
- The real run completed normally, had six completed tool calls, zero open
  required questions, no unsupported claims and no SQL/controller exception.
- All five document-tool calls returned the same first evidence reference. The
  second available source was never selected by the controller.

### 16.2 Reproduction of the main defect

Uploaded-document chunking assigns `page_id` from the document-local page
locator or the local chunk ordinal. A one-chunk Markdown file therefore gets
`page_id=1`; each separate uploaded file gets the same value.

The retrieval postprocessor groups the page quota by only
`(knowledge_base_id, page_id)`, while `upload_sota_mvp` inherits
`page_quota=2`. Three different uploaded documents with `page_id=1` are thus
treated as one page group and the third candidate is dropped as
`PAGE_QUOTA`.

A short in-memory probe against the current implementation produced exactly:

```text
page_quota= 2
selected= ['document-1', 'document-2']
dropped= [('document-3', 'PAGE_QUOTA')]
```

This matches the real artifact: two documents were retained and the runbook
document was missing. The relevant code is in
`src/wikipediarag/document_ingestion.py:364`,
`src/wikipediarag/retrieval.py:800-802` and
`config/retrieval.yaml:34`.

### 16.3 Independent document-tool defect

The controller's advisory planner schema can name a document tool, but it
does not select a source. `_controller_planner_decision` always builds
document-tool arguments from `visible[0]`. A short probe with three visible
sources returned the first source every time, even when the other two were
available.

This explains the repeated first-source document lookups in the real artifact.
It also means that increasing retrieval `top_k` alone would not be sufficient:
the controller still would not rotate to the missing source.

The relevant code is `src/wikipediarag/deep_research.py:2632-2647`.

### 16.4 Why the search stopped too early

The extended-search harness stops when its internal term inventory reaches
`coverage >= 1.0`, with only one minimum search step for this question. That
inventory is based on terms from the user question, not on the expected
multi-document evidence chain. The real tool summary simultaneously reported
`stop_reason=evidence_sufficient` and `answerability=PARTIAL`.

The question is not recognized by the current bridge detector, so the second
reformulation was optional. The run therefore did not force another broad
search after the partial result. The relevant code is
`src/wikipediarag/extended.py:135` and `src/wikipediarag/extended.py:280-282`.

### 16.5 Responsibility assessment

| Layer | Assessment | Reason |
|---|---|---|
| Fixture data | Low likelihood | The runbook text exists and all uploads completed. A direct published-chunk probe is still needed for absolute proof. |
| Retrieval/postprocess | High likelihood, primary cause | Local upload page IDs collide and the page quota deterministically removes the third document. |
| Controller/tool routing | High likelihood, secondary cause | Document tools always use the first visible evidence source. |
| Search algorithm | Medium-high likelihood, contributing cause | Partial answerability is allowed to stop the extended search after one step. |
| Model | Low likelihood as root cause | The model cannot select a source that retrieval did not expose, and the same fixture passes in mock mode. It may still affect ranking and query choice. |
| ACL/tenant scope | Very low likelihood | ACL safety is true and the missing document is from the same fixture KB. |

### 16.6 Minimal confirmation and fix order

Before changing model prompts or retrieval ranking, run a safe database/API
probe for each uploaded document and verify: active document, published chunk
count, chunk `page_id`, and presence in the normal search candidate set.

Then run three bounded A/B checks:

1. Fix only the page-group key to include document identity. If recall becomes
   `1.0`, the primary defect is confirmed.
2. Keep the page-group fix and rotate document-tool sources deterministically.
   This verifies that the tool can inspect the runbook source rather than
   repeatedly reading the first source.
3. Require another search step when answerability is `PARTIAL` or missing
   answer-bearing terms. This verifies the fallback when the first ranking
   still misses a document.

Only after these checks should model prompt or reranker changes be considered.
Changing `page_quota` from `2` to `3` may hide this fixture failure but would
not correct the identity bug for other upload sets.

## 17. Реализованное системное исправление (2026-08-07)

Проверка кода подтвердила, что это дефект retrieval/control-контракта, а не
самостоятельная проблема качества провайдера. Реализованы следующие
изменения без миграции БД, переиндексации, изменения ranking или настройки
`page_quota=2`:

- единый `page_scope_key` использует `(knowledge_base_id, document_id,
  page_id)` с проверкой `is not None`, затем locator и уникальный `chunk_id`;
  quota увеличивается только после прохождения token budget;
- `ToolRequest.evaluation_query` передаёт неизменный research question в
  document-tools, поэтому section/metadata lookup больше не оцениваются по
  пустому wire-query и непустой нерелевантный результат не закрывает вопрос;
- controller выбирает источник только из повторно ACL-trimmed evidence
  текущего run, учитывает приватную `validated_args` history и детерминированно
  предпочитает релевантные неизученные документы; публичный detail по-прежнему
  не содержит `validated_args`;
- Extended Search строит provisional final-evidence набор после каждого шага и
  разрешает `evidence_sufficient` только для `ANSWERABLE` без missing terms и
  конфликтов. Для `PARTIAL`/`CONFLICTING` добавляется bounded gap-repair query,
  а исчерпание бюджета конфликта фиксируется как `conflict_unresolved`;
- `RunContract` поднят до schema v2 и включает версии context-selection и
  Extended Search control policy, поэтому изменение поведения отражается в
  `run_contract_id`.

Такое решение сохраняет существующие ACL, Model Gateway, ranking, контекстные
лимиты и HTTP-формы. Оно переносит из STORM/Open Deep Research/AIR только
разделение сбора и синтеза, bounded loop и gap-driven stopping; multi-agent
архитектура этих проектов в систему не добавляется. После unit/static и
focused mock checks необходим один focused real-provider pass с сохранением
стека при failure и project-scoped cleanup только после pass.

## 18. Acceptance pass

Focused OpenRouter/Qwen run от 2026-08-07 (`upload_sota_mvp`, один task,
900-секундный лимит) завершился успешно: `run_status=completed`,
`coverage_score=1.0`, `evidence_recall=1.0`, `unsupported_claim_count=0`,
`acl_safety=true`, `open_required_questions=0`, `raw_tool_payload_leak=false`.
Detail содержит все три fixture evidence markers. В пяти document-tool calls
источники выбирались как новые ACL-visible S2, S1, S3, после чего S2 был
повторно использован только после исчерпания альтернатив. Extended Search
сохранил `answerability=PARTIAL` вместе с `stop_reason=no_new_evidence`, то
есть ложного `evidence_sufficient` больше нет.

После проверки отчёта, detail и фильтрованных worker logs был удалён только
изолированный Compose project `wikipediarag-dr-gate-20260807165245-2466f827`
с его volumes и orphan-контейнерами. Полная hard matrix по остальным задачам
не запускалась и остаётся отдельной работой.

## 19. Retrieval Correctness V3 implementation update (2026-08-07)

После повторного критического аудита область исправления расширена до общего
retrieval-контракта. Новые ingestion-пути получают tenant/KB-scoped IDs и
индексы, исходные IDs сохраняются в metadata и `legacy_id_mappings`, а BM25
read-path больше не создаёт индексы. Retrieval events теперь используют
allowlisted projection, sequence ordering и жёсткий payload cap; raw content,
ACL/storage/provider payloads не сохраняются в stage events.

Context selector канонизирует expanded parents, сохраняет supporting chunk
provenance, применяет стабильные tie-breakers, tokenizer-like budget и soft
quota repair. Answerability больше не полагается на общий score threshold:
используется rank confidence, полный title/alias match и conflict detection
для divergent values даже без слова «конфликт». Multi-KB dense searches
получают один embedding и выполняются параллельно; pre-rerank equal KB cap
удалён. Eval generator и metrics исправлены: hard-negative/unanswerable
labels измеримы, а Recall@K теперь является долей найденных gold, а не
binary Hit@K.

Детерминированная проверка этой инкрементальной реализации: 360 unit tests,
`ruff check src`, `mypy src`, schema application и PostgreSQL persistence
integration (`1 passed`) успешны. Full `mypy src tests` и repository-wide
format check всё ещё включают ранее существовавшие ошибки в тестовых/handler
файлах; focused real-provider retrieval baseline после rolling reindex ещё не
запускался и не считается acceptance result.
