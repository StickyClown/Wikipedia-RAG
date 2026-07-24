# Production RAG Platform — Codex starter pack

Этот каталог содержит не готовое приложение, а **управляющий пакет для его последовательной реализации в Codex**.

## Что уже зафиксировано

- продукт: локальная production-ready RAG-платформа;
- первый источник: Wikipedia ZIM;
- backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, asyncpg;
- frontend: React, Vite, TypeScript;
- инфраструктура MVP: Docker Compose, PostgreSQL, OpenSearch, MinIO, Redis/Valkey;
- очередь MVP: Dramatiq + Redis/Valkey;
- модели сейчас: OpenRouter через внутренний Model Gateway;
- локальные модели позже: три отдельных `llama-server`;
- обычный retrieval: BM25 + dense + RRF + reranking;
- agent mode: только по сигналу сложности и нехватки доказательств.

Все эти решения являются стартовыми defaults. Изменять их можно только отдельным ADR.

## Состав пакета

1. `AGENTS.md` — обязательные правила Codex для всего репозитория.
2. `SPEC.md` — стабильная спецификация продукта.
3. `.agent/PLANS.md` — формат исполняемых планов.
4. `.codex/config.toml` — безопасные repo-level настройки Codex.
5. `docs/architecture.md` — исходная подробная архитектура.
6. `docs/decisions/` — принятые и ожидающие решения.
7. `docs/contracts/` — API, БД и системные инварианты.
8. `docs/quality/` — Definition of Done, security и evaluation gates.
9. `docs/exec-plans/` — последовательность реализации.
10. `prompts/` — готовые команды для Codex.
11. `.env.example` — перечень секретов и настроек, но без реальных значений.

## Рекомендуемый способ запуска

### Вариант A: Codex CLI

#### 1. Установите Codex

macOS/Linux:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
```

Альтернативы:

```bash
npm install -g @openai/codex
# или на macOS
brew install --cask codex
```

#### 2. Создайте рабочий Git-репозиторий

```bash
unzip rag_codex_starter_pack.zip
mv rag_codex_starter_pack rag-platform
cd rag-platform
git init -b main
git add .
git commit -m "docs: initialize Codex implementation pack"
```

Windows PowerShell:

```powershell
Expand-Archive .\rag_codex_starter_pack.zip -DestinationPath .
Rename-Item .\rag_codex_starter_pack rag-platform
Set-Location .\rag-platform
git init -b main
git add .
git commit -m "docs: initialize Codex implementation pack"
```

#### 3. Запустите Codex и войдите через ChatGPT

```bash
codex
```

При первом запуске выберите **Sign in with ChatGPT**.

#### 4. Проверьте, что инструкции прочитаны

В интерактивной сессии отправьте:

```text
Перечисли все AGENTS.md и другие управляющие документы, которые ты загрузил. Ничего не изменяй.
```

Codex должен назвать корневой `AGENTS.md`, `SPEC.md`, `.agent/PLANS.md` и текущий ExecPlan.

#### 5. Запустите первый план

Включите Plan mode командой `/plan` и вставьте содержимое:

```text
prompts/00_REVIEW_AND_START.md
```

Сначала Codex должен проверить план и перечислить блокирующие противоречия. Если критических противоречий нет, дайте вторую команду из `prompts/01_IMPLEMENT_CURRENT_PLAN.md`.

### Вариант B: приложение Codex / режим Codex в ChatGPT desktop

1. Создайте локальный каталог `rag-platform` из ZIP.
2. Откройте каталог как новый Codex project.
3. Убедитесь, что primary folder указывает на корень, где лежит `AGENTS.md`.
4. Откройте новый thread, включите `/plan` и вставьте `prompts/00_REVIEW_AND_START.md`.
5. После проверки плана создайте отдельный thread для реализации первого milestone.

## Режим разрешений

Для первого запуска используйте безопасный режим:

```bash
codex --cd . --sandbox workspace-write --ask-for-approval on-request
```

Не применяйте `--dangerously-bypass-approvals-and-sandbox` на основной машине. Полный доступ допустим только внутри отдельной одноразовой VM/контейнера без секретов и важных файлов.

## Порядок работы

- Одна Codex-сессия — один понятный milestone или один review.
- Не запускайте сразу все планы.
- После каждого milestone проверяйте diff и создавайте commit.
- Перед следующим планом запускайте `/review` или prompt `prompts/02_REVIEW_MILESTONE.md`.
- Реальные ключи храните только в `.env`, никогда не в Git.
- Phase 3 нельзя принимать без сведений о GPU/RAM из `docs/DECISIONS_REQUIRED.md`.

## Первый полезный результат

Первый ExecPlan заканчивается узким вертикальным срезом:

```text
POST /api/v1/chat
→ FastAPI
→ Model Gateway
→ mock/OpenRouter-compatible provider
→ SSE stream
→ query_run в PostgreSQL
→ OpenTelemetry trace
```

Retrieval, Wikipedia и UI в этот первый срез не входят.
