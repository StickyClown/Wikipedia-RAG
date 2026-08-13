# Codex Repository Instructions

These rules are for coding agents working in this repository. Product overview,
current status and architecture details live elsewhere:

- [README.md](README.md) - product introduction, quick start and documentation map.
- [docs/STATUS.md](docs/STATUS.md) - active goal, validation, blockers and next task.

Before implementation, briefly state which of these files were read and the active
goal from [docs/STATUS.md](docs/STATUS.md).

## Work Scope

- Work on one approved task at a time.
- Do not expand scope because adjacent functionality looks useful.
- Do not replace approved components with similar libraries without an explicit decision.
- Do not add GraphRAG, multi-agent swarm, ColBERT, learned sparse retrieval or proposition indexing without a separate approved research plan.
- Do not create synchronous ingestion of large files inside an HTTP request.
- Business code must use the Model Gateway contract for model calls.
- The project target is fully local/private model usage. OpenRouter-backed Qwen
  aliases in `config/models.yaml` are a development/proxy simulation of local
  model behavior until the local runtime is ready; business code must still call
  only Model Gateway aliases and must not call OpenRouter or any provider
  directly.
- Architecture invariants are maintained in [docs/architecture.md](docs/architecture.md).

## Work Protocol

1. Inspect current code, tests and the relevant compact docs before editing.
2. Implement the smallest complete behavior for the approved task.
3. Add or update deterministic tests for success and important failure paths.
4. Run the relevant stable checks for the changed surface.
5. Review the diff for security, tenancy, errors, migrations, observability and accidental scope growth.
6. Update [docs/STATUS.md](docs/STATUS.md) when the task changes project state.
7. Do not claim a command passed unless it was run and exited successfully.
8. For architecture and refactoring work, trace each critical decision as
   `code → contract → owner → boundary → executable invariant`.
   Cite executable code and its verification; label unproven ownership or
   enforcement as `UNCLEAR` or `NOT ENFORCED`. Documentation never substitutes
   for code evidence.

Use stable Make targets when available. If `make` is unavailable, run the
equivalent `uv`, `pnpm` or `docker compose` commands and record the exact
commands and exit codes.

## Выбор проверки по изменённому договору

- Поиск: проверка текущего поведения, HTTP-путь и экран, если функция видна пользователю.
- Загрузка: загрузка → работник → публикация → поиск.
- Доступ: разрешённый и запрещённый путь через открытую границу.
- Шлюз или настройки модели: предметная операция → настоящий шлюз → имитационный поставщик → наблюдаемая фактическая конфигурация.
- Только отображение: проверка типов, сборка и выбранный путь Playwright.
- Не запускать несвязанные длительные проверки без анализа зависимостей.

## Safety Rules

- Do not use destructive Git, Docker volume, database or index commands without explicit user approval.
- Do not delete or rewrite existing migrations after commit; add a new migration.
- Do not suppress type or lint errors broadly.
- Do not commit `.env`, keys, passwords, model files, ZIM snapshots, uploads, generated indices or large evaluation artifacts.
- Do not introduce secrets, prompts, provider payloads, raw document contents or storage object keys into normal logs.
- Do not trust client-supplied tenant, user, group, filter or object-prefix authority.
- Preserve tenant and knowledge-base isolation on every persistent entity, search document and retrieval path.
- Mocks are allowed only in tests and explicit local demo profiles.

## Long-Running Commands

- Do not run long eval, ingestion, release-gate, provider generation or benchmark commands silently.
- Commands expected to take more than a few minutes must expose live progress, status polling or append-only logs with stage, processed/total, last update and failure state.
- Use bounded `--batch-size` or `--concurrency` when supported.
- If interrupted or timed out, inspect remaining processes and artifact state before continuing.

## Definition Of Done

A task is done only when:

- requested behavior exists end-to-end;
- success and important failure paths are tested;
- lint, format, type checks and relevant tests pass or failures are explicitly documented as blockers;
- public contracts and migrations are documented when changed;
- no secrets, unbounded retries, cross-tenant paths or silent failures were introduced;
- [docs/STATUS.md](docs/STATUS.md) reflects reality;
- the final response reports files changed, commands run, results, risks and remaining work.

## Final Report

Report:

- files read;
- files created and changed;
- checks run with results;
- implementation notes and important corrected assumptions;
- risks, blockers and remaining work.

## Review Priority

Prioritize findings in this order:

1. cross-tenant data exposure;
2. secret leakage or unsafe parsing;
3. data loss, non-idempotent jobs and broken migrations;
4. incorrect citations or provenance;
5. unbounded loops, retries or concurrency;
6. API compatibility and error semantics;
7. observability gaps;
8. performance regressions;
9. maintainability.
