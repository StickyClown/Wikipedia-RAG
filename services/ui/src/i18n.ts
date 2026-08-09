import { useEffect, useState } from "react";

export type Locale = "en" | "ru";

const STORAGE_KEY = "wikipediarag.locale";

const translations: Record<Locale, Record<string, string>> = {
  en: {
    product_tagline: "Local Russian Wikipedia RAG MVP",
    ready_checking: "checking",
    ready_ok: "ready",
    ready_offline: "offline",
    sign_in: "Sign in",
    username: "Username",
    password: "Password",
    local: "Local",
    oidc: "OIDC",
    logout: "Logout",
    primary_kb: "Primary knowledge base",
    retrieval_scope: "Retrieval scope",
    scope_summary: "{count} KB selected",
    new_kb_name: "New KB name",
    create: "Create",
    chat: "Chat",
    search: "Search",
    research: "Research",
    knowledge_base: "Knowledge Base",
    wikipedia_import: "Wikipedia import",
    import_limit: "Pages to import",
    start_import: "Start import",
    upload: "Upload documents",
    choose_files: "Choose files",
    sources: "External sources",
    add_source: "Add source",
    refresh: "Refresh",
    advanced_configuration: "Advanced configuration",
    credentials: "Credentials JSON",
    config: "Config JSON",
    connector: "Connector",
    source_name: "Name",
    refresh_seconds: "Refresh seconds",
    visibility: "Visibility",
    no_sources: "No external sources yet",
    last_sync: "Last sync",
    next_sync: "Next sync",
    health: "Health",
    sync: "Sync",
    full_sync: "Full sync",
    disable: "Disable",
    enable: "Enable",
    apply_access: "Apply access",
    permissions: "Permissions",
    search_documents: "Search documents",
    filters: "Filters",
    document_type: "Document type",
    language: "Language",
    date_from: "Date from",
    date_to: "Date to",
    source: "Source",
    no_results: "No results",
    deep_research: "Deep Research",
    research_topic: "Research topic for the selected knowledge base",
    quick_run: "Quick run",
    create_plan: "Create plan",
    plans: "Plans",
    runs: "Runs",
    no_plans: "No research plans yet",
    no_runs: "No research runs yet",
    save_plan: "Save plan",
    approve_run: "Approve and run",
    pause: "Pause",
    resume: "Resume",
    cancel: "Cancel",
    debug: "Debug",
    coverage: "Coverage",
    evidence_memory: "Evidence memory",
    latest_reflection: "Latest reflection",
    failure_taxonomy: "Failure taxonomy",
    report: "Report",
    ask: "Ask",
    advanced_retrieval: "Advanced retrieval settings",
    profile: "Profile",
    top_k: "Top K",
    bm25: "BM25",
    dense: "Dense",
    rerank: "Rerank",
    fusion: "Fusion",
    parent_expansion: "Parent expansion",
    extended_search: "Extended Search",
    mode: "Mode",
    normal: "Normal",
    extended: "Extended",
    answer: "Answer",
    close: "Close",
    save_document_access: "Save document access",
    document_search_placeholder: "Search inside this document",
    loading_context: "Loading context…",
    no_text_context: "No text context",
    sections: "sections",
    questions: "questions",
    episodes: "episodes",
    single_kb: "Single KB",
    not_selected: "not selected",
    plan_topic: "Plan topic",
    plan_questions: "Plan questions",
    notes: "Notes",
    partial_terminal: "partial completion",
    chat_waiting: "Generating an answer…",
    chat_empty: "Enter a question before asking.",
    chat_stream_unavailable: "The answer stream is unavailable. Try again.",
    chat_incomplete: "The answer stream ended before completion. Try again.",
    chat_stop: "Stop",
    chat_stopped: "Answer generation stopped.",
    chat_failed: "The answer could not be generated. Try again.",
    search_not_ready: "This knowledge base is not ready for search yet.",
    research_not_ready: "This knowledge base is not ready for research yet.",
    request_failed: "The request could not be completed.",
    conflict: "This action is no longer available for the current state.",
    request_validation_failed: "Check the entered values and try again.",
    error_code: "Error code",
    reset_app: "Reload app",
    status: "Status",
    loading: "Loading…",
    tenant: "Tenant",
    restricted: "Restricted",
    kb_visibility: "KB",
    tooltip_readiness: "API readiness reported by the server.",
    tooltip_scope:
      "Choose the knowledge bases used for Chat and Search retrieval.",
    tooltip_profile:
      "Retrieval profile controls the pipeline used for this query.",
    tooltip_top_k:
      "Maximum number of retrieval candidates passed to post-processing.",
    tooltip_fusion: "How sparse and dense retrieval candidates are combined.",
    tooltip_parent: "Whether related parent chunks are expanded around a hit.",
    tooltip_extended:
      "Controls bounded follow-up retrieval when the first answer is incomplete.",
    tooltip_health: "Check connector availability without starting a sync.",
    tooltip_sync: "Run an incremental sync for changed source documents.",
    tooltip_full_sync:
      "Re-read the complete source and reconcile deleted documents.",
    tooltip_debug:
      "Open retrieval stages and candidate movements for this query.",
    tooltip_reload: "Reload the application and restore the current session.",
  },
  ru: {
    product_tagline: "Локальная RAG-платформа по русской Википедии",
    ready_checking: "проверка",
    ready_ok: "готово",
    ready_offline: "нет связи",
    sign_in: "Вход",
    username: "Имя пользователя",
    password: "Пароль",
    local: "Локальный",
    oidc: "OIDC",
    logout: "Выйти",
    primary_kb: "Основная база знаний",
    retrieval_scope: "Область поиска",
    scope_summary: "Выбрано баз: {count}",
    new_kb_name: "Название новой базы",
    create: "Создать",
    chat: "Чат",
    search: "Поиск",
    research: "Исследование",
    knowledge_base: "База знаний",
    wikipedia_import: "Импорт Википедии",
    import_limit: "Страниц к импорту",
    start_import: "Начать импорт",
    upload: "Загрузка документов",
    choose_files: "Выбрать файлы",
    sources: "Внешние источники",
    add_source: "Добавить источник",
    refresh: "Обновить",
    advanced_configuration: "Расширенная конфигурация",
    credentials: "JSON учётных данных",
    config: "JSON конфигурации",
    connector: "Коннектор",
    source_name: "Название",
    refresh_seconds: "Интервал обновления, сек.",
    visibility: "Видимость",
    no_sources: "Внешних источников пока нет",
    last_sync: "Последняя синхронизация",
    next_sync: "Следующая синхронизация",
    health: "Проверить",
    sync: "Синхронизировать",
    full_sync: "Полная синхронизация",
    disable: "Отключить",
    enable: "Включить",
    apply_access: "Применить доступ",
    permissions: "Права доступа",
    search_documents: "Поиск по документам",
    filters: "Фильтры",
    document_type: "Тип документа",
    language: "Язык",
    date_from: "Дата от",
    date_to: "Дата до",
    source: "Источник",
    no_results: "Ничего не найдено",
    deep_research: "Deep Research",
    research_topic: "Тема исследования по выбранной базе знаний",
    quick_run: "Быстрый запуск",
    create_plan: "Создать план",
    plans: "Планы",
    runs: "Запуски",
    no_plans: "Планов исследования пока нет",
    no_runs: "Запусков исследования пока нет",
    save_plan: "Сохранить план",
    approve_run: "Утвердить и запустить",
    pause: "Пауза",
    resume: "Продолжить",
    cancel: "Отменить",
    debug: "Отладка",
    coverage: "Покрытие",
    evidence_memory: "Память свидетельств",
    latest_reflection: "Последняя рефлексия",
    failure_taxonomy: "Классификация ошибок",
    report: "Отчёт",
    ask: "Спросить",
    advanced_retrieval: "Расширенные настройки поиска",
    profile: "Профиль",
    top_k: "Top K",
    bm25: "BM25",
    dense: "Плотный поиск",
    rerank: "Переранжирование",
    fusion: "Слияние",
    parent_expansion: "Расширение родителями",
    extended_search: "Расширенный поиск",
    mode: "Режим",
    normal: "Обычный",
    extended: "Расширенный",
    answer: "Ответ",
    close: "Закрыть",
    save_document_access: "Сохранить доступ к документу",
    document_search_placeholder: "Поиск внутри этого документа",
    loading_context: "Загрузка контекста…",
    no_text_context: "Текстовый контекст отсутствует",
    sections: "разделов",
    questions: "вопросов",
    episodes: "эпизодов",
    single_kb: "Одна база",
    not_selected: "не выбрана",
    plan_topic: "Тема плана",
    plan_questions: "Вопросы плана",
    notes: "Заметки",
    partial_terminal: "завершён частично",
    chat_waiting: "Генерируем ответ…",
    chat_empty: "Введите вопрос перед отправкой.",
    chat_stream_unavailable: "Поток ответа недоступен. Повторите попытку.",
    chat_incomplete:
      "Поток ответа завершился без финального события. Повторите попытку.",
    chat_stop: "Остановить",
    chat_stopped: "Генерация ответа остановлена.",
    chat_failed: "Не удалось сгенерировать ответ. Повторите попытку.",
    search_not_ready: "Эта база знаний ещё не готова к поиску.",
    research_not_ready: "Эта база знаний ещё не готова к исследованию.",
    request_failed: "Не удалось выполнить запрос.",
    conflict: "Это действие больше недоступно в текущем состоянии.",
    request_validation_failed:
      "Проверьте введённые значения и повторите попытку.",
    error_code: "Код ошибки",
    reset_app: "Перезагрузить приложение",
    status: "Статус",
    loading: "Загрузка…",
    tenant: "Тенант",
    restricted: "Ограниченный",
    kb_visibility: "База",
    tooltip_readiness: "Состояние готовности API, полученное от сервера.",
    tooltip_scope: "Выберите базы знаний для поиска в чате и обычном поиске.",
    tooltip_profile:
      "Профиль поиска определяет используемый retrieval pipeline.",
    tooltip_top_k: "Максимальное число кандидатов для последующей обработки.",
    tooltip_fusion: "Способ объединения разреженного и плотного поиска.",
    tooltip_parent: "Нужно ли добавлять родительские чанки вокруг найденного.",
    tooltip_extended:
      "Управляет ограниченным дополнительным поиском при неполном ответе.",
    tooltip_health:
      "Проверить доступность коннектора без запуска синхронизации.",
    tooltip_sync:
      "Запустить инкрементальную синхронизацию изменённых документов.",
    tooltip_full_sync:
      "Полностью перечитать источник и учесть удалённые документы.",
    tooltip_debug:
      "Открыть этапы retrieval и перемещения кандидатов для запроса.",
    tooltip_reload: "Перезагрузить приложение и восстановить текущую сессию.",
  },
};

function detectLocale(): Locale {
  if (typeof window === "undefined") return "en";
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "en" || saved === "ru") return saved;
  } catch {
    // Storage can be disabled by a browser policy; fall back to navigator.
  }
  return navigator.language.toLowerCase().startsWith("ru") ? "ru" : "en";
}

export function useLocale() {
  const [locale, setLocaleState] = useState<Locale>(detectLocale);

  useEffect(() => {
    document.documentElement.lang = locale;
    try {
      window.localStorage.setItem(STORAGE_KEY, locale);
    } catch {
      // Locale remains available for the current session when storage is blocked.
    }
  }, [locale]);

  function setLocale(next: Locale) {
    setLocaleState(next);
  }

  function t(key: string, fallback?: string) {
    const template =
      translations[locale][key] ?? translations.en[key] ?? fallback ?? key;
    return template;
  }

  return { locale, setLocale, t };
}

export function interpolate(
  template: string,
  values: Record<string, string | number>,
) {
  return template.replace(/\{(\w+)\}/g, (_, key: string) =>
    String(values[key] ?? ""),
  );
}
