# Исходный замер P0.1 от 15 августа 2026 года

**Статус: закрыт.** P0.1 подтверждает безопасный воспроизводимый workflow
качества и совместимости. Он не утверждает производственное качество поиска,
точность evidence-чанков, цитат или ответов.

## Короткий итог

Технический полный прогон выполнен на локальном безопасном наборе
`p0-search-quality-v1` через имитационный поставщик моделей.

| Часть | Вопросы | Поиск | Ответ | Ошибки выполнения |
| --- | ---: | ---: | ---: | ---: |
| `dev` | 44/44 | 44/44 | 44/44 | 0 |
| `test` | 176/176 | 176/176 | 176/176 | 0 |
| Всего | 220/220 | 220/220 | 220/220 | 0 |

Состояние отчёта: `incompatible_results`. Это означает, что пять языковых
баз имеют разные номера индекса. Поэтому общий средний показатель намеренно не
считался; в JSON отчёте результаты разделены по номеру индекса, языку, виду
вопроса и виду материала.

## Что закреплено

- Набор: `p0-search-quality-v1`.
- Контрольная сумма набора:
  `fc2c379f01b60347988b746b944d55ab269c409b7cd277a3ac33dbdfa1bab1b0`.
- Контрольная сумма описания материалов:
  `a8a381fa4dc34dd49474b86ea45594ec3b0977774f7a6d2b1e8c6c37781499b4`.
- Загрузка: `p01-ingest-20260815-mock`.
- Замер: `p01-quality-20260815-mock-v3`.
- Профиль поиска: `upload_mock`.
- Модель: локальный имитационный поставщик.
- На момент запуска API и шлюз модели отвечали `ok`; очередь исправления
  поисковой проекции была пустой (`pending=0`).

Загрузка обработала 20 из 20 файлов и создала отдельные базы для `ru`, `en`,
`uk`, `de` и `ko`. Список баз и соответствие каждого файла документу записаны
в файле состояния загрузки.

## Файлы результата

- [сводный JSON](../artifacts/eval/quality/p0-search-quality-v1/latest.json);
- [состояние запуска](../artifacts/eval/quality/p0-search-quality-v1/p01-quality-20260815-mock-v3/status.json);
- [поиск по вопросам](../artifacts/eval/quality/p0-search-quality-v1/p01-quality-20260815-mock-v3/retrieval.jsonl);
- [ответы по вопросам](../artifacts/eval/quality/p0-search-quality-v1/p01-quality-20260815-mock-v3/answer.jsonl);
- [состояние загрузки](../eval-corpus/p0-search-quality-v1/ingestion/p01-ingest-20260815-mock.json).

## Как повторить

```powershell
$env:MODEL_PROVIDER='mock'
$env:RETRIEVAL_PROFILE='upload_mock'
$env:DOCUMENT_PARSER_SERVICES_REQUIRED='true'
docker compose up -d --build postgres redis minio opensearch mock-provider model-gateway metadata-service xberg docling api worker
uv run python -m wikipediarag.cli eval-quality-ingest --corpus-dir eval-corpus/p0-search-quality-v1 --api http://localhost:8000 --run-id <новый-id>
uv run python -m wikipediarag.cli eval-quality-run --corpus-dir eval-corpus/p0-search-quality-v1 --api http://localhost:8000 --split dev --run-id <новый-id>
uv run python -m wikipediarag.cli eval-quality-run --corpus-dir eval-corpus/p0-search-quality-v1 --api http://localhost:8000 --split test --resume-run-id <тот-же-id>
uv run python -m wikipediarag.cli eval-quality-report --corpus-dir eval-corpus/p0-search-quality-v1
```

## Ограничения замера

Этот прогон подтверждает полный путь подготовки, загрузки, поиска, ответа,
сохранения шагов и построения отчёта. Он не является производственным
показателем качества: материалы в текущем наборе безопасные и вымышленные,
а эталонные номера фрагментов ещё не связаны с внутренними номерами фрагментов
после загрузки. Поэтому низкие значения качества в JSON нужно считать
диагностическим исходным уровнем, а не доказательством ухудшения рабочего
поиска.

Попытка передать все пять баз одним запросом (`p01-quality-20260815-mock-v4`)
остановилась с безопасной причиной `KeyError: stage` в рабочем много-базовом
пути. Она не входит в полный замер и оставлена только как отдельный
диагностический артефакт.

Отдельный real RRNCB baseline зафиксирован как document-level reference в
[`p0-search-quality-v2.md`](p0-search-quality-v2.md). Он не меняет границы
P0.1: без reviewed section/chunk anchors любые chunk-метрики для RRNCB являются
неприменимыми, а не нулевыми результатами.
