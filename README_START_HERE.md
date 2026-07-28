# WikipediaRag local MVP

Локальный Docker-first MVP для RAG по русской Wikipedia. Реальный демонстрационный путь использует один русский Wikipedia ZIM: Kiwix показывает полный архив, а worker через `python-libzim` индексирует первые 10 000 валидных canonical article entries. Wikimedia XML `pages-articles` bzip2 multistream adapter сохранён как regression/local fallback.

## Architecture and LLM handoff

Этот файл является operator runbook: здесь собраны setup, import и validation commands. Для передачи проекта инженеру или LLM-агенту сначала откройте [README.md](README.md), где описаны бизнес-логика, runtime контуры, retrieval pipeline, multi-hop ограничения, evaluation snapshot и backlog роста. Архитектурный источник истины остаётся [docs/architecture.md](docs/architecture.md).

## Что входит

- FastAPI backend, worker и Model Gateway на Python 3.12.
- React + Vite + TypeScript web UI.
- PostgreSQL, Valkey/Redis, MinIO, OpenSearch, Kiwix и OpenTelemetry Collector через Docker Compose.
- Mock model provider для локальной демонстрации и тестов без внешнего ключа.
- OpenRouter provider через Model Gateway для `sota_mvp`; mock provider остаётся для `test_mock`.
- Optional llama.cpp Docker profile в `compose.llamacpp.yaml`.
- Импорт ZIM с checkpoint, progress, cancel/resume и restart recovery; XML import path сохранён.
- Section-aware chunking, deterministic IDs, BM25 + dense retrieval, RRF, rerank, citations, insufficient-evidence mode, retrieval debugger, upload UTF-8 documents, bounded Extended Search и mini eval.

## Требования Windows

1. Docker Desktop с включенным WSL2 backend.
2. Git.
3. Python 3.12 и `uv`.
4. Node.js 22.14+ и `pnpm`.
5. GNU Make в WSL или другой совместимый `make`.

В текущем Windows окружении Codex `make` доступен через WSL, но `uv`/`pnpm` доступны как Windows-команды. На чистой машине проще установить `uv` и `pnpm` внутри WSL, затем запускать команды из WSL в каталоге репозитория.

## Данные Wikipedia ZIM

Для реального demo-MVP положите один настоящий русский Wikipedia ZIM в каталог:

```text
zim/*.zim
```

Один и тот же каталог монтируется read-only:

- в Kiwix как `/data`;
- в API/worker как `/zim`.

Kiwix обслуживает полный архив на http://localhost:8083. RAG импортирует только первые 10 000 canonical non-redirect article pages. Source links строятся из `KIWIX_PUBLIC_BASE_URL`, имени книги и точного `zim_entry_path`, полученного из libzim.

## XML fallback data

Большие dump-файлы являются локальными data assets и исключены из Git:

```text
zip/ruwiki-20260701-pages-articles-multistream.xml.bz2
zip/ruwiki-20260701-pages-articles-multistream-index.txt.bz2
```

Проверенные свойства локального dump:

- XML archive size: `6,135,514,301` bytes.
- Index archive size: `65,980,533` bytes.
- Оба файла имеют bzip2 signature `BZh9`.
- Index UTF-8, формат строк `offset:page_id:title`.
- Offsets являются monotonic non-decreasing; повторяющиеся offsets означают страницы в одном bzip2 stream.

## Первый запуск

```bash
cp .env.example .env
make dev-up
```

URL:

- Web UI: http://localhost:5173
- API health: http://localhost:8000/health
- API readiness: http://localhost:8000/ready
- Model Gateway: http://localhost:8081
- Mock provider: http://localhost:8082
- Kiwix: http://localhost:8083
- MinIO console: http://localhost:9001
- OpenSearch: http://localhost:9200

## ZIM demo import

```bash
make import-zim-small WIKI_LIMIT=10000
```

Эта команда создаёт async job, потоково читает `/zim/*.zim`, пропускает assets/metadata/service entries, не считает redirects в лимит, пишет checkpoint и индексирует chunks в profile-specific OpenSearch index version.

## XML fallback import

Малый development-импорт:

```bash
make import-wiki-small WIKI_LIMIT=10000
```

Для быстрой проверки можно указать меньший лимит:

```bash
make import-wiki-small WIKI_LIMIT=1000
```

Полный resumable импорт всего dump:

```bash
make import-wiki-full
```

Job progress доступен через UI и API:

```bash
curl http://localhost:8000/api/v1/ingestion-jobs/<job_id>
```

Cancel/resume:

```bash
curl -X POST http://localhost:8000/api/v1/ingestion-jobs/<job_id>:cancel
curl -X POST http://localhost:8000/api/v1/ingestion-jobs/<job_id>:resume
```

## Проверки

Одна команда полной проверки:

```bash
make check-all
```

Отдельные команды:

```bash
make lint
make format-check
make typecheck
make test-unit
make test-integration
make test-e2e
make smoke
make eval
make smoke-models PROVIDER=mock
```

UI checks:

```bash
cd services/ui
pnpm lint
pnpm typecheck
pnpm format:check
pnpm build
```

Model Gateway smoke:

```bash
make smoke-models PROVIDER=mock
make smoke-models PROVIDER=openrouter
make demo-release-gate
```

## Демонстрационный вопрос

После малого импорта откройте http://localhost:5173 и задайте:

```text
Что такое Россия?
```

Ожидается ответ на русском с citation IDs вида `[S1]`, списком clickable sources и retrieval debugger stages: profile/query, BM25, dense, RRF, rerank, policy/context, harness при Extended Search.

## OpenRouter

Для реального `sota_mvp` demo добавьте ключ только в локальный `.env`:

```env
MODEL_PROVIDER=openrouter
RETRIEVAL_PROFILE=sota_mvp
OPENROUTER_API_KEY=...
ZIM_DIR=/zim
KIWIX_PUBLIC_BASE_URL=http://localhost:8083
```

`sota_mvp` не делает silent fallback на mock. Если OpenRouter key/model/embedding/rerank smoke fails, readiness/query должны падать безопасно. Не храните ключи в `openrouter_key.txt`, Git или документации.

## llama.cpp profile

GPU/GGUF не требуются для MVP. Подготовлен optional profile:

```bash
docker compose -f compose.yaml -f compose.llamacpp.yaml --profile llamacpp up -d
make smoke-models PROVIDER=llamacpp
```

Перед реальным запуском llama.cpp нужны локальные model artifacts, лицензии, checksums и hardware decision из `docs/DECISIONS_REQUIRED.md`.

## Остановка

```bash
make down
```

Команда останавливает контейнеры без удаления volumes.
