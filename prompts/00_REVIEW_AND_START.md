Работай в Plan mode. Пока не изменяй файлы приложения и не создавай production-код.

Прочитай полностью:
- AGENTS.md
- SPEC.md
- .agent/PLANS.md
- docs/architecture.md
- docs/DECISIONS_REQUIRED.md
- docs/contracts/*
- docs/decisions/*
- docs/quality/*
- docs/STATUS.md
- docs/exec-plans/00-bootstrap-and-foundation-slice.md

Затем:
1. Сопоставь ExecPlan 00 с архитектурой и управляющими правилами.
2. Найди противоречия, неисполняемые acceptance criteria и недостающие prerequisites.
3. Разрешай мелкие неоднозначности консервативно и запиши решения в Decision log.
4. Для материальных архитектурных развилок предложи ADR, но не меняй baseline без моего решения.
5. Уточни ExecPlan так, чтобы каждый milestone был самостоятельно проверяемым.
6. Не реализуй код.

В финале выдай:
- результат проверки: READY или BLOCKED;
- список внесённых только в ExecPlan/документацию изменений;
- точные команды, которые будут выполняться в первом milestone;
- вопросы владельцу только для действительно блокирующих решений.
