# Deep Research Hard-Gate Failure Analysis

Дата отчёта: 2026-08-06
Репозиторий: `C:\my_python_project\WikipediaRag`
Статус: hard-gate не принят; причина локализована, но финальный focused rerun после последней настройки ещё не выполнен.

## 1. Итог

Hard Deep Research не сломался на инфраструктуре, ACL или отсутствии evidence. Главная последовательность отказа:

1. Реальный provider/Qwen иногда возвращал planner-ответ, который не соответствовал ожидаемой схеме: `planner_invalid_schema`.
2. После восстановления planner tool loop выполнял поиск и находил требуемые документы.
3. Runtime считал повторно найденные chunks прогрессом и слишком рано завершал цикл по `no_progress_saturation`.
4. Из-за этого открытые вопросы `RB-17` и `Night Harbor` не всегда обрабатывались.
5. В результате evidence recall достигал `1.0`, ACL оставался безопасным, но evaluator отклонял run из-за незакрытых expected questions.
6. В более раннем `policy_exception_bridge` была дополнительная ошибка: planner сгенерировал повреждённый mixed-script query, answerability стала `UNANSWERABLE`, после чего run завершился `ProgrammingError`.

Фактический production blocker сейчас: controller должен корректно drain-ить оставшиеся открытые вопросы до saturation, а не только увеличивать числовой лимит `max_no_progress_steps`.

## 2. Что читалось и какой был активный goal

Перед анализом прочитаны:

- `README.md`;
- `docs/STATUS.md`;
- `docs/architecture.md`;
- `docs/architecture/search-and-deep-research.md`;
- `docs/research/deep-research-runtime-failure-analysis-2026-08-04.md`;
- `docs/exec-plans/36-deep-research-tool-loop.md`;
- `config/retrieval.yaml`;
- `config/models.yaml`;
- ключевые `src/wikipediarag/*research*` файлы;
- пользовательский отчёт `C:\Users\Компьютер\Downloads\deep-research-report (40).md`.

Активная цель из `docs/STATUS.md`: стабилизировать real-provider Deep Research path, прежде всего planner contract и focused fixtures, не меняя context default `45% / 55% / 70%`.

## 3. Fixtures и ожидаемый путь

### `alias_reformulation_chain`

Ожидаемая цепочка ответа:

```text
Project Lantern -> LTN-42 -> RB-17 -> Night Harbor -> Borealis
```

Evidence в fixture:

- `lantern-summary.md`: Project Lantern переименован в `LTN-42`, используется runbook `RB-17`;
- `runbook-index.md`: `RB-17` относится к сервису `Night Harbor`;
- `night-harbor-ops.md`: сервис поддерживает команда `Borealis`, on-call 24/7, эскалация 15 минут.

Ожидаемые evaluator-вопросы: `Project Lantern`, `LTN-42`, `RB-17`, `Night Harbor`. Минимум: 3 coverage records, минимум 2 covered/partial, минимум 3 completed tool calls.

### `policy_exception_bridge`

Ожидаемый ответ:

```text
Aster-North находится в регионе North-7 и относится к telemetry.
Для North-7 действует regional override: retention 90 дней до 2026-12-31.
Глобальная политика даёт 30 дней только при отсутствии regional override.
```

Фактически evidence был найден, но повреждённый planner query нарушил answerability.

## 4. Команды и вызовы tools

### Unit/static checks

```powershell
uv run ruff check src\wikipediarag\deep_research.py src\wikipediarag\retrieval_profile.py tests\unit\test_retrieval_profile.py
uv run mypy src\wikipediarag\deep_research.py src\wikipediarag\retrieval_profile.py
uv run pytest tests\unit\test_retrieval_profile.py tests\unit\test_deep_research.py tests\unit\test_answerability.py -q
git diff --check
```

Результаты:

- `ruff`: exit `0`;
- `mypy`: exit `0`;
- pytest: `48 passed`;
- `git diff --check`: exit `0`, только предупреждения о CRLF.

### Focused hard-gate calls

```powershell
uv run python -m wikipediarag.cli deep-research-hard-gate `
  --task-id alias_reformulation_chain `
  --max-tasks 1 `
  --timeout-seconds 300 `
  --down-after
```

Затем повторные bounded runs:

```powershell
uv run python -m wikipediarag.cli deep-research-hard-gate `
  --task-id alias_reformulation_chain `
  --max-tasks 1 `
  --timeout-seconds 900 `
  --down-after
```

### Runtime probes

Во время зависшего run выполнялись безопасные probes без удаления volumes:

```powershell
docker ps --format '{{.Names}}\t{{.Status}}'
docker compose -p <project> ps
docker compose -p <project> logs --tail 120 worker
docker compose -p <project> logs --tail 80 model-gateway mock-provider
docker compose -p <project> exec -T postgres psql -U rag -d rag -c "select ... from research_runs ..."
docker compose -p <project> exec -T postgres psql -U rag -d rag -c "select ... from research_episodes ..."
docker compose -p <project> exec -T postgres psql -U rag -d rag -c "select ... from research_tool_calls ..."
```

Эти probes показали, что containers были healthy, worker продолжал работать, а run находился на конкретном stage, а не падал из-за Compose.

## 5. Хронология запусков

| Report | Run | Результат | Главная ошибка |
|---|---|---|---|
| `20260805T182753Z` | полный hard matrix | exit `1` | alias partial, policy run failed, два следующих fixture упёрлись в suite deadline |
| `20260805T192109Z` | alias focused | exit `1` | `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED` после planner/provider failures |
| `20260805T193550Z` | alias focused | exit `1` | suite deadline; worker находился в `retrieve` |
| `20260805T195245Z` | alias после planner/query fixes | exit `1` | run terminal `completed_partial`, evaluator: `Night Harbor` остался `open` |
| `20260805T201606Z` | alias после `max_no_progress_steps=5` | exit `1` | run terminal `completed_partial`, evaluator: `RB-17` и `Night Harbor` остались `open` |

Полные артефакты:

- [full hard report](../../artifacts/validation/deep-research-hard-gate/20260805T182753Z/report.json);
- [baseline alias detail](../../artifacts/validation/deep-research-hard-gate/20260805T182753Z/alias_reformulation_chain-detail.json);
- [baseline policy detail](../../artifacts/validation/deep-research-hard-gate/20260805T182753Z/policy_exception_bridge-detail.json);
- [alias report after timeout/recovery](../../artifacts/validation/deep-research-hard-gate/20260805T195245Z/report.json);
- [alias detail after timeout/recovery](../../artifacts/validation/deep-research-hard-gate/20260805T195245Z/alias_reformulation_chain-detail.json);
- [alias report after saturation window 5](../../artifacts/validation/deep-research-hard-gate/20260805T201606Z/report.json);
- [alias detail after saturation window 5](../../artifacts/validation/deep-research-hard-gate/20260805T201606Z/alias_reformulation_chain-detail.json).

Live logs:

- [initial hard live-log](../../artifacts/validation/live/deep-research-hard-gate-run-20260805T212753Z.log);
- [alias recovery live-log](../../artifacts/validation/live/deep-research-hard-alias-fix-20260805T224500Z.log);
- [alias timeout live-log](../../artifacts/validation/live/deep-research-hard-alias-timeout-final-20260805T231500Z.log);
- [latest alias live-log](../../artifacts/validation/live/deep-research-hard-alias-final-pass-20260806T000000Z.log).

## 6. Первый отказ: planner contract

В initial real-provider run worker сначала успешно завершил episode 1:

```json
{
  "stage": "episode_completed",
  "last_episode_index": 1,
  "last_coverage_status": "covered",
  "derived_question_count": 5
}
```

На следующем planner transition live-log многократно показывал:

```json
{
  "stage": "planner_failed",
  "last_episode_index": 2,
  "last_planner_error_code": "planner_invalid_schema"
}
```

Почему это ломало run:

- provider envelope приходил не в ожидаемой форме;
- schema parser не мог построить `ResearchPlannerOutput`;
- повторные попытки не переходили к безопасному terminal state;
- hard-gate тратил deadline на planner recovery;
- дальнейшие expected tool calls не выполнялись.

Исправления:

- bounded `asyncio.wait_for(..., timeout=90)` для planner и repair call;
- безопасная проверка response envelope;
- отдельные коды `planner_invalid_json`, `planner_invalid_schema`, `planner_timeout`, `planner_provider_error`, `planner_empty_content`;
- deterministic fallback для recoverable real-provider planner failures.

Код: [research_planner.py](../../src/wikipediarag/research_planner.py), `PLANNER_TIMEOUT_SECONDS`, `_planner_response_content`.

## 7. Второй отказ: повреждённый tool query

В `policy_exception_bridge` planner сформировал query с повреждённым mixed-script текстом. В detail artifact видно:

- тема run: `Какой срок удержания логов применить к кейсу Aster-North и почему?`;
- tool: `extended_search`;
- tool call: completed;
- evidence count: `3`;
- `answerability.status`: `UNANSWERABLE`;
- reason: `no_required_parts_covered`;
- run error: `ProgrammingError`.

При этом evidence уже содержал правильные документы:

- North-7 exception: 90 дней;
- global telemetry policy: 30 дней;
- Aster-North case: North-7 + telemetry.

Таким образом, проблема была не в индексе и не в ACL. Planner query заменил исходный вопрос и ухудшил answerability. Исправление: `_ensure_original_research_query()` всегда ставит исходный вопрос первым и добавляет planner query только как bounded supplement.

## 8. Третий отказ: suite deadline во время retrieval

После planner/query fixes run перестал падать сразу, но hard-gate несколько раз получил:

```text
DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED
deep research hard gate deadline elapsed during run wait
```

Runtime probes для run `fd232a53-975f-433f-b417-1611322d75be` показывали:

```text
research_runs.status  = running
progress              = {"stage":"retrieve","last_episode_index":1}
research_episodes    = episode 1 completed, episode 2 running/retrieve
```

Worker logs при этом показывали успешные обращения к:

```text
POST /v1/embeddings 200
POST /v1/rerank 200
POST /_search 200
```

Причина: один `execute_research_tool()` мог последовательно выполнять primary search и до двух rewrites без общего timeout. Отдельные model-client timeouts не ограничивали всю операцию целиком.

Исправление:

- `DeepResearchConfig.tool_timeout_seconds = 180`;
- `asyncio.wait_for(execute_research_tool(...))`;
- safe code `research_tool_timeout`;
- failed tool call и episode сохраняются с безопасным error code вместо бесконечного ожидания.

Код: [deep_research.py](../../src/wikipediarag/deep_research.py), `ResearchToolTimeoutError`, вызов около `asyncio.wait_for`.

В последующих runs timeout не сработал: retrieval был медленным, но конечным.

## 9. Четвёртый отказ: no-progress saturation

После bounded retrieval run `20260805T195245Z` стал terminal:

```json
{
  "status": "completed",
  "stop_reason": "no_progress_saturation",
  "progress": {
    "stage": "completed_partial"
  },
  "error_code": null
}
```

Метрики evaluator:

```text
coverage_score       = 0.75
evidence_recall      = 1.0
unsupported_claims   = 0
acl_safety            = true
completed_tool_calls = 6
derived_questions     = 7
```

Tool-call ledger:

| # | Tool | Query hash | Latency | Evidence | Answerability |
|---:|---|---|---:|---:|---|
| 1 | `extended_search` | `f98abf243f834c76aab39722b701decc` | 2664 ms | 2 | `PARTIAL` |
| 2 | `extended_search` | `2c060180277c23220b6139ab0f8974d2` | 12762 ms | 3 | `ANSWERABLE` |
| 3 | `extended_search` | `2c060180277c23220b6139ab0f8974d2` | 21133 ms | 3 | `ANSWERABLE` |
| 4 | `extended_search` | `0c06acbd44a506d0ceccdbfcaa8c2abf` | 15700 ms | 2 | `ANSWERABLE` |
| 5 | `extended_search` | `60f20cf10114101b5d2adf96a58ce662` | 7464 ms | 2 | `ANSWERABLE` |
| 6 | `extended_search` | `25ff1d148e597d0352be1efd2e02552d` | 12438 ms | 2 | `ANSWERABLE` |

Все tool calls завершились успешно. Ошибка возникла после retrieval, когда controller решил, что повторные chunks больше не дают progress.

## 10. Почему увеличение лимита до 5 не решило проблему

Run `20260805T201606Z` после настройки `max_no_progress_steps=5` показал:

```text
coverage_score  = 0.50
evidence_recall = 1.0
ACL             = true
unsupported     = 0
run status      = completed
stop reason     = no_progress_saturation
```

Evaluator failure:

```text
question containing 'RB-17' had status open
question containing 'Night Harbor' had status open
```

Episode path:

| Episode | Stage | Coverage | Evidence | Result |
|---:|---|---|---:|---|
| 1 | `quality_gate` | partial | 2 | original question |
| 2 | `quality_gate` | partial | 3 | repeated/expanded search |
| 3 | `quality_gate` | covered | 3 | Project Lantern |
| 4 | `quality_gate` | covered | 3 | LTN-42 bridge |
| 5 | `quality_gate` | finish from existing evidence | 0 | no tool call |
| 6 | `quality_gate` | covered | 2 | next derived question |
| 7 | `quality_gate` | covered | 2 | next derived question |

Controller stopped while the expected `RB-17` and `Night Harbor` questions were still open. This proves that changing the numeric window is only a mitigation, not the correct invariant.

The current config was subsequently changed to:

```yaml
deep_research:
  max_no_progress_steps: 7
  tool_timeout_seconds: 180
```

The `7` setting has not yet been accepted by a final focused hard rerun.

## 11. Stage-by-stage diagnosis

| Stage | Observed result | Error | Conclusion |
|---|---|---|---|
| Compose startup | healthy services | none | infrastructure is not the root cause |
| Auth/tenant/KB setup | succeeded | none | credentials and scope setup are valid |
| Upload/ingestion/index | documents published | none | OpenSearch and ingestion are functional |
| `triage`/question creation | derived terms created | none | decomposition works, although it can overproduce duplicate questions |
| `plan` | initial real-provider schema failures | `planner_invalid_schema`, later provider envelope failures | planner contract/latency was an initial blocker |
| `retrieve` | searches and rewrites complete | earlier suite deadline, later no tool error | retrieval finds evidence; whole-tool timeout was missing |
| `evaluate` | coverage records and answerability written | policy fixture once got `UNANSWERABLE` | corrupted planner query caused false negative answerability |
| `verify_claims` | supported claims, no unsupported claims | none | claim verification is not the failing stage |
| `rewrite`/question scheduling | repeated searches, open questions remain | `no_progress_saturation` | current controller stops before draining open questions |
| `synthesize` | not reached for partial terminal runs; `synthesis: null` | no direct synthesis error | partial terminal path intentionally skips confident synthesis |
| `quality_gate`/evaluator | rejects run | expected questions `open` | evaluator correctly rejects incomplete chain |

## 12. Что было исправлено

Implemented changes:

- [research_planner.py](../../src/wikipediarag/research_planner.py): bounded planner/repair calls, safe provider-envelope parsing and deterministic recovery;
- [deep_research.py](../../src/wikipediarag/deep_research.py): original-query retention, new-evidence accounting, bounded tool timeout;
- [repository.py](../../src/wikipediarag/repository.py): clear transient run error after successful recovery;
- [retrieval_profile.py](../../src/wikipediarag/retrieval_profile.py): typed `tool_timeout_seconds` setting;
- [config/retrieval.yaml](../../config/retrieval.yaml): production timeout and saturation settings;
- [tests/unit/test_deep_research.py](../../tests/unit/test_deep_research.py): original query retention test;
- [tests/unit/test_retrieval_profile.py](../../tests/unit/test_retrieval_profile.py): profile defaults and timeout settings;
- [docs/STATUS.md](../STATUS.md): current blocker and validation history.

Не менялись:

- context default `45% / 55% / 70%`;
- Model Gateway contract;
- tenant/KB/ACL scope;
- GraphRAG, public web browsing, multi-agent swarm, ColBERT, learned sparse retrieval и proposition indexing.

## 13. Что нужно сделать дальше

1. Исправить saturation invariant: не завершать run, пока есть open questions из уже созданного bounded question set, либо явно переводить их в `partial` с причиной `no_progress_saturation`.
2. Разделить `question status` и `coverage status`, чтобы evaluator не видел `open`, когда coverage уже `covered` или `partial`.
3. Добавить unit/runtime test: repeated evidence + два open expected questions должны обработать оба вопроса до terminal state.
4. Повторить focused `alias_reformulation_chain` после этой правки.
5. Затем выполнить `within_doc_exception_clause`, `section_alias_owner_chain`, `policy_exception_bridge` и полный hard gate.
6. Только после этого сравнивать context policies `35/45/55`.

## 14. Acceptance criteria для следующего hard rerun

Для `alias_reformulation_chain`:

- run status `completed` или допустимый `completed_partial` без незакрытых required expected questions;
- `RB-17` и `Night Harbor`: `covered` или `partial`;
- `evidence_recall = 1.0`;
- `unsupported_claim_count = 0`;
- `acl_safety = true`;
- минимум 3 completed `extended_search` tool calls;
- нет `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED`;
- нет raw provider payload, storage key или raw tool query leak;
- synthesis формируется только из verified/partial claims либо partial report явно перечисляет unresolved claims.

## 15. Reproduction shortcuts

Focused run:

```powershell
uv run python -m wikipediarag.cli deep-research-hard-gate `
  --task-id alias_reformulation_chain `
  --max-tasks 1 `
  --timeout-seconds 900 `
  --down-after
```

Full hard gate:

```powershell
uv run python -m wikipediarag.cli deep-research-hard-gate `
  --timeout-seconds 900 `
  --down-after
```

В отчёт включены только safe metadata и ссылки на полные артефакты. Сырые provider payload, секреты, object keys и raw document storage keys намеренно не копируются в этот документ.

## 16. Фактические ответы и terminal reports

### Alias run: что реально было найдено

В partial report были ACL-visible claims:

```text
Project Lantern -> LTN-42
RB-17 -> Night Harbor
Night Harbor -> team Borealis
Night Harbor incident readiness -> on-call 24/7, escalation 15 minutes
```

Это подтверждается тремя fixture markers:

```text
DR_HARD_LANTERN_ALIAS
DR_HARD_LANTERN_RUNBOOK
DR_HARD_LANTERN_OWNER
```

Но публичный final report для run `d157cf80-3edc-4629-bd0c-e3c67809e7c8` имел:

```text
Status: completed
Coverage: 5/9
Stop reason: no_progress_saturation
Terminal mode: partial
Synthesis: null
```

То есть данные для ответа были, но controller не довёл все required questions до terminal coverage и не сформировал confident synthesis.

### Policy exception run: evidence был правильным, answerability была неправильной

Публичный partial report содержал:

```text
North-7 telemetry override: 90 days until 2026-12-31
Global telemetry default: 30 days
Aster-North: region North-7, data class telemetry
```

Но run завершился:

```text
Status: failed
Error code: ProgrammingError
Coverage: 0/7
Tool result: UNANSWERABLE
Reason: no_required_parts_covered
Synthesis: null
```

Это важное расхождение: retrieval returned the correct evidence, but the planner-produced query prevented the answerability gate from recognizing the required parts.

### Full matrix baseline

В full hard report `20260805T182753Z`:

| Fixture | Result | Metrics/error |
|---|---|---|
| `alias_reformulation_chain` | failed | coverage `0.25`, evidence recall `0.667`, 1 completed tool call |
| `policy_exception_bridge` | failed | run status `failed`, evidence recall `1.0`, `ProgrammingError` |
| `contradiction_after_bridge` | not completed | `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED` |
| `finance_alias_chain` | not completed | `DEEP_RESEARCH_SUITE_DEADLINE_EXHAUSTED` |

Так выглядит полный путь отказа: первые ошибки planner/query увеличили latency и снизили progress, затем общий suite deadline не дал следующим fixtures получить отдельный stage-level результат.
