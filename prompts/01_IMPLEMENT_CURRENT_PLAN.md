Реализуй утверждённый `docs/exec-plans/00-bootstrap-and-foundation-slice.md` строго milestone за milestone.

Правила:
- соблюдай AGENTS.md;
- не расширяй scope;
- после каждого milestone запускай его validation commands;
- исправляй обычные ошибки сборки, типов и тестов до продолжения;
- обновляй Progress, Discoveries и Decision log в ExecPlan;
- обновляй docs/STATUS.md фактическими командами и exit codes;
- mocks допустимы только в тестовом/local demo provider profile;
- не используй реальный OPENROUTER_API_KEY без необходимости;
- не выполняй destructive cleanup;
- не отмечай plan завершённым при красных обязательных проверках.

В конце:
1. запусти полный набор проверок плана;
2. проведи self-review diff по правилам AGENTS.md;
3. исправь найденные high/medium проблемы;
4. выдай краткий отчёт: результат, изменённые файлы, команды и exit codes, demo, риски и следующий plan.
