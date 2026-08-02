import {
  BookOpen,
  Bug,
  Database,
  ExternalLink,
  FileUp,
  KeyRound,
  LogIn,
  LogOut,
  MessageSquare,
  Play,
  Plug,
  RotateCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

const SOURCE_KINDS = [
  "confluence_dc",
  "jira_dc",
  "gitlab_self_managed",
  "kiwix_zim",
  "local_folder",
  "internal_crawler",
  "sunduk_mock",
  "docsmart_mock",
] as const;

type SourceKind = (typeof SOURCE_KINDS)[number];

type SourceTemplate = {
  label: string;
  name: string;
  config: Record<string, unknown>;
  credentials: Record<string, unknown>;
};

const SOURCE_TEMPLATES: Record<SourceKind, SourceTemplate> = {
  confluence_dc: {
    label: "Confluence DC",
    name: "Confluence DC",
    config: {
      base_url: "https://confluence.local",
      space: "DOC",
      limit: 25,
    },
    credentials: { username: "", password: "" },
  },
  jira_dc: {
    label: "Jira DC",
    name: "Jira DC",
    config: {
      base_url: "https://jira.local",
      jql: "project = IT ORDER BY updated ASC",
      limit: 25,
      updated_overlap_minutes: 5,
    },
    credentials: { username: "", password: "" },
  },
  gitlab_self_managed: {
    label: "GitLab Self-Managed",
    name: "GitLab Self-Managed",
    config: {
      base_url: "https://gitlab.local",
      project_id: "123",
      ref: "main",
      path_allowlist: ["README.md", "docs"],
      max_files: 50,
    },
    credentials: { token: "" },
  },
  kiwix_zim: {
    label: "Kiwix/ZIM",
    name: "Kiwix ZIM",
    config: {
      zim_dir: "/zim",
      zim_filename: "wikipedia_ru_all.zim",
    },
    credentials: {},
  },
  local_folder: {
    label: "Local Folder",
    name: "Local Folder",
    config: {
      root_path: "/sources/docs",
      extensions: [".md", ".txt", ".html"],
      max_files: 1000,
    },
    credentials: {},
  },
  internal_crawler: {
    label: "Internal Crawler",
    name: "Internal Crawler",
    config: {
      base_url: "https://intranet.local",
      allowed_domains: ["intranet.local"],
      max_pages: 25,
      max_depth: 2,
      exclude_url_patterns: [],
    },
    credentials: { bearer_token: "" },
  },
  sunduk_mock: {
    label: "Sunduk Mock",
    name: "Sunduk Mock",
    config: {
      query: "резервное копирование",
      filters: { department: "IT" },
      limit: 20,
    },
    credentials: {},
  },
  docsmart_mock: {
    label: "DocSmart Mock",
    name: "DocSmart Mock",
    config: {},
    credentials: {},
  },
};

type Job = {
  id: string;
  status: string;
  progress: {
    pages_imported?: number;
    chunks_indexed?: number;
    pages_seen?: number;
    stage?: string;
    bytes_received?: number;
    documents_total?: number;
    documents_completed?: number;
    documents_failed?: number;
    parser_route?: string;
    chunks_staged?: number;
    chunks_published?: number;
    timings_ms?: Record<string, number>;
  };
  error_code?: string | null;
  error_message?: string | null;
};

type Evidence = {
  evidence_id: string;
  title: string;
  section_path: string[];
  content: string;
  source_url: string;
};

type RetrievalEvent = {
  stage: string;
  payload: unknown;
  event_type?: string;
  created_at?: string;
};

type QueryRunSummary = {
  id?: string;
  mode?: string;
  status?: string;
  input_text?: string;
  answer?: string | null;
  model_alias?: string | null;
  error_code?: string | null;
  trace_id?: string;
  usage?: Record<string, unknown>;
};

type SsePayload = {
  query_run_id: string;
  data: {
    text?: string;
    evidence?: Evidence[];
  };
};

type RetrievalOverrideState = {
  bm25Enabled: boolean;
  denseEnabled: boolean;
  rerankEnabled: boolean;
  fusionMode: "rrf" | "none";
  parentExpansion: "off" | "selective" | "always";
  extendedSearchMode: "off" | "conditional" | "always";
  topK: number;
};

type UploadSessionAccepted = {
  upload_session_id: string;
  upload_url: string;
  expires_at: string;
  required_headers: Record<string, string>;
};

type UploadCompleteResponse = {
  document_id: string;
  document_version_id: string;
  job_id: string;
  status: string;
};

type UploadBatchAccepted = {
  batch_id: string;
  knowledge_base_id: string;
  status: string;
  total_items: number;
  items: Array<
    UploadSessionAccepted & {
      filename: string;
      content_type: string;
      size_bytes: number;
      checksum_sha256: string;
    }
  >;
};

type UploadBatchItemStatus = {
  upload_session_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  checksum_sha256: string;
  status: string;
  upload_completed_at?: string | null;
  document_id?: string | null;
  document_version_id?: string | null;
  job_id?: string | null;
  job_status?: string | null;
  progress?: Job["progress"];
  error_code?: string | null;
  error_message?: string | null;
};

type UploadBatchStatus = {
  batch_id: string;
  knowledge_base_id: string;
  status: string;
  total_items: number;
  completed_items: number;
  failed_items: number;
  cancelled_items: number;
  pending_items: number;
  items: UploadBatchItemStatus[];
};

type UploadItemState = {
  id: string;
  filename: string;
  size_bytes: number;
  status: string;
  upload_session_id?: string;
  document_id?: string | null;
  job_id?: string | null;
  progress?: Job["progress"];
  error_code?: string | null;
  error_message?: string | null;
};

type AuthSession = {
  authenticated: boolean;
  csrf_token?: string | null;
  active_tenant_id?: string | null;
  tenant_role?: string | null;
  user?: {
    id: string;
    username?: string | null;
    display_name?: string | null;
    platform_role: string;
    password_change_required: boolean;
  } | null;
};

type KnowledgeBase = {
  id: string;
  name: string;
};

type SourceResponse = {
  id: string;
  knowledge_base_id: string;
  kind: SourceKind | string;
  name: string;
  status: string;
  config: Record<string, unknown>;
  metadata: Record<string, unknown>;
  refresh_interval_seconds?: number | null;
  last_sync_run_id?: string | null;
  last_sync_status?: string | null;
  last_synced_at?: string | null;
  next_sync_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type SourceListResponse = {
  sources: SourceResponse[];
};

type SourceHealthResponse = {
  source_id: string;
  status: string;
  details: Record<string, unknown>;
};

type SourceSyncResponse = {
  source_id: string;
  run_id: string;
  job_id: string;
  status: string;
};

type SourceSyncRunResponse = {
  id: string;
  source_id: string;
  knowledge_base_id: string;
  mode: string;
  status: string;
  stats: Record<string, unknown>;
  error_code?: string | null;
  error_message?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type DocumentPublicMetadata = {
  id: string;
  title: string;
  filename?: string;
  status?: string;
  parser_route?: string | null;
  parser_name?: string | null;
  parser_version?: string | null;
  content_hash?: string | null;
  normalized_hash?: string | null;
  uploaded_at?: string | null;
  upload_completed_at?: string | null;
  ingested_at?: string | null;
  published_at?: string | null;
  public_metadata?: Record<string, unknown>;
};

type SearchFilters = {
  document_type?: string;
  language?: string;
  date_from?: string;
  date_to?: string;
  source?: string;
};

type SearchResult = {
  chunk_id: string;
  document_id: string;
  document_version_id: string;
  knowledge_base_id: string;
  title: string;
  snippet: string;
  section_path: string[];
  source_url: string;
  source_type: string;
  document_type?: string | null;
  language?: string | null;
  document_date?: string | null;
  locator?: Record<string, unknown> | null;
  score: number;
  ranks: Record<string, number>;
  highlights?: { field: string; fragments: string[] }[];
};

type SearchFacet = {
  field: string;
  buckets: { value: string; count: number }[];
};

type SearchResponse = {
  results: SearchResult[];
  limit: number;
  offset: number;
  has_more: boolean;
  next_cursor?: string | null;
  facets?: SearchFacet[];
};

type DocumentSection = {
  section_id: string;
  parent_section_id?: string | null;
  title: string;
  level: number;
  path: string[];
  ordinal: number;
  locator?: Record<string, unknown>;
  first_chunk_id?: string | null;
  last_chunk_id?: string | null;
};

type DocumentStructure = {
  document_id: string;
  document_version_id?: string | null;
  knowledge_base_id: string;
  title: string;
  source_type: string;
  source_url?: string | null;
  sections: DocumentSection[];
  public_metadata?: Record<string, unknown>;
};

type DocumentContextChunk = {
  chunk_id: string;
  document_id: string;
  document_version_id?: string | null;
  knowledge_base_id: string;
  title: string;
  section_path: string[];
  content: string;
  source_url: string;
  locator?: Record<string, unknown>;
  prev_chunk_id?: string | null;
  next_chunk_id?: string | null;
  chunk_ordinal?: number | null;
  highlighted: boolean;
};

type DocumentContextResponse = {
  document_id: string;
  document_version_id?: string | null;
  anchor_chunk_id?: string | null;
  section_id?: string | null;
  chunks: DocumentContextChunk[];
  limit: number;
  offset: number;
};

type DocumentSearchResult = {
  chunk_id: string;
  document_id: string;
  document_version_id?: string | null;
  knowledge_base_id: string;
  title: string;
  snippet: string;
  section_path: string[];
  source_url: string;
  locator?: Record<string, unknown>;
  prev_chunk_id?: string | null;
  next_chunk_id?: string | null;
  score: number;
  ranks: Record<string, number>;
};

type DocumentSearchResponse = {
  document_id: string;
  document_version_id?: string | null;
  results: DocumentSearchResult[];
  limit: number;
  offset: number;
  has_more: boolean;
};

export function App() {
  const [ready, setReady] = useState("checking");
  const [session, setSession] = useState<AuthSession>({
    authenticated: false,
  });
  const [authUsername, setAuthUsername] = useState("admin");
  const [authPassword, setAuthPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState("");
  const [
    selectedRetrievalKnowledgeBaseIds,
    setSelectedRetrievalKnowledgeBaseIds,
  ] = useState<string[]>([]);
  const [newKnowledgeBaseName, setNewKnowledgeBaseName] = useState("");
  const [limit, setLimit] = useState(1000);
  const [job, setJob] = useState<Job | null>(null);
  const [question, setQuestion] = useState("Что такое Россия?");
  const [mode, setMode] = useState<"normal" | "extended">("normal");
  const [retrievalProfile, setRetrievalProfile] = useState("sota_mvp");
  const [debugTopK, setDebugTopK] = useState(12);
  const [bm25Enabled, setBm25Enabled] = useState(true);
  const [denseEnabled, setDenseEnabled] = useState(true);
  const [rerankEnabled, setRerankEnabled] = useState(true);
  const [fusionMode, setFusionMode] = useState<"rrf" | "none">("rrf");
  const [parentExpansion, setParentExpansion] = useState<
    "off" | "selective" | "always"
  >("selective");
  const [extendedSearchMode, setExtendedSearchMode] = useState<
    "off" | "conditional" | "always"
  >("conditional");
  const [answer, setAnswer] = useState("");
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [queryRunId, setQueryRunId] = useState("");
  const [events, setEvents] = useState<RetrievalEvent[]>([]);
  const [debuggerRun, setDebuggerRun] = useState<QueryRunSummary | null>(null);
  const [uploadStatus, setUploadStatus] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadBatch, setUploadBatch] = useState<UploadBatchStatus | null>(
    null,
  );
  const [uploadItems, setUploadItems] = useState<UploadItemState[]>([]);
  const [uploadDocument, setUploadDocument] =
    useState<DocumentPublicMetadata | null>(null);
  const [uploadError, setUploadError] = useState("");
  const [sources, setSources] = useState<SourceResponse[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const [sourcesError, setSourcesError] = useState("");
  const [sourceStatus, setSourceStatus] = useState("");
  const [sourceBusy, setSourceBusy] = useState<Record<string, boolean>>({});
  const [sourceRuns, setSourceRuns] = useState<
    Record<string, SourceSyncRunResponse>
  >({});
  const [sourceKind, setSourceKind] = useState<SourceKind>("local_folder");
  const [sourceName, setSourceName] = useState(
    SOURCE_TEMPLATES.local_folder.name,
  );
  const [sourceRefreshInterval, setSourceRefreshInterval] = useState("3600");
  const [sourceConfigText, setSourceConfigText] = useState(
    formatJson(SOURCE_TEMPLATES.local_folder.config),
  );
  const [sourceCredentialsText, setSourceCredentialsText] = useState(
    formatJson(SOURCE_TEMPLATES.local_folder.credentials),
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [searchHasMore, setSearchHasMore] = useState(false);
  const [searchNextCursor, setSearchNextCursor] = useState<string | null>(null);
  const [searchFacets, setSearchFacets] = useState<SearchFacet[]>([]);
  const [searchDocumentType, setSearchDocumentType] = useState("");
  const [searchLanguage, setSearchLanguage] = useState("");
  const [searchDateFrom, setSearchDateFrom] = useState("");
  const [searchDateTo, setSearchDateTo] = useState("");
  const [searchSource, setSearchSource] = useState("");
  const [viewerStructure, setViewerStructure] =
    useState<DocumentStructure | null>(null);
  const [viewerContext, setViewerContext] =
    useState<DocumentContextResponse | null>(null);
  const [viewerSearchQuery, setViewerSearchQuery] = useState("");
  const [viewerSearchResults, setViewerSearchResults] = useState<
    DocumentSearchResult[]
  >([]);
  const [viewerBusy, setViewerBusy] = useState(false);
  const [viewerSearchBusy, setViewerSearchBusy] = useState(false);
  const [viewerError, setViewerError] = useState("");

  useEffect(() => {
    fetch(`${API_BASE}/ready`)
      .then((response) => response.json())
      .then((data) => setReady(data.status))
      .catch(() => setReady("offline"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadInitialSession() {
      const response = await fetch(`${API_BASE}/api/v1/auth/session`, {
        credentials: "include",
      });
      if (!response.ok || cancelled) return;
      const nextSession = (await response.json()) as AuthSession;
      setSession(nextSession);
      if (!nextSession.authenticated) return;
      const kbResponse = await fetch(`${API_BASE}/api/v1/knowledge-bases`, {
        credentials: "include",
      });
      if (!kbResponse.ok || cancelled) return;
      const items = (await kbResponse.json()) as KnowledgeBase[];
      setKnowledgeBases(items);
      if (items[0]) {
        setSelectedKnowledgeBaseId(items[0].id);
        setSelectedRetrievalKnowledgeBaseIds([items[0].id]);
      }
    }
    void loadInitialSession();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!session.authenticated || !selectedKnowledgeBaseId) {
      setSources([]);
      setSourcesLoading(false);
      setSourcesError("");
      return;
    }
    let cancelled = false;
    async function loadSelectedSources() {
      setSourcesLoading(true);
      setSourcesError("");
      try {
        const response = await fetch(
          `${API_BASE}/api/v1/knowledge-bases/${encodeURIComponent(selectedKnowledgeBaseId)}/sources`,
          { credentials: "include" },
        );
        if (!response.ok) throw new Error(await response.text());
        const payload = (await response.json()) as SourceListResponse;
        if (!cancelled) setSources(payload.sources);
      } catch (error) {
        if (!cancelled) {
          setSourcesError(
            error instanceof Error ? error.message : String(error),
          );
        }
      } finally {
        if (!cancelled) setSourcesLoading(false);
      }
    }
    void loadSelectedSources();
    return () => {
      cancelled = true;
    };
  }, [session.authenticated, selectedKnowledgeBaseId]);

  const imported = useMemo(() => job?.progress?.pages_imported ?? 0, [job]);
  const chunks = useMemo(() => job?.progress?.chunks_indexed ?? 0, [job]);
  const searchKnowledgeBaseIds =
    selectedRetrievalKnowledgeBaseIds.length > 0
      ? selectedRetrievalKnowledgeBaseIds
      : selectedKnowledgeBaseId
        ? [selectedKnowledgeBaseId]
        : [];

  async function apiFetch(path: string, init: RequestInit = {}) {
    const method = init.method?.toUpperCase() ?? "GET";
    const headers = new Headers(init.headers);
    if (method !== "GET" && session.csrf_token) {
      headers.set("X-CSRF-Token", session.csrf_token);
    }
    return fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: "include",
      headers,
    });
  }

  async function refreshSession() {
    const response = await fetch(`${API_BASE}/api/v1/auth/session`, {
      credentials: "include",
    });
    if (response.ok) {
      const nextSession = (await response.json()) as AuthSession;
      setSession(nextSession);
      if (nextSession.authenticated) {
        await loadKnowledgeBases();
      }
    }
  }

  async function localLogin(event: FormEvent) {
    event.preventDefault();
    setAuthError("");
    const response = await fetch(`${API_BASE}/api/v1/auth/local/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: authUsername,
        password: authPassword,
      }),
    });
    if (!response.ok) {
      setAuthError(await response.text());
      return;
    }
    setAuthPassword("");
    await refreshSession();
  }

  async function oidcLogin() {
    setAuthError("");
    const response = await fetch(`${API_BASE}/api/v1/auth/oidc/start`, {
      method: "POST",
      credentials: "include",
    });
    if (!response.ok) {
      setAuthError(await response.text());
      return;
    }
    const started = (await response.json()) as { authorization_url: string };
    window.location.assign(started.authorization_url);
  }

  async function logout() {
    const response = await apiFetch("/api/v1/auth/logout", { method: "POST" });
    if (response.ok) {
      setSession({ authenticated: false });
      setKnowledgeBases([]);
      setSelectedKnowledgeBaseId("");
      setSelectedRetrievalKnowledgeBaseIds([]);
      setSources([]);
      setSourceRuns({});
      setSearchResults([]);
      setSearchError("");
    }
  }

  async function loadKnowledgeBases() {
    const response = await apiFetch("/api/v1/knowledge-bases");
    if (!response.ok) return;
    const items = (await response.json()) as KnowledgeBase[];
    setKnowledgeBases(items);
    if (!selectedKnowledgeBaseId && items[0]) {
      setSelectedKnowledgeBaseId(items[0].id);
    }
    if (selectedRetrievalKnowledgeBaseIds.length === 0 && items[0]) {
      setSelectedRetrievalKnowledgeBaseIds([items[0].id]);
    }
  }

  async function loadSources(kbId = selectedKnowledgeBaseId) {
    if (!kbId) return;
    setSourcesLoading(true);
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/sources`,
      );
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as SourceListResponse;
      setSources(payload.sources);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourcesLoading(false);
    }
  }

  async function createKnowledgeBase(event: FormEvent) {
    event.preventDefault();
    if (!newKnowledgeBaseName.trim()) return;
    const response = await apiFetch("/api/v1/knowledge-bases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newKnowledgeBaseName.trim() }),
    });
    if (response.ok) {
      setNewKnowledgeBaseName("");
      await loadKnowledgeBases();
    }
  }

  function selectSourceKind(kind: SourceKind) {
    const template = SOURCE_TEMPLATES[kind];
    setSourceKind(kind);
    setSourceName(template.name);
    setSourceConfigText(formatJson(template.config));
    setSourceCredentialsText(formatJson(template.credentials));
    setSourceStatus("");
    setSourcesError("");
  }

  async function createSource(event: FormEvent) {
    event.preventDefault();
    if (!selectedKnowledgeBaseId || !sourceName.trim()) return;
    setSourceStatus("");
    setSourcesError("");
    setSourceBusy((busy) => ({ ...busy, create: true }));
    try {
      const refreshInterval = sourceRefreshInterval.trim()
        ? Number(sourceRefreshInterval)
        : null;
      if (
        refreshInterval !== null &&
        (!Number.isInteger(refreshInterval) || refreshInterval < 60)
      ) {
        throw new Error("refresh_interval_seconds must be at least 60");
      }
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(selectedKnowledgeBaseId)}/sources`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: sourceKind,
            name: sourceName.trim(),
            config: parseJsonObject("config", sourceConfigText),
            credentials: parseJsonObject("credentials", sourceCredentialsText),
            refresh_interval_seconds: refreshInterval,
            metadata: { ui_created: true },
          }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const created = (await response.json()) as SourceResponse;
      setSourceStatus(`Created ${created.name}`);
      setSourceCredentialsText(
        formatJson(SOURCE_TEMPLATES[sourceKind].credentials),
      );
      await loadSources(selectedKnowledgeBaseId);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourceBusy((busy) => ({ ...busy, create: false }));
    }
  }

  async function healthcheckSource(source: SourceResponse) {
    setSourceBusy((busy) => ({ ...busy, [source.id]: true }));
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/sources/${encodeURIComponent(source.id)}:healthcheck`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await response.text());
      const health = (await response.json()) as SourceHealthResponse;
      setSourceStatus(`${source.name}: ${health.status}`);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourceBusy((busy) => ({ ...busy, [source.id]: false }));
    }
  }

  async function syncSource(
    source: SourceResponse,
    mode: "incremental" | "full",
  ) {
    setSourceBusy((busy) => ({ ...busy, [source.id]: true }));
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/sources/${encodeURIComponent(source.id)}:sync`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as SourceSyncResponse;
      setSourceStatus(`${source.name}: sync ${payload.status}`);
      void pollSourceSyncRun(payload.run_id);
      await loadSources(source.knowledge_base_id);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
      setSourceBusy((busy) => ({ ...busy, [source.id]: false }));
    }
  }

  async function patchSourceStatus(source: SourceResponse) {
    const nextStatus = source.status === "disabled" ? "active" : "disabled";
    setSourceBusy((busy) => ({ ...busy, [source.id]: true }));
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/sources/${encodeURIComponent(source.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: nextStatus }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      setSourceStatus(`${source.name}: ${nextStatus}`);
      await loadSources(source.knowledge_base_id);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourceBusy((busy) => ({ ...busy, [source.id]: false }));
    }
  }

  async function pollSourceSyncRun(runId: string) {
    const response = await apiFetch(
      `/api/v1/source-sync-runs/${encodeURIComponent(runId)}`,
    );
    if (!response.ok) {
      setSourcesError(await response.text());
      return;
    }
    const run = (await response.json()) as SourceSyncRunResponse;
    setSourceRuns((runs) => ({ ...runs, [run.source_id]: run }));
    if (!["completed", "failed", "cancelled"].includes(run.status)) {
      window.setTimeout(() => pollSourceSyncRun(runId), 1500);
      return;
    }
    setSourceBusy((busy) => ({ ...busy, [run.source_id]: false }));
    await loadSources(run.knowledge_base_id);
  }

  async function startImport() {
    const response = await apiFetch("/api/v1/wikipedia/zim-imports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit }),
    });
    const created = await response.json();
    pollJob(created.job_id);
  }

  async function pollJob(jobId: string) {
    const response = await apiFetch(`/api/v1/ingestion-jobs/${jobId}`);
    const nextJob = await response.json();
    setJob(nextJob);
    if (!["completed", "failed", "cancelled"].includes(nextJob.status)) {
      window.setTimeout(() => pollJob(jobId), 1500);
    }
  }

  async function submitChat(event: FormEvent) {
    event.preventDefault();
    setAnswer("");
    setEvidence([]);
    setEvents([]);
    setDebuggerRun(null);
    const response = await apiFetch("/api/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: question,
        knowledge_base_ids:
          selectedRetrievalKnowledgeBaseIds.length > 0
            ? selectedRetrievalKnowledgeBaseIds
            : selectedKnowledgeBaseId
              ? [selectedKnowledgeBaseId]
              : [],
        mode,
        stream: true,
        retrieval_profile: retrievalProfile,
        retrieval_overrides: buildRetrievalOverrides({
          bm25Enabled,
          denseEnabled,
          rerankEnabled,
          fusionMode,
          parentExpansion,
          extendedSearchMode,
          topK: debugTopK,
        }),
      }),
    });
    const reader = response.body?.getReader();
    if (!reader) return;
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const parsed = parseSse(part);
        if (parsed?.event === "message.delta") {
          setAnswer(parsed.data.data.text ?? "");
          setEvidence(parsed.data.data.evidence ?? []);
          setQueryRunId(parsed.data.query_run_id);
        }
        if (parsed?.event === "run.completed") {
          setQueryRunId(parsed.data.query_run_id);
        }
      }
    }
  }

  async function submitSearch(event: FormEvent) {
    event.preventDefault();
    await runSearch(0);
  }

  async function loadMoreSearch() {
    await runSearch(searchResults.length, searchNextCursor);
  }

  async function runSearch(offset: number, cursor: string | null = null) {
    const query = searchQuery.trim();
    if (!query || searchKnowledgeBaseIds.length === 0) return;
    setSearchBusy(true);
    setSearchError("");
    if (offset === 0) {
      setSearchResults([]);
      setSearchHasMore(false);
      setSearchNextCursor(null);
      setSearchFacets([]);
    }
    try {
      const filters = buildSearchFilters({
        documentType: searchDocumentType,
        language: searchLanguage,
        dateFrom: searchDateFrom,
        dateTo: searchDateTo,
        source: searchSource,
      });
      const response = await apiFetch("/api/v1/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          knowledge_base_ids: searchKnowledgeBaseIds,
          limit: 10,
          offset,
          cursor,
          group_by_document: true,
          include_highlights: true,
          include_facets: true,
          filters,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as SearchResponse;
      setSearchResults((items) =>
        offset === 0 ? payload.results : [...items, ...payload.results],
      );
      setSearchHasMore(payload.has_more);
      setSearchNextCursor(payload.next_cursor ?? null);
      setSearchFacets(payload.facets ?? []);
    } catch (error) {
      setSearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setSearchBusy(false);
    }
  }

  async function openDocumentViewer(item: SearchResult) {
    setViewerBusy(true);
    setViewerError("");
    setViewerSearchResults([]);
    setViewerSearchQuery("");
    try {
      const structureResponse = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(item.document_id)}/structure`,
      );
      if (!structureResponse.ok)
        throw new Error(await structureResponse.text());
      const structure = (await structureResponse.json()) as DocumentStructure;
      setViewerStructure(structure);
      await loadDocumentContext(item.document_id, { chunkId: item.chunk_id });
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error));
    } finally {
      setViewerBusy(false);
    }
  }

  async function loadDocumentContext(
    documentId: string,
    options: { chunkId?: string; sectionId?: string } = {},
  ) {
    setViewerBusy(true);
    setViewerError("");
    try {
      const params = new URLSearchParams();
      if (options.chunkId) params.set("chunk_id", options.chunkId);
      if (options.sectionId) params.set("section_id", options.sectionId);
      params.set("before", "2");
      params.set("after", "2");
      const response = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(documentId)}/context?${params.toString()}`,
      );
      if (!response.ok) throw new Error(await response.text());
      setViewerContext((await response.json()) as DocumentContextResponse);
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error));
    } finally {
      setViewerBusy(false);
    }
  }

  async function submitDocumentSearch(event: FormEvent) {
    event.preventDefault();
    const query = viewerSearchQuery.trim();
    if (!viewerStructure || !query) return;
    setViewerSearchBusy(true);
    setViewerError("");
    try {
      const response = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(viewerStructure.document_id)}/search`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, limit: 10, offset: 0 }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as DocumentSearchResponse;
      setViewerSearchResults(payload.results);
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error));
    } finally {
      setViewerSearchBusy(false);
    }
  }

  function closeDocumentViewer() {
    setViewerStructure(null);
    setViewerContext(null);
    setViewerSearchResults([]);
    setViewerSearchQuery("");
    setViewerError("");
  }

  async function loadDebugger() {
    if (!queryRunId) return;
    const response = await apiFetch(
      `/api/v1/query-runs/${queryRunId}/retrieval`,
    );
    const data = await response.json();
    setDebuggerRun(data.run ?? null);
    setEvents(data.events);
  }

  function toggleRetrievalKnowledgeBase(kbId: string): void {
    setSelectedRetrievalKnowledgeBaseIds((selected) =>
      selected.includes(kbId)
        ? selected.filter((candidate) => candidate !== kbId)
        : [...selected, kbId],
    );
  }

  function patchUploadItem(id: string, patch: Partial<UploadItemState>): void {
    setUploadItems((items) =>
      items.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  }

  async function uploadFile(event: React.ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const files = Array.from(input.files ?? []);
    if (files.length === 0) return;
    const planned = files.map((file, index) => ({
      id: `local-${Date.now()}-${index}`,
      filename: file.name,
      size_bytes: file.size,
      status: "preparing",
    }));
    setUploadBusy(true);
    setUploadError("");
    setUploadBatch(null);
    setUploadItems(planned);
    setUploadDocument(null);
    setUploadStatus(`Preparing ${files.length} file(s)`);
    try {
      const batchItems = [];
      for (const [index, file] of files.entries()) {
        patchUploadItem(planned[index].id, { status: "hashing" });
        batchItems.push({
          filename: file.name,
          content_type: file.type || "application/octet-stream",
          size_bytes: file.size,
          checksum_sha256: await sha256Hex(file),
          parser_profile: "standard",
          metadata: { ui_upload: true },
        });
      }
      const batchResponse = await apiFetch("/api/v1/uploads/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          knowledge_base_id: selectedKnowledgeBaseId || undefined,
          metadata: { ui_upload: true },
          items: batchItems,
        }),
      });
      if (!batchResponse.ok) throw new Error(await batchResponse.text());
      const batch = (await batchResponse.json()) as UploadBatchAccepted;
      setUploadStatus(`Created batch ${batch.batch_id}`);
      for (const [index, item] of batch.items.entries()) {
        const localId = planned[index].id;
        patchUploadItem(localId, {
          status: "uploading",
          upload_session_id: item.upload_session_id,
        });
        const putResponse = await fetch(item.upload_url, {
          method: "PUT",
          headers: item.required_headers,
          body: files[index],
        });
        if (!putResponse.ok) {
          patchUploadItem(localId, {
            status: "failed",
            error_message: await putResponse.text(),
          });
          continue;
        }
        patchUploadItem(localId, { status: "completing" });
        const completeResponse = await apiFetch(
          `/api/v1/uploads/sessions/${item.upload_session_id}:complete`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ metadata: { ui_upload: true } }),
          },
        );
        if (!completeResponse.ok) {
          patchUploadItem(localId, {
            status: "failed",
            error_message: await completeResponse.text(),
          });
          continue;
        }
        const completed =
          (await completeResponse.json()) as UploadCompleteResponse;
        patchUploadItem(localId, {
          status: "queued",
          document_id: completed.document_id,
          job_id: completed.job_id,
        });
      }
      setUploadStatus(`Queued batch ${batch.batch_id}`);
      void pollUploadBatch(batch.batch_id);
    } catch (error) {
      setUploadBusy(false);
      setUploadError(error instanceof Error ? error.message : String(error));
      setUploadStatus("");
    } finally {
      input.value = "";
    }
  }

  async function pollUploadBatch(batchId: string) {
    const response = await apiFetch(
      `/api/v1/uploads/batches/${encodeURIComponent(batchId)}`,
    );
    if (!response.ok) {
      setUploadBusy(false);
      setUploadError(await response.text());
      return;
    }
    const nextBatch = (await response.json()) as UploadBatchStatus;
    setUploadBatch(nextBatch);
    setUploadItems((items) =>
      items.map((item) => {
        const serverItem = nextBatch.items.find(
          (candidate) => candidate.upload_session_id === item.upload_session_id,
        );
        if (!serverItem) return item;
        return {
          ...item,
          filename: serverItem.filename,
          size_bytes: serverItem.size_bytes,
          status: serverItem.status,
          document_id: serverItem.document_id,
          job_id: serverItem.job_id,
          progress: serverItem.progress,
          error_code: serverItem.error_code,
          error_message: serverItem.error_message,
        };
      }),
    );
    setUploadStatus(
      `Batch ${nextBatch.status}: ${nextBatch.completed_items}/${nextBatch.total_items} complete`,
    );
    if (!["completed", "failed", "cancelled"].includes(nextBatch.status)) {
      window.setTimeout(() => pollUploadBatch(batchId), 1500);
      return;
    }
    setUploadBusy(false);
    const firstCompleted = nextBatch.items.find((item) => item.document_id);
    if (firstCompleted?.document_id) {
      const documentResponse = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(firstCompleted.document_id)}`,
      );
      if (documentResponse.ok) {
        setUploadDocument(
          (await documentResponse.json()) as DocumentPublicMetadata,
        );
      }
    }
  }

  async function retryUploadItem(item: UploadItemState) {
    if (!item.job_id || !uploadBatch) return;
    patchUploadItem(item.id, { status: "resume_requested" });
    const response = await apiFetch(
      `/api/v1/ingestion-jobs/${item.job_id}:resume`,
      { method: "POST" },
    );
    if (!response.ok) {
      patchUploadItem(item.id, {
        status: "failed",
        error_message: await response.text(),
      });
      return;
    }
    setUploadBusy(true);
    void pollUploadBatch(uploadBatch.batch_id);
  }

  return (
    <main>
      <header>
        <div>
          <h1>WikipediaRag</h1>
          <p>Local Russian Wikipedia RAG MVP</p>
        </div>
        <div className="header-actions">
          <span className={`status ${ready}`}>{ready}</span>
          {session.authenticated ? (
            <>
              <span className="session-pill">
                {session.user?.username ?? session.user?.id}
                {session.active_tenant_id ? "" : " · no tenant"}
              </span>
              <button type="button" onClick={logout}>
                <LogOut size={16} /> Logout
              </button>
            </>
          ) : null}
        </div>
      </header>

      {!session.authenticated && (
        <section className="band auth-band">
          <form className="auth-panel" onSubmit={localLogin}>
            <h2>
              <KeyRound size={18} /> Sign in
            </h2>
            <input
              value={authUsername}
              onChange={(event) => setAuthUsername(event.target.value)}
              placeholder="Username"
              autoComplete="username"
            />
            <input
              value={authPassword}
              onChange={(event) => setAuthPassword(event.target.value)}
              placeholder="Password"
              type="password"
              autoComplete="current-password"
            />
            <div className="row">
              <button type="submit">
                <LogIn size={16} /> Local
              </button>
              <button type="button" onClick={oidcLogin}>
                <KeyRound size={16} /> OIDC
              </button>
            </div>
            {authError && <p className="error">{authError}</p>}
          </form>
        </section>
      )}

      {session.authenticated && (
        <section className="band kb-toolbar">
          <label>
            Primary knowledge base
            <select
              value={selectedKnowledgeBaseId}
              onChange={(event) =>
                setSelectedKnowledgeBaseId(event.target.value)
              }
            >
              {knowledgeBases.map((kb) => (
                <option key={kb.id} value={kb.id}>
                  {kb.name}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="kb-scope">
            <legend>Retrieval scope</legend>
            {knowledgeBases.map((kb) => (
              <label key={kb.id}>
                <input
                  type="checkbox"
                  checked={selectedRetrievalKnowledgeBaseIds.includes(kb.id)}
                  onChange={() => toggleRetrievalKnowledgeBase(kb.id)}
                />
                <span>{kb.name}</span>
              </label>
            ))}
          </fieldset>
          <form className="row" onSubmit={createKnowledgeBase}>
            <input
              value={newKnowledgeBaseName}
              onChange={(event) => setNewKnowledgeBaseName(event.target.value)}
              placeholder="New KB name"
            />
            <button type="submit">
              <Database size={16} /> Create
            </button>
          </form>
        </section>
      )}

      {session.authenticated && (
        <>
          <section className="band grid">
            <div className="panel">
              <h2>
                <Database size={18} /> Wikipedia Import
              </h2>
              <div className="row">
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={limit}
                  onChange={(event) => setLimit(Number(event.target.value))}
                />
                <button onClick={startImport}>
                  <Play size={16} /> Start
                </button>
              </div>
              {job && (
                <div className="progress">
                  <strong>{job.status}</strong>
                  <span>{imported} pages</span>
                  <span>{chunks} chunks</span>
                  {job.error_message && (
                    <span className="error">{job.error_message}</span>
                  )}
                </div>
              )}
            </div>

            <div className="panel">
              <h2>
                <FileUp size={18} /> Upload
              </h2>
              <input
                type="file"
                accept=".txt,.md,.markdown,.html,.htm,.csv,.tsv,.json,.jsonl,.pdf,.docx,.pptx,.xlsx"
                multiple
                onChange={uploadFile}
                disabled={uploadBusy}
              />
              {uploadStatus && <p className="upload-status">{uploadStatus}</p>}
              {uploadBatch && (
                <div className="progress upload-progress">
                  <strong>{uploadBatch.status}</strong>
                  <span>{uploadBatch.total_items} files</span>
                  <span>{uploadBatch.completed_items} completed</span>
                  <span>{uploadBatch.failed_items} failed</span>
                  <span>{uploadBatch.pending_items} pending</span>
                </div>
              )}
              {uploadItems.length > 0 && (
                <div className="upload-items">
                  {uploadItems.map((item) => (
                    <article key={item.id} className="upload-item">
                      <div>
                        <strong>{item.filename}</strong>
                        <span>{item.size_bytes} bytes</span>
                      </div>
                      <div>
                        <span>{item.status}</span>
                        <span>{item.progress?.stage ?? "waiting"}</span>
                        <span>
                          {item.progress?.parser_route ?? "parser pending"}
                        </span>
                        <span>
                          {item.progress?.chunks_published ?? 0} published
                        </span>
                      </div>
                      {(item.error_code || item.error_message) && (
                        <p className="error">
                          {item.error_code ?? item.error_message}
                        </p>
                      )}
                      {item.status === "failed" && item.job_id && (
                        <button
                          type="button"
                          onClick={() => void retryUploadItem(item)}
                        >
                          <RotateCw size={16} /> Retry
                        </button>
                      )}
                    </article>
                  ))}
                </div>
              )}
              {uploadDocument && (
                <dl className="upload-meta">
                  <div>
                    <dt>Title</dt>
                    <dd>{uploadDocument.title}</dd>
                  </div>
                  <div>
                    <dt>Status</dt>
                    <dd>{uploadDocument.status ?? "published"}</dd>
                  </div>
                  <div>
                    <dt>Language</dt>
                    <dd>
                      {metadataValue(uploadDocument, "detected_language")}
                    </dd>
                  </div>
                  <div>
                    <dt>Document Date</dt>
                    <dd>{metadataValue(uploadDocument, "document_date")}</dd>
                  </div>
                  <div>
                    <dt>Parser</dt>
                    <dd>{uploadDocument.parser_route ?? "unknown"}</dd>
                  </div>
                  <div>
                    <dt>Uploaded</dt>
                    <dd>{formatTimestamp(uploadDocument.uploaded_at)}</dd>
                  </div>
                </dl>
              )}
              {uploadError && <p className="error">{uploadError}</p>}
            </div>

            <div className="panel source-panel">
              <h2>
                <Plug size={18} /> External Sources
              </h2>
              <form className="source-form" onSubmit={createSource}>
                <label>
                  Connector
                  <select
                    value={sourceKind}
                    onChange={(event) =>
                      selectSourceKind(event.target.value as SourceKind)
                    }
                  >
                    {SOURCE_KINDS.map((kind) => (
                      <option key={kind} value={kind}>
                        {SOURCE_TEMPLATES[kind].label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Name
                  <input
                    value={sourceName}
                    onChange={(event) => setSourceName(event.target.value)}
                    placeholder="Source name"
                  />
                </label>
                <label>
                  Refresh seconds
                  <input
                    type="number"
                    min={60}
                    value={sourceRefreshInterval}
                    onChange={(event) =>
                      setSourceRefreshInterval(event.target.value)
                    }
                  />
                </label>
                <label className="source-wide">
                  Config JSON
                  <textarea
                    className="source-json"
                    value={sourceConfigText}
                    onChange={(event) =>
                      setSourceConfigText(event.target.value)
                    }
                    spellCheck={false}
                  />
                </label>
                <label className="source-wide">
                  Credentials JSON
                  <textarea
                    className="source-json"
                    value={sourceCredentialsText}
                    onChange={(event) =>
                      setSourceCredentialsText(event.target.value)
                    }
                    spellCheck={false}
                  />
                </label>
                <div className="source-wide row">
                  <button
                    type="submit"
                    disabled={!selectedKnowledgeBaseId || sourceBusy.create}
                  >
                    <Plug size={16} /> Add Source
                  </button>
                  <button
                    type="button"
                    onClick={() => void loadSources()}
                    disabled={!selectedKnowledgeBaseId || sourcesLoading}
                  >
                    <RotateCw size={16} /> Refresh
                  </button>
                </div>
              </form>
              {(sourceStatus || sourcesLoading) && (
                <p className="upload-status">
                  {sourcesLoading ? "Loading sources" : sourceStatus}
                </p>
              )}
              {sourcesError && <p className="error">{sourcesError}</p>}
              <div className="source-list">
                {sources.length === 0 && !sourcesLoading && (
                  <p className="empty-state">No external sources</p>
                )}
                {sources.map((source) => {
                  const run = sourceRuns[source.id];
                  return (
                    <article key={source.id} className="source-item">
                      <div className="source-item-header">
                        <div>
                          <h3>{source.name}</h3>
                          <div className="search-meta">
                            <span>{sourceKindLabel(source.kind)}</span>
                            <span>{source.status}</span>
                            <span>
                              {source.refresh_interval_seconds
                                ? `${source.refresh_interval_seconds}s`
                                : "manual"}
                            </span>
                          </div>
                        </div>
                        <span className={`source-badge ${source.status}`}>
                          {source.last_sync_status ?? source.status}
                        </span>
                      </div>
                      <dl className="source-meta">
                        <div>
                          <dt>Last Sync</dt>
                          <dd>{formatTimestamp(source.last_synced_at)}</dd>
                        </div>
                        <div>
                          <dt>Next Sync</dt>
                          <dd>{formatTimestamp(source.next_sync_at)}</dd>
                        </div>
                        <div>
                          <dt>Config</dt>
                          <dd>
                            {formatCompactJson(redactSensitive(source.config))}
                          </dd>
                        </div>
                      </dl>
                      {run && (
                        <div className="source-run">
                          <ShieldCheck size={15} />
                          <span>{run.mode}</span>
                          <span>{run.status}</span>
                          <span>{formatCompactJson(run.stats)}</span>
                          {run.error_code && (
                            <span className="error">{run.error_code}</span>
                          )}
                        </div>
                      )}
                      <div className="source-actions">
                        <button
                          type="button"
                          onClick={() => void healthcheckSource(source)}
                          disabled={sourceBusy[source.id]}
                        >
                          <ShieldCheck size={15} /> Health
                        </button>
                        <button
                          type="button"
                          onClick={() => void syncSource(source, "incremental")}
                          disabled={
                            sourceBusy[source.id] ||
                            source.status === "disabled"
                          }
                        >
                          <RotateCw size={15} /> Sync
                        </button>
                        <button
                          type="button"
                          onClick={() => void syncSource(source, "full")}
                          disabled={
                            sourceBusy[source.id] ||
                            source.status === "disabled"
                          }
                        >
                          <RotateCw size={15} /> Full
                        </button>
                        <button
                          type="button"
                          onClick={() => void patchSourceStatus(source)}
                          disabled={sourceBusy[source.id]}
                        >
                          {source.status === "disabled" ? "Enable" : "Disable"}
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            </div>
          </section>

          <section className="band">
            <form className="search-panel" onSubmit={submitSearch}>
              <h2>
                <Search size={18} /> Search
              </h2>
              <div className="search-query">
                <input
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  placeholder="Search documents"
                />
                <button
                  type="submit"
                  disabled={
                    searchBusy ||
                    !searchQuery.trim() ||
                    searchKnowledgeBaseIds.length === 0
                  }
                >
                  <Search size={16} /> Search
                </button>
              </div>
              <div className="search-filters">
                <label>
                  Document type
                  <input
                    value={searchDocumentType}
                    onChange={(event) =>
                      setSearchDocumentType(event.target.value)
                    }
                    placeholder="pdf, text, html"
                  />
                </label>
                <label>
                  Language
                  <input
                    value={searchLanguage}
                    onChange={(event) => setSearchLanguage(event.target.value)}
                    placeholder="ru"
                  />
                </label>
                <label>
                  Date from
                  <input
                    type="date"
                    value={searchDateFrom}
                    onChange={(event) => setSearchDateFrom(event.target.value)}
                  />
                </label>
                <label>
                  Date to
                  <input
                    type="date"
                    value={searchDateTo}
                    onChange={(event) => setSearchDateTo(event.target.value)}
                  />
                </label>
                <label>
                  Source
                  <input
                    value={searchSource}
                    onChange={(event) => setSearchSource(event.target.value)}
                    placeholder="upload, wikipedia, url"
                  />
                </label>
              </div>
            </form>

            <div className="search-results">
              {searchError && <p className="error">{searchError}</p>}
              {searchFacets.length > 0 && (
                <div className="search-facets">
                  {searchFacets.map((facet) => (
                    <div key={facet.field}>
                      <strong>{formatFacetName(facet.field)}</strong>
                      <span>
                        {facet.buckets
                          .slice(0, 4)
                          .map((bucket) => `${bucket.value} (${bucket.count})`)
                          .join(", ")}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {!searchBusy &&
                !searchError &&
                searchQuery &&
                searchResults.length === 0 && (
                  <p className="empty-state">No results</p>
                )}
              {searchResults.map((item, index) => (
                <article
                  key={`${item.chunk_id}-${index}`}
                  className="search-result"
                >
                  <div>
                    <h3>{item.title}</h3>
                    <p>
                      {item.highlights?.[0]?.fragments?.[0] ?? item.snippet}
                    </p>
                  </div>
                  <div className="search-meta">
                    <span>
                      {knowledgeBaseName(
                        knowledgeBases,
                        item.knowledge_base_id,
                      )}
                    </span>
                    <span>{item.section_path.join(" / ") || "No section"}</span>
                    <span>{item.document_date ?? "No date"}</span>
                    <span>{item.document_type ?? item.source_type}</span>
                    <span>{item.language ?? "No language"}</span>
                    <span>{formatScore(item.score)}</span>
                  </div>
                  <div className="search-actions">
                    <button
                      type="button"
                      onClick={() => void openDocumentViewer(item)}
                    >
                      <BookOpen size={15} /> Open in viewer
                    </button>
                    {item.source_url ? (
                      <a
                        href={item.source_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <ExternalLink size={15} /> Source
                      </a>
                    ) : (
                      <span>Document {item.document_id}</span>
                    )}
                  </div>
                </article>
              ))}
              {searchHasMore && (
                <button
                  type="button"
                  onClick={loadMoreSearch}
                  disabled={searchBusy}
                >
                  {searchBusy ? "Loading" : "Load more"}
                </button>
              )}
            </div>
            {viewerStructure && (
              <DocumentViewer
                structure={viewerStructure}
                context={viewerContext}
                searchQuery={viewerSearchQuery}
                searchResults={viewerSearchResults}
                busy={viewerBusy}
                searchBusy={viewerSearchBusy}
                error={viewerError}
                onClose={closeDocumentViewer}
                onSearchQueryChange={setViewerSearchQuery}
                onSearch={submitDocumentSearch}
                onOpenChunk={(chunkId) =>
                  void loadDocumentContext(viewerStructure.document_id, {
                    chunkId,
                  })
                }
                onOpenSection={(sectionId) =>
                  void loadDocumentContext(viewerStructure.document_id, {
                    sectionId,
                  })
                }
              />
            )}
          </section>

          <section className="band">
            <form className="chat" onSubmit={submitChat}>
              <h2>
                <MessageSquare size={18} /> Chat
              </h2>
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
              />
              <details className="advanced">
                <summary>
                  <SlidersHorizontal size={16} /> Advanced retrieval settings
                </summary>
                <div className="advanced-grid">
                  <label>
                    Profile
                    <select
                      value={retrievalProfile}
                      onChange={(event) =>
                        setRetrievalProfile(event.target.value)
                      }
                    >
                      <option value="sota_mvp">sota_mvp</option>
                      <option value="test_mock">test_mock</option>
                      <option value="upload_mock">upload_mock</option>
                      <option value="upload_sota_mvp">upload_sota_mvp</option>
                      <option value="bm25_only">bm25_only</option>
                      <option value="rewrite_off">rewrite_off</option>
                      <option value="parent_expansion_off">
                        parent_expansion_off
                      </option>
                    </select>
                  </label>
                  <label>
                    Top K
                    <input
                      type="number"
                      min={1}
                      max={50}
                      value={debugTopK}
                      onChange={(event) =>
                        setDebugTopK(Number(event.target.value))
                      }
                    />
                  </label>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={bm25Enabled}
                      onChange={(event) => setBm25Enabled(event.target.checked)}
                    />
                    BM25
                  </label>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={denseEnabled}
                      onChange={(event) =>
                        setDenseEnabled(event.target.checked)
                      }
                    />
                    Dense
                  </label>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={rerankEnabled}
                      onChange={(event) =>
                        setRerankEnabled(event.target.checked)
                      }
                    />
                    Rerank
                  </label>
                  <label>
                    Fusion
                    <select
                      value={fusionMode}
                      onChange={(event) =>
                        setFusionMode(event.target.value as "rrf" | "none")
                      }
                    >
                      <option value="rrf">rrf</option>
                      <option value="none">none</option>
                    </select>
                  </label>
                  <label>
                    Parent expansion
                    <select
                      value={parentExpansion}
                      onChange={(event) =>
                        setParentExpansion(
                          event.target.value as "off" | "selective" | "always",
                        )
                      }
                    >
                      <option value="selective">selective</option>
                      <option value="off">off</option>
                      <option value="always">always</option>
                    </select>
                  </label>
                  <label>
                    Extended Search
                    <select
                      value={extendedSearchMode}
                      onChange={(event) =>
                        setExtendedSearchMode(
                          event.target.value as
                            | "off"
                            | "conditional"
                            | "always",
                        )
                      }
                    >
                      <option value="conditional">conditional</option>
                      <option value="off">off</option>
                      <option value="always">always</option>
                    </select>
                  </label>
                </div>
              </details>
              <div className="row">
                <select
                  value={mode}
                  onChange={(event) =>
                    setMode(event.target.value as "normal" | "extended")
                  }
                >
                  <option value="normal">Normal</option>
                  <option value="extended">Extended</option>
                </select>
                <button type="submit">
                  <Search size={16} /> Ask
                </button>
                <button
                  type="button"
                  onClick={loadDebugger}
                  disabled={!queryRunId}
                >
                  <Bug size={16} /> Debug
                </button>
                <button type="button" onClick={() => window.location.reload()}>
                  <RotateCw size={16} />
                </button>
              </div>
            </form>

            {answer && (
              <div className="answer">
                <h2>Answer</h2>
                <p>{answer}</p>
                <h3>Sources</h3>
                <div className="sources">
                  {evidence.map((item) => (
                    <article key={item.evidence_id}>
                      <a
                        href={item.source_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        [{item.evidence_id}] {item.title}
                      </a>
                      <span>{item.section_path.join(" / ")}</span>
                      <p>{item.content}</p>
                    </article>
                  ))}
                </div>
              </div>
            )}

            {events.length > 0 && (
              <div className="debugger">
                <h2>Retrieval Debugger</h2>
                <RetrievalDebugger run={debuggerRun} events={events} />
              </div>
            )}
          </section>
        </>
      )}
    </main>
  );
}

function buildRetrievalOverrides(state: RetrievalOverrideState) {
  return {
    retrieval: {
      bm25: state.bm25Enabled,
      dense: state.denseEnabled,
      fusion: state.fusionMode,
      rerank: state.rerankEnabled,
      top_k: state.topK,
    },
    postprocess: {
      parent_expansion: state.parentExpansion,
      extended_search: state.extendedSearchMode,
    },
  };
}

function buildSearchFilters(values: {
  documentType: string;
  language: string;
  dateFrom: string;
  dateTo: string;
  source: string;
}): SearchFilters {
  const filters: SearchFilters = {};
  if (values.documentType.trim()) {
    filters.document_type = values.documentType.trim();
  }
  if (values.language.trim()) {
    filters.language = values.language.trim();
  }
  if (values.dateFrom) {
    filters.date_from = values.dateFrom;
  }
  if (values.dateTo) {
    filters.date_to = values.dateTo;
  }
  if (values.source.trim()) {
    filters.source = values.source.trim();
  }
  return filters;
}

function knowledgeBaseName(
  knowledgeBases: KnowledgeBase[],
  id: string,
): string {
  return knowledgeBases.find((kb) => kb.id === id)?.name ?? id;
}

function formatFacetName(field: string) {
  const labels: Record<string, string> = {
    source_type: "Source",
    document_type: "Type",
    language: "Language",
    knowledge_base_id: "KB",
  };
  return labels[field] ?? field;
}

function formatScore(value: number) {
  if (!Number.isFinite(value)) return "score 0";
  return `score ${value.toFixed(value >= 10 ? 0 : 3)}`;
}

async function sha256Hex(file: File) {
  const hashBuffer = await crypto.subtle.digest(
    "SHA-256",
    await file.arrayBuffer(),
  );
  return Array.from(new Uint8Array(hashBuffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function metadataValue(document: DocumentPublicMetadata, key: string) {
  const value = document.public_metadata?.[key];
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  return "";
}

function formatTimestamp(value?: string | null) {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatJson(value: Record<string, unknown>) {
  return JSON.stringify(value, null, 2);
}

function formatCompactJson(value: Record<string, unknown>) {
  const text = JSON.stringify(value);
  if (!text || text === "{}") return "";
  return text.length > 140 ? `${text.slice(0, 137)}...` : text;
}

function redactSensitive(
  value: Record<string, unknown>,
): Record<string, unknown> {
  const secretTokens = ["secret", "password", "token", "cookie", "credential"];
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => {
      if (secretTokens.some((token) => key.toLowerCase().includes(token))) {
        return [key, "<redacted>"];
      }
      if (typeof item === "object" && item !== null && !Array.isArray(item)) {
        return [key, redactSensitive(item as Record<string, unknown>)];
      }
      return [key, item];
    }),
  );
}

function parseJsonObject(label: string, text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  const parsed: unknown = JSON.parse(trimmed);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

function sourceKindLabel(kind: string) {
  return SOURCE_KINDS.includes(kind as SourceKind)
    ? SOURCE_TEMPLATES[kind as SourceKind].label
    : kind;
}

function DocumentViewer({
  structure,
  context,
  searchQuery,
  searchResults,
  busy,
  searchBusy,
  error,
  onClose,
  onSearchQueryChange,
  onSearch,
  onOpenChunk,
  onOpenSection,
}: {
  structure: DocumentStructure;
  context: DocumentContextResponse | null;
  searchQuery: string;
  searchResults: DocumentSearchResult[];
  busy: boolean;
  searchBusy: boolean;
  error: string;
  onClose: () => void;
  onSearchQueryChange: (value: string) => void;
  onSearch: (event: FormEvent) => void;
  onOpenChunk: (chunkId: string) => void;
  onOpenSection: (sectionId: string) => void;
}) {
  return (
    <section className="document-viewer" aria-live="polite">
      <div className="document-viewer-header">
        <div>
          <h2>
            <BookOpen size={18} /> {structure.title}
          </h2>
          <div className="search-meta">
            <span>{structure.source_type}</span>
            <span>{structure.sections.length} sections</span>
            {structure.document_version_id && (
              <span>{structure.document_version_id}</span>
            )}
          </div>
        </div>
        <div className="row">
          {structure.source_url && (
            <a href={structure.source_url} target="_blank" rel="noreferrer">
              <ExternalLink size={15} /> Source
            </a>
          )}
          <button type="button" onClick={onClose}>
            <X size={16} /> Close
          </button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="document-viewer-grid">
        <nav className="document-toc">
          {structure.sections.map((section) => (
            <button
              key={section.section_id}
              type="button"
              style={{
                paddingLeft: `${Math.max(0, section.level - 1) * 14 + 12}px`,
              }}
              onClick={() => onOpenSection(section.section_id)}
            >
              {section.title}
            </button>
          ))}
        </nav>
        <div className="document-main">
          <form className="document-search" onSubmit={onSearch}>
            <input
              value={searchQuery}
              onChange={(event) => onSearchQueryChange(event.target.value)}
              placeholder="Search inside this document"
            />
            <button type="submit" disabled={searchBusy || !searchQuery.trim()}>
              <Search size={15} /> Search
            </button>
          </form>
          {searchResults.length > 0 && (
            <div className="document-search-results">
              {searchResults.map((item) => (
                <button
                  key={item.chunk_id}
                  type="button"
                  onClick={() => onOpenChunk(item.chunk_id)}
                >
                  <strong>{item.section_path.join(" / ") || item.title}</strong>
                  <span>{item.snippet}</span>
                </button>
              ))}
            </div>
          )}
          {busy && <p className="empty-state">Loading</p>}
          {context && context.chunks.length === 0 && !busy && (
            <p className="empty-state">No text context</p>
          )}
          {context && context.chunks.length > 0 && (
            <div className="document-context">
              {context.chunks.map((chunk) => (
                <article
                  key={chunk.chunk_id}
                  className={
                    chunk.highlighted
                      ? "document-chunk highlighted"
                      : "document-chunk"
                  }
                >
                  <div className="search-meta">
                    <span>{chunk.section_path.join(" / ") || chunk.title}</span>
                    <span>
                      {chunk.chunk_ordinal
                        ? `#${chunk.chunk_ordinal}`
                        : chunk.chunk_id}
                    </span>
                    {chunk.locator && (
                      <span>{formatLocator(chunk.locator)}</span>
                    )}
                  </div>
                  <p>{chunk.content}</p>
                </article>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function formatLocator(locator: Record<string, unknown>) {
  const entries = Object.entries(locator).filter(([, value]) => {
    return typeof value === "string" || typeof value === "number";
  });
  return entries.map(([key, value]) => `${key}: ${value}`).join(" · ");
}

function RetrievalDebugger({
  run,
  events,
}: {
  run: QueryRunSummary | null;
  events: RetrievalEvent[];
}) {
  const payloads = events.map(eventPayload);
  const transforms = payloads
    .filter((payload) => String(payload.stage ?? "") === "query_transform")
    .flatMap((payload) => {
      const raw = payload.transforms;
      return Array.isArray(raw) ? raw : [];
    })
    .filter((item): item is Record<string, unknown> => {
      return typeof item === "object" && item !== null;
    });
  const candidates = payloads.flatMap(candidateRows);
  const queryYields = payloads.filter((payload) => {
    return payload.stage === "harness_tool" && payload.tool === "search";
  });
  const decisions = payloads.filter((payload) => {
    return Boolean(payload.decision || payload.reason || payload.reason_codes);
  });
  const context = payloads.find((payload) => {
    return (
      payload.stable_stage === "context_selection" ||
      payload.stage === "context"
    );
  });
  const answerEvent = payloads.find(
    (payload) => payload.stage === "answer_generation",
  );
  const citationEvent = payloads.find(
    (payload) => payload.stage === "citation_validation",
  );
  const feedbackEvents = payloads.filter(
    (payload) => payload.stage === "feedback" || payload.stage === "evaluation",
  );

  return (
    <>
      <div className="debug-summary">
        <span>{run?.status ?? "unknown"}</span>
        <span>{run?.mode ?? ""}</span>
        <span>{run?.model_alias ?? ""}</span>
        <code>{run?.trace_id ?? ""}</code>
      </div>

      <section className="debug-stage">
        <h3>Timeline</h3>
        <table>
          <thead>
            <tr>
              <th>Stage</th>
              <th>Subquery</th>
              <th>Count</th>
              <th>Latency</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {payloads.map((payload, index) => (
              <tr key={`timeline-${index}`}>
                <td>{stageName(payload)}</td>
                <td>{queryContextValue(payload, "subquery_id")}</td>
                <td>{String(payload.count ?? "")}</td>
                <td>
                  {String(payload.latency_ms ?? payload.stage_latency_ms ?? "")}
                </td>
                <td>
                  {String(
                    payload.status ?? payload.decision ?? payload.reason ?? "",
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {transforms.length > 0 && (
        <section className="debug-stage">
          <h3>Query Transforms</h3>
          <table>
            <thead>
              <tr>
                <th>Order</th>
                <th>ID</th>
                <th>Type</th>
                <th>Status</th>
                <th>Output Subqueries</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {transforms.map((transform, index) => (
                <tr key={`transform-${index}`}>
                  <td>{String(transform.order ?? index + 1)}</td>
                  <td>{String(transform.transform_id ?? "")}</td>
                  <td>{String(transform.type ?? "")}</td>
                  <td>
                    {String(
                      transform.status ?? (transform.changed ? "changed" : ""),
                    )}
                  </td>
                  <td>
                    <code>{queryRefIds(transform.query_refs)}</code>
                  </td>
                  <td>
                    <code>
                      {JSON.stringify(
                        transform.text ??
                          transform.queries ??
                          transform.reason ??
                          "",
                      )}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {queryYields.length > 0 && (
        <section className="debug-stage">
          <h3>Query Yield</h3>
          <table>
            <thead>
              <tr>
                <th>Step</th>
                <th>Subquery</th>
                <th>Transform</th>
                <th>New</th>
                <th>Selected</th>
                <th>Coverage</th>
                <th>Max BM25</th>
                <th>Max Dense</th>
                <th>Max Fusion</th>
                <th>Max Rerank</th>
                <th>Latency</th>
              </tr>
            </thead>
            <tbody>
              {queryYields.map((payload, index) => (
                <tr key={`query-yield-${index}`}>
                  <td>{String(payload.step ?? "")}</td>
                  <td>{queryContextValue(payload, "subquery_id")}</td>
                  <td>{queryContextValue(payload, "transform_id")}</td>
                  <td>{String(payload.new_evidence ?? "")}</td>
                  <td>{String(payload.selected_count ?? "")}</td>
                  <td>{formatNumber(payload.coverage)}</td>
                  <td>{formatNumber(payload.max_bm25_score)}</td>
                  <td>{formatNumber(payload.max_dense_score)}</td>
                  <td>{formatNumber(payload.max_fusion_score)}</td>
                  <td>{formatNumber(payload.max_rerank_score)}</td>
                  <td>{String(payload.latency_ms ?? "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {candidates.length > 0 && (
        <section className="debug-stage">
          <h3>Candidate Movement</h3>
          <table>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Subquery</th>
                <th>Chunk</th>
                <th>Document</th>
                <th>KB</th>
                <th>BM25</th>
                <th>Dense</th>
                <th>Fusion</th>
                <th>Rerank</th>
                <th>Final</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((row, index) => (
                <tr key={`candidate-${index}`}>
                  <td>{row.stage}</td>
                  <td>{row.subqueryId}</td>
                  <td>{row.chunkId}</td>
                  <td>{row.documentId}</td>
                  <td>{row.kbId}</td>
                  <td>{rankScore(row, "bm25")}</td>
                  <td>{rankScore(row, "dense")}</td>
                  <td>{rankScore(row, "fusion")}</td>
                  <td>{rankScore(row, "rerank")}</td>
                  <td>{row.ranks.final ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {decisions.length > 0 && (
        <section className="debug-stage">
          <h3>Decisions</h3>
          <table>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Chunk</th>
                <th>Decision</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((payload, index) => (
                <tr key={`decision-${index}`}>
                  <td>{stageName(payload)}</td>
                  <td>{String(payload.chunk_id ?? "")}</td>
                  <td>{String(payload.decision ?? "")}</td>
                  <td>
                    <code>
                      {JSON.stringify(
                        payload.reason_codes ?? payload.reason ?? "",
                      )}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {(context ||
        answerEvent ||
        citationEvent ||
        feedbackEvents.length > 0) && (
        <section className="debug-stage">
          <h3>Final Context</h3>
          <pre>
            {JSON.stringify(
              {
                context,
                answer_generation: answerEvent,
                citation_validation: citationEvent,
                feedback: feedbackEvents,
              },
              null,
              2,
            )}
          </pre>
        </section>
      )}

      {payloads.map((payload, index) => (
        <DebugStage key={`raw-${index}`} payload={payload} />
      ))}
    </>
  );
}

function DebugStage({ payload }: { payload: Record<string, unknown> }) {
  const stage = String(payload.stage ?? "");
  const rawCandidates = payload.candidates;
  const candidates = Array.isArray(rawCandidates)
    ? rawCandidates
        .filter((candidate): candidate is Record<string, unknown> => {
          return typeof candidate === "object" && candidate !== null;
        })
        .slice(0, 10)
    : [];
  return (
    <section className="debug-stage">
      <h3>{stage}</h3>
      {candidates.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Ranks</th>
              <th>Scores</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((row, index) => {
              return (
                <tr key={`${stage}-${index}`}>
                  <td>{String(row.title ?? row.chunk_id ?? "")}</td>
                  <td>
                    <code>{JSON.stringify(row.ranks ?? {})}</code>
                  </td>
                  <td>
                    <code>{JSON.stringify(row.scores ?? {})}</code>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <pre>{JSON.stringify(payload, null, 2)}</pre>
      )}
    </section>
  );
}

function eventPayload(event: RetrievalEvent): Record<string, unknown> {
  return typeof event.payload === "object" && event.payload !== null
    ? (event.payload as Record<string, unknown>)
    : (event as unknown as Record<string, unknown>);
}

function stageName(payload: Record<string, unknown>): string {
  return String(payload.stable_stage ?? payload.stage ?? "");
}

type CandidateRow = {
  stage: string;
  subqueryId: string;
  transformId: string;
  chunkId: string;
  documentId: string;
  kbId: string;
  ranks: Record<string, number>;
  scores: Record<string, number>;
};

function candidateRows(payload: Record<string, unknown>): CandidateRow[] {
  const raw = payload.candidates;
  const payloadSubqueryId = queryContextValue(payload, "subquery_id");
  const payloadTransformId = queryContextValue(payload, "transform_id");
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((candidate): candidate is Record<string, unknown> => {
      return typeof candidate === "object" && candidate !== null;
    })
    .map((candidate) => ({
      stage: stageName(payload),
      subqueryId: String(
        candidate.subquery_id ??
          queryContextValue(candidate, "subquery_id") ??
          payloadSubqueryId,
      ),
      transformId: String(
        candidate.transform_id ??
          queryContextValue(candidate, "transform_id") ??
          payloadTransformId,
      ),
      chunkId: String(candidate.chunk_id ?? ""),
      documentId: String(candidate.document_id ?? ""),
      kbId: String(candidate.knowledge_base_id ?? ""),
      ranks: numericRecord(candidate.ranks),
      scores: numericRecord(candidate.scores),
    }));
}

function queryContextValue(
  payload: Record<string, unknown>,
  key: string,
): string {
  const direct = payload[key];
  if (typeof direct === "string") return direct;
  const context = payload.query_context;
  if (typeof context === "object" && context !== null) {
    const value = (context as Record<string, unknown>)[key];
    if (typeof value === "string") return value;
  }
  return "";
}

function queryRefIds(value: unknown): string {
  if (!Array.isArray(value)) return "";
  const ids = value
    .filter((item): item is Record<string, unknown> => {
      return typeof item === "object" && item !== null;
    })
    .map((item) => String(item.subquery_id ?? ""))
    .filter(Boolean);
  return ids.join(", ");
}

function formatNumber(value: unknown): string {
  return typeof value === "number" ? value.toFixed(3) : "";
}

function numericRecord(value: unknown): Record<string, number> {
  if (typeof value !== "object" || value === null) return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).filter(
      (entry): entry is [string, number] => typeof entry[1] === "number",
    ),
  );
}

function rankScore(row: CandidateRow, key: string): string {
  const rank = row.ranks[key];
  const score = row.scores[key] ?? row.scores[`rrf_${key}`];
  if (rank === undefined && score === undefined) return "";
  return `${rank ?? ""}${score === undefined ? "" : ` / ${score.toFixed(3)}`}`;
}

function parseSse(block: string): { event: string; data: SsePayload } | null {
  const event = block
    .split("\n")
    .find((line) => line.startsWith("event: "))
    ?.replace("event: ", "");
  const data = block
    .split("\n")
    .find((line) => line.startsWith("data: "))
    ?.replace("data: ", "");
  if (!event || !data) return null;
  return { event, data: JSON.parse(data) };
}
