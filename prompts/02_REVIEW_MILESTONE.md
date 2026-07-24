Проведи read-only review текущих uncommitted changes относительно:
- AGENTS.md;
- активного ExecPlan;
- API/DB/domain contracts;
- security checklist;
- Definition of Done.

Сначала изучи diff и тесты. Не исправляй код.

Приоритизируй:
1. cross-tenant exposure;
2. secret leakage/unsafe parsing;
3. data loss, migrations and idempotency;
4. citation/provenance correctness;
5. unbounded retries/loops/concurrency;
6. API/error compatibility;
7. missing tests and observability;
8. performance/maintainability.

Для каждого finding укажи severity, файл/строку, сценарий отказа и минимальное исправление. Если blocking findings отсутствуют, явно напиши APPROVED и перечисли оставшиеся test gaps.
